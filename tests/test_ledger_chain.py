"""The contribution ledger's tamper-evident hash chain (ADR 0017).

Tamper-*evident*, not tamper-proof, and the distinction is the point. An
operator with database access can rewrite every entry and every link and this
will report a clean chain. What it catches is accidental corruption, a partial
edit, and casual modification. One test below deliberately proves the limitation
rather than papering over it.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import ledger
import server_state as state
from ledger import (
    LEDGER_CHAIN_GENESIS_DIGEST,
    chain_entry_digest,
    ensure_contribution_schema,
    insert_contribution_in_transaction,
    verify_ledger_chain,
)
from scripts import ledger_chain_admin
from sqlite_store import connect
from tests.protocol_harness import CREDENTIALS, REQUESTER_HOSTS, TASK_TEXTS, CoordinatorHarness


@pytest.fixture
def database(tmp_path):
    path = Path(tmp_path) / "events.db"
    con = connect(path, row_factory=sqlite3.Row)
    ensure_contribution_schema(con)
    con.commit()
    con.close()
    return path


@pytest.fixture
def harness(tmp_path):
    coordinator = CoordinatorHarness(Path(tmp_path) / "state")
    try:
        yield coordinator
    finally:
        coordinator.close()


def _append(path: Path, index: int, *, created_at: float | None = None) -> bool:
    con = connect(path, row_factory=sqlite3.Row)
    try:
        con.execute("BEGIN IMMEDIATE")
        inserted = insert_contribution_in_transaction(
            con,
            contribution_id=f"attempt:{index}",
            contributor="n0",
            contribution_type="compute",
            points=5,
            basis="compute_contribution",
            attempt_id=f"a{index}",
            enrollment_id="1" * 32,
            node_id="n0",
            created_at=1000.0 + index if created_at is None else created_at,
        )
        con.commit()
        return inserted
    finally:
        con.close()


# ── a clean chain ────────────────────────────────────────────────────


def test_entries_link_from_a_defined_genesis(database):
    for index in range(3):
        assert _append(database, index) is True

    result = verify_ledger_chain(database)
    assert result.ok is True
    assert result.chained_entries == 3
    assert result.genesis_unchained_entries == 0

    with sqlite3.connect(database) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM contributions ORDER BY entry_index"
        ).fetchall()
    assert [row["entry_index"] for row in rows] == [0, 1, 2]
    assert rows[0]["previous_digest"] == LEDGER_CHAIN_GENESIS_DIGEST
    assert rows[1]["previous_digest"] == rows[0]["entry_digest"]
    assert rows[2]["previous_digest"] == rows[1]["entry_digest"]


def test_entries_predating_the_chain_are_the_genesis_boundary(database):
    """History is never rewritten to fabricate links it did not have."""
    with sqlite3.connect(database) as con:
        con.execute(
            "INSERT INTO contributions (contribution_id, contributor, "
            "contribution_type, points, task, details, basis, "
            "points_are_monetary, created_at) VALUES "
            "('legacy:1', 'old-node', 'compute', 5, 'compute_contribution', '', "
            "'compute_contribution', 0, 1.0)"
        )
        con.commit()

    _append(database, 0)
    result = verify_ledger_chain(database)

    assert result.ok is True
    assert result.genesis_unchained_entries == 1
    assert result.chained_entries == 1
    with sqlite3.connect(database) as con:
        legacy = con.execute(
            "SELECT entry_index, entry_digest FROM contributions "
            "WHERE contribution_id = 'legacy:1'"
        ).fetchone()
    assert legacy == (None, None), "a pre-chain entry was retrofitted with a link"


# ── replay, restart, and concurrency ─────────────────────────────────


def test_a_replayed_contribution_creates_no_second_entry_and_no_gap(database):
    _append(database, 0)
    _append(database, 1)

    assert _append(database, 0, created_at=9999.0) is False

    result = verify_ledger_chain(database)
    assert result.ok is True
    assert result.chained_entries == 2, "a replay forked or duplicated the chain"


def test_an_ambiguous_commit_that_retries_resolves_to_one_entry(database):
    """Theme 1.1's lesson: one entry, not two, and not a gap."""
    _append(database, 0)

    # The caller never learned whether its commit landed, so it retries.
    for _ in range(4):
        assert _append(database, 0) is False

    result = verify_ledger_chain(database)
    assert result.ok is True
    assert result.chained_entries == 1


def test_a_restart_between_appends_continues_one_chain(database):
    _append(database, 0)
    # A restart is a new process against the same database; the chain head is
    # read from durable state, not from anything held in memory.
    _append(database, 1)
    _append(database, 2)

    result = verify_ledger_chain(database)
    assert result.ok is True
    assert result.chained_entries == 3


def test_concurrent_appends_serialize_into_one_unambiguous_order(database):
    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(lambda index: _append(database, index), range(12)))

    assert all(outcomes), "a concurrent append was silently dropped"
    result = verify_ledger_chain(database)
    assert result.ok is True, result.reason
    assert result.chained_entries == 12
    with sqlite3.connect(database) as con:
        indices = [
            row[0]
            for row in con.execute(
                "SELECT entry_index FROM contributions ORDER BY entry_index"
            )
        ]
    assert indices == list(range(12)), "concurrent appends produced an ambiguous order"


# ── settlement atomicity ─────────────────────────────────────────────


def test_the_chain_rides_the_settlement_transaction(harness):
    """Appending a link must not need settlement to be relaxed to fit it."""
    assert harness.register("n0", CREDENTIALS[0], "bootstrap").status_code == 200
    execution = harness.submit_execution(
        host=REQUESTER_HOSTS[0], task=TASK_TEXTS[0], idempotency_key=None
    )
    execution_id = execution.json()["execution_id"]
    harness.enqueue_unit("u0", execution_id=execution_id, unit_id="candidate-u0")
    handout = harness.poll("n0")
    assert handout is not None

    body = harness.result_body(handout)
    assert harness.submit("u0", body, label="n0").status_code == 200

    receipts = harness.durable_receipts()
    credits = harness.durable_credits()
    assert set(receipts) == set(credits), "settlement and credit diverged"
    chained = harness.rows(
        "SELECT contribution_id, entry_index, entry_digest FROM contributions "
        "WHERE entry_index IS NOT NULL"
    )
    assert len(chained) == 1, "settlement produced no chained ledger entry"
    assert verify_ledger_chain(harness.database).ok is True

    # And a replayed settlement adds neither a receipt nor a link.
    assert harness.submit("u0", body, label="n0").status_code == 200
    assert len(harness.durable_credits()) == 1
    assert verify_ledger_chain(harness.database).chained_entries == 1


@pytest.mark.parametrize("fault_index", list(range(0, 14)))
def test_a_persistence_fault_during_settlement_leaves_no_fork_or_gap(tmp_path, fault_index):
    harness = CoordinatorHarness(Path(tmp_path) / "state")
    try:
        assert harness.register("n0", CREDENTIALS[0], "bootstrap").status_code == 200
        execution = harness.submit_execution(
            host=REQUESTER_HOSTS[0], task=TASK_TEXTS[0], idempotency_key=None
        )
        execution_id = execution.json()["execution_id"]
        harness.enqueue_unit("u0", execution_id=execution_id, unit_id="candidate-u0")
        handout = harness.poll("n0")
        assert handout is not None

        harness.faults.arm(target_index=fault_index, mode="io")
        try:
            harness.submit("u0", harness.result_body(handout), label="n0")
        except Exception:
            pass
        finally:
            harness.faults.disarm()

        result = verify_ledger_chain(harness.database)
        assert result.ok is True, (
            f"a fault at index {fault_index} broke the chain: {result.reason}"
        )
        receipts = harness.durable_receipts()
        credits = harness.durable_credits()
        assert set(receipts) == set(credits)
        assert result.chained_entries == len(credits), (
            "a chained entry exists without its contribution, or the reverse"
        )

        harness.restart()
        after = verify_ledger_chain(harness.database)
        assert after.ok is True
        assert after.chained_entries == result.chained_entries
    finally:
        harness.close()


# ── detection, and its honest limit ──────────────────────────────────


def test_a_single_edited_entry_is_detected_at_the_right_index(database):
    for index in range(4):
        _append(database, index)

    with sqlite3.connect(database) as con:
        con.execute(
            "UPDATE contributions SET points = 500 WHERE contribution_id = 'attempt:2'"
        )
        con.commit()

    result = verify_ledger_chain(database)
    assert result.ok is False
    assert result.break_at_index == 2
    assert result.break_entry_id == "attempt:2"
    assert result.expected_digest and result.observed_digest
    assert result.expected_digest != result.observed_digest
    assert "does not match its recorded digest" in result.reason


def test_a_deleted_middle_entry_is_detected_as_a_gap(database):
    for index in range(4):
        _append(database, index)
    with sqlite3.connect(database) as con:
        con.execute("DELETE FROM contributions WHERE contribution_id = 'attempt:1'")
        con.commit()

    result = verify_ledger_chain(database)
    assert result.ok is False
    assert result.break_at_index == 1
    assert "gap or a duplicate" in result.reason


def test_a_relinked_entry_is_detected_where_its_link_stops_matching(database):
    """Editing one entry and fixing only *its own* digest still breaks the next."""
    for index in range(3):
        _append(database, index)
    with sqlite3.connect(database) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM contributions WHERE contribution_id = 'attempt:0'"
        ).fetchone()
        content = {key: row[key] for key in ledger._CHAINED_FIELDS}
        content["points"] = 500.0
        con.execute(
            "UPDATE contributions SET points = 500, entry_digest = ? "
            "WHERE contribution_id = 'attempt:0'",
            (
                chain_entry_digest(
                    entry_index=0,
                    previous_digest=LEDGER_CHAIN_GENESIS_DIGEST,
                    content=content,
                ),
            ),
        )
        con.commit()

    result = verify_ledger_chain(database)
    assert result.ok is False
    assert result.break_at_index == 1, (
        "the edit should surface at the first entry whose link no longer matches"
    )


def test_a_fully_recomputed_chain_is_not_detectable(database):
    """The honest limit, asserted rather than hidden.

    An operator with write access who edits an entry *and* recomputes every link
    after it produces a chain that verifies clean. This mechanism is tamper
    evidence against corruption and casual edits; it is not a defence against the
    party that holds the database, and ADR 0017 says so in those words.
    """
    for index in range(3):
        _append(database, index)

    with sqlite3.connect(database) as con:
        con.row_factory = sqlite3.Row
        rows = [
            dict(row)
            for row in con.execute("SELECT * FROM contributions ORDER BY entry_index")
        ]
        rows[0]["points"] = 500.0
        previous = LEDGER_CHAIN_GENESIS_DIGEST
        for row in rows:
            content = {key: row[key] for key in ledger._CHAINED_FIELDS}
            digest = chain_entry_digest(
                entry_index=int(row["entry_index"]),
                previous_digest=previous,
                content=content,
            )
            con.execute(
                "UPDATE contributions SET points = ?, previous_digest = ?, "
                "entry_digest = ? WHERE contribution_id = ?",
                (row["points"], previous, digest, row["contribution_id"]),
            )
            previous = digest
        con.commit()

    result = verify_ledger_chain(database)
    assert result.ok is True, (
        "if this ever starts failing, the mechanism gained a property it does "
        "not claim, and ADR 0017 needs updating"
    )


# ── the operator command ─────────────────────────────────────────────


def test_the_verify_command_reports_a_clean_chain(database, capsys):
    _append(database, 0)

    assert ledger_chain_admin.main(["--state-dir", str(database.parent), "verify"]) == 0

    rendered = capsys.readouterr().out
    assert "chain:             intact" in rendered
    assert "tamper evidence, not tamper proofing" in rendered
    assert "not\nproof that any recorded work happened or was correct" in rendered


def test_the_verify_command_reports_a_break_and_exits_nonzero(database, capsys):
    for index in range(3):
        _append(database, index)
    with sqlite3.connect(database) as con:
        con.execute("UPDATE contributions SET points = 9 WHERE contribution_id = 'attempt:1'")
        con.commit()

    assert ledger_chain_admin.main(["--state-dir", str(database.parent), "verify"]) == 1

    rendered = capsys.readouterr().out
    assert "chain:             BROKEN" in rendered
    assert "first break at index: 1" in rendered
    assert "attempt:1" in rendered


def test_verification_output_carries_no_sensitive_content(database, capsys):
    _append(database, 0)
    assert (
        ledger_chain_admin.main(["--state-dir", str(database.parent), "verify", "--json"])
        == 0
    )
    rendered = capsys.readouterr().out

    for forbidden in ("synthetic prompt", "output", "credential", "token", "nonce", "secret"):
        assert forbidden not in rendered.lower(), f"verification output leaked {forbidden!r}"
    for forbidden in ("verified", "tamper-proof", "tamperproof", "trustless"):
        assert forbidden not in rendered.lower()


def test_the_command_refuses_a_missing_state_directory(tmp_path, capsys):
    missing = Path(tmp_path) / "not-here"
    assert ledger_chain_admin.main(["--state-dir", str(missing), "verify"]) == 2
    assert "state directory does not exist" in capsys.readouterr().err


def test_standings_still_read_the_ledger_after_chaining(database, monkeypatch):
    monkeypatch.setattr(ledger, "LEDGER_DB_FILE", database)
    monkeypatch.setattr(ledger, "LEDGER_FILE", database.parent / "ledger.json")
    ledger._cache = None
    _append(database, 0)
    _append(database, 1)

    standings = ledger.get_standings()
    assert len(standings) == 1
    assert standings[0]["total_points"] == 10
    assert standings[0]["points_are_monetary"] is False
    assert state is not None
