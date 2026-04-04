"""
Contribution ledger — tracks who contributed what to the network.

This is the seed of the guild economics layer. Every compute contribution,
every pitch, every review gets logged. Simple append-only JSON file for now,
cryptographic integrity (Merkle tree) comes later.

Usage:
    from ledger import log_contribution, get_standings

    log_contribution("node-1", "compute", credits=47.2, task="build_3_1712...")
    standings = get_standings()
"""

import json
import time
from pathlib import Path

LEDGER_FILE = Path("ledger.json")


def _load() -> list[dict]:
    """Load the ledger from disk."""
    if LEDGER_FILE.exists():
        try:
            return json.loads(LEDGER_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save(entries: list[dict]):
    """Save the ledger to disk."""
    LEDGER_FILE.write_text(json.dumps(entries, indent=2))


def log_contribution(
    contributor_id: str,
    contribution_type: str,
    credits: float = 0,
    task: str = "",
    details: str = "",
):
    """Record a contribution to the ledger.

    Types:
      "compute"  — node executed a builder task
      "pitch"    — someone pitched a task
      "review"   — reviewer pass completed
      "uptime"   — node was available (future)
    """
    entries = _load()
    entries.append({
        "contributor": contributor_id,
        "type": contribution_type,
        "credits": credits,
        "task": task,
        "details": details,
        "timestamp": time.time(),
    })
    _save(entries)


def get_standings() -> list[dict]:
    """Get contributor standings sorted by total credits.

    Returns list of:
      {"contributor": str, "total_credits": float, "contributions": int,
       "compute_tasks": int, "pitches": int}
    """
    entries = _load()
    contributors: dict[str, dict] = {}

    for entry in entries:
        cid = entry["contributor"]
        if cid not in contributors:
            contributors[cid] = {
                "contributor": cid,
                "total_credits": 0,
                "contributions": 0,
                "compute_tasks": 0,
                "pitches": 0,
            }
        c = contributors[cid]
        c["total_credits"] += entry.get("credits", 0)
        c["contributions"] += 1
        if entry["type"] == "compute":
            c["compute_tasks"] += 1
        elif entry["type"] == "pitch":
            c["pitches"] += 1

    standings = sorted(contributors.values(), key=lambda x: x["total_credits"], reverse=True)
    return standings


def get_history(contributor_id: str | None = None, limit: int = 50) -> list[dict]:
    """Get recent ledger entries, optionally filtered by contributor."""
    entries = _load()
    if contributor_id:
        entries = [e for e in entries if e["contributor"] == contributor_id]
    return entries[-limit:]
