"""Shared SQLite connection, retry, transaction, and migration policy.

Mycelium supports one coordinator process per state directory, but many
coroutines and background threads inside that process write the same database.
Every production ``events.db`` user goes through this module so WAL, foreign
keys, busy handling, and failure bounds cannot drift between stores.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar

SQLITE_TIMEOUT_SECONDS = 10.0
SQLITE_BUSY_TIMEOUT_MS = 10_000
SQLITE_BUSY_RETRIES = 5
SQLITE_RETRY_BASE_SECONDS = 0.02
SQLITE_SYNCHRONOUS = "NORMAL"

_T = TypeVar("_T")
_migration_guard = threading.Lock()
_migration_locks: dict[str, threading.RLock] = {}


def _is_transient_busy(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    message = str(exc).lower()
    return "database is locked" in message or "database is busy" in message


def retry_busy(operation: Callable[[], _T], *, attempts: int = SQLITE_BUSY_RETRIES) -> _T:
    """Retry only transient lock failures, with a small finite backoff."""
    if attempts < 1:
        raise ValueError("attempts must be positive")
    for attempt in range(attempts):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not _is_transient_busy(exc) or attempt + 1 >= attempts:
                raise
            time.sleep(SQLITE_RETRY_BASE_SECONDS * (2**attempt))
    raise AssertionError("unreachable")


class RetryConnection(sqlite3.Connection):
    """Connection whose individual SQL operations have bounded busy retry."""

    def execute(self, sql: str, parameters: Any = (), /):  # type: ignore[override]
        return retry_busy(lambda: super(RetryConnection, self).execute(sql, parameters))

    def executemany(self, sql: str, seq_of_parameters, /):  # type: ignore[override]
        return retry_busy(
            lambda: super(RetryConnection, self).executemany(sql, seq_of_parameters)
        )

    def executescript(self, sql_script: str, /):  # type: ignore[override]
        return retry_busy(lambda: super(RetryConnection, self).executescript(sql_script))

    def commit(self) -> None:  # type: ignore[override]
        retry_busy(lambda: super(RetryConnection, self).commit())


def connect(
    path: str | Path,
    *,
    row_factory: Any | None = None,
    wal: bool = True,
) -> RetryConnection:
    """Open one consistently configured SQLite connection."""
    database = str(Path(path))
    con = sqlite3.connect(
        database,
        timeout=SQLITE_TIMEOUT_SECONDS,
        factory=RetryConnection,
    )
    try:
        con.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        con.execute("PRAGMA foreign_keys = ON")
        if wal and database != ":memory:":
            # WAL is persistent for the database. Reissuing the pragma is safe
            # and keeps independently constructed store objects coherent.
            con.execute("PRAGMA journal_mode = WAL")
        con.execute(f"PRAGMA synchronous = {SQLITE_SYNCHRONOUS}")
        if row_factory is not None:
            con.row_factory = row_factory
        return con
    except Exception:
        con.close()
        raise


@contextlib.contextmanager
def connection(
    path: str | Path,
    *,
    row_factory: Any | None = None,
    wal: bool = True,
) -> Iterator[RetryConnection]:
    con = connect(path, row_factory=row_factory, wal=wal)
    try:
        yield con
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


@contextlib.contextmanager
def transaction(
    path: str | Path,
    *,
    immediate: bool = True,
    row_factory: Any | None = None,
) -> Iterator[RetryConnection]:
    """Open, begin, commit, and close a finite-retry transaction."""
    with connection(path, row_factory=row_factory) as con:
        con.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield con
        except Exception:
            con.rollback()
            raise
        else:
            con.commit()


def migration_lock(path: str | Path) -> threading.RLock:
    """Return the process-wide migration lock for a database path."""
    key = str(Path(path).resolve())
    with _migration_guard:
        return _migration_locks.setdefault(key, threading.RLock())


def foreign_keys_enabled(path: str | Path) -> bool:
    """Operational diagnostic used by preflight and tests."""
    with connection(path) as con:
        row = con.execute("PRAGMA foreign_keys").fetchone()
        return bool(row and row[0] == 1)
