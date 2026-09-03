#!/usr/bin/env python3
"""Verify the contribution ledger's tamper-evident hash chain.

    python scripts/ledger_chain_admin.py verify
    python scripts/ledger_chain_admin.py --state-dir /srv/mycelium verify --json

Each ledger entry carries the digest of the entry before it, so changing any
entry breaks every link after it and this reports where.

**What a passing verification means, and what it does not.** It means no entry
was changed without also recomputing every link after it. It is *tamper
evidence*, not tamper proofing: an operator with write access to this database
can rewrite every entry and every link, and this will then report a clean chain.
There is no consensus here, no external anchor, and nobody outside this machine
attesting to anything. A clean chain is not proof that any recorded work
happened, was correct, or is owed anything. See ADR 0017.

Output carries an index, an entry ID, and two digests. No credentials, prompts,
outputs, or artifact contents can appear, because none of them are in the
chained columns.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - script entry convenience
    sys.path.insert(0, str(REPO_ROOT))

from coordinator_lock import default_state_dir  # noqa: E402
from ledger import verify_ledger_chain  # noqa: E402


class LedgerChainAdminError(RuntimeError):
    """A ledger chain administration request cannot be carried out."""


def _database_path(state_dir: Path | str | None) -> Path:
    root = Path(state_dir) if state_dir is not None else default_state_dir()
    root = root.expanduser()
    if not root.exists() or not root.is_dir():
        raise LedgerChainAdminError(f"state directory does not exist: {root}")
    database = root / "events.db"
    if not database.exists():
        raise LedgerChainAdminError(f"no coordinator database at {database}")
    return database


def verify(state_dir: Path | str | None, *, json_output: bool = False) -> int:
    result = verify_ledger_chain(_database_path(state_dir))
    if json_output:
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
        return 0 if result.ok else 1

    print(f"chained entries:   {result.chained_entries}")
    print(f"pre-chain entries: {result.genesis_unchained_entries}")
    if result.ok:
        print("chain:             intact")
        print()
        print(
            "This means no entry was changed without also recomputing every link\n"
            "after it. It is tamper evidence, not tamper proofing, and it is not\n"
            "proof that any recorded work happened or was correct."
        )
        return 0

    print("chain:             BROKEN")
    print(f"first break at index: {result.break_at_index}")
    print(f"entry id:             {result.break_entry_id}")
    print(f"reason:               {result.reason}")
    if result.expected_digest:
        print(f"expected digest:      {result.expected_digest}")
    if result.observed_digest:
        print(f"observed digest:      {result.observed_digest}")
    print()
    print(
        "Investigate before trusting any standings computed from this ledger.\n"
        "A break means an entry changed after it was written, which may be disk\n"
        "corruption, a partial restore, or an edit."
    )
    return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-dir",
        type=Path,
        help="Mycelium coordinator state directory (default: MYCELIUM_STATE_DIR or .)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    verification = commands.add_parser("verify", help="walk the chain and report breaks")
    verification.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return verify(args.state_dir, json_output=args.json_output)
    except LedgerChainAdminError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - script entry point
    raise SystemExit(main())
