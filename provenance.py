"""Durable provenance envelopes: a binding of identity, not a claim of correctness.

An envelope records *how* an artifact set was produced and *under whose
identity*. It says nothing about whether the output is correct, useful, or
honest, and no field, response, or document here may suggest otherwise. SLSA and
in-toto draw exactly that line; this draws it too.

What an envelope binds together, from facts that already existed separately:
the execution and its accepted attempts and receipts, the producing enrollment
and its node label at the time, the capability descriptor version and hash, the
executor and worker protocol, the selected model and the sampling parameters it
was asked for, the validators that ran and their outcomes, and the sealed
per-file artifact hashes.

The `sampling` block is scoped rather than asserted. It records the temperature
and seed the *coordinator* was configured with when the envelope sealed, says
so in its own `scope` field, and never speaks for a distributed producer: that
machine reads its own configuration and the worker protocol does not carry it
back, so `producer_sampling` goes in `unknown_facts` whenever a producer
exists. A seed set but not shown to be honoured is `pinned: false` and adds
`sampling_seed_honoured` — see `sampling.py` for what is verified and what is
assumed. The block is **additive within envelope version 1**, on the same
reasoning ADR 0017 already applies to the audit bundle: envelopes sealed before
it keep their stored payload and their digest verifies unchanged, and a reader
that does not know the key still reads every field it always did.

Three constraints shape it:

* **Terminal state is not mutated.** ADR 0009 makes terminal execution state
  monotonic. The envelope references an execution; it never lives on one, and it
  has no foreign key that could block or cascade into terminal state.
* **Absence is explicit.** A missing model digest, a legacy session with no
  enrollment, an execution that produced no distributed attempt - each is
  recorded as unknown and listed in `unknown_facts`. Nothing is inferred, and
  nothing is backfilled with a guess.
* **The digest is deterministic.** Canonical JSON - sorted keys, compact
  separators, UTF-8 - hashed with SHA-256, so the same production facts always
  yield the same envelope digest.

There is a reserved slot for a future signature. It is never populated here.
Signing, key management, transparency logs, in-toto layouts and SLSA attestation
formats are all deliberately absent; see ADR 0017.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlite_store import connection, migration_lock


PROVENANCE_ENVELOPE_VERSION = "1"

# The digest covers the production facts. It deliberately excludes the reserved
# signature slot: a signature is over the digest, so including it would make the
# digest depend on itself.
_DIGEST_DOMAIN = "mycelium.provenance-envelope.v1"

MAX_ENVELOPE_JSON_BYTES = 1_048_576

# Every fact an envelope can be missing. Named so absence is a value rather than
# a silence, and so a reader can tell "we do not know" from "there was nothing".
UNKNOWN_PRODUCER_IDENTITY = "producer_identity"
UNKNOWN_SINGLE_PRODUCER = "single_producer"
UNKNOWN_ENROLLMENT = "enrollment_id"
UNKNOWN_DESCRIPTOR = "capability_descriptor"
UNKNOWN_EXECUTOR_VERSION = "executor_version"
UNKNOWN_WORKER_PROTOCOL_VERSION = "worker_protocol_version"
UNKNOWN_MODEL_DIGEST = "model_digest"
UNKNOWN_MODEL_VARIANT = "model_variant"
UNKNOWN_VALIDATORS = "validators"
UNKNOWN_SAMPLING = "sampling_parameters"
UNKNOWN_SEED_HONOURED = "sampling_seed_honoured"
UNKNOWN_PRODUCER_SAMPLING = "producer_sampling"

# The scope of the envelope's sampling block, said in the envelope itself.
# A distributed producer reads its own configuration and the worker protocol
# does not carry it back, so the coordinator knows what *it* was configured
# with and nothing more. Recording that as the producer's setting would be a
# guess, which is the one thing this envelope does not do.
SAMPLING_SCOPE = (
    "the coordinator configuration in force when this envelope sealed. A "
    "distributed producer applies its own configuration and does not report "
    "it, so this pins the locally executed path only."
)


def canonical_json(payload: Any) -> str:
    """Sorted keys, compact separators, UTF-8. The one serialization."""

    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def envelope_digest(payload: Mapping[str, Any]) -> str:
    """SHA-256 over the canonical form of the production facts."""

    material = canonical_json(payload)
    return hashlib.sha256(
        _DIGEST_DOMAIN.encode("ascii") + b"\0" + material.encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class ProvenanceEnvelopeRecord:
    execution_id: str
    envelope_version: str
    envelope_digest: str
    payload: Mapping[str, Any]
    signature: str | None
    signature_algorithm: str | None
    created_at: float

    @property
    def is_signed(self) -> bool:
        """Always False today. The slot is reserved, not implemented."""
        return self.signature is not None

    def as_export(self) -> dict[str, Any]:
        """The form written into an audit bundle and read back offline."""

        return {
            **dict(self.payload),
            "envelope_digest": self.envelope_digest,
            # Reserved. Documented in ADR 0017 so that adding signing later is
            # not a schema break for anyone already reading these bundles.
            "signature": self.signature,
            "signature_algorithm": self.signature_algorithm,
        }


def ensure_provenance_schema(con: sqlite3.Connection) -> None:
    """Install the additive append-only envelope schema.

    One envelope per execution: the primary key is the execution ID, which is
    what makes accepted-result replay resolve to the identical envelope rather
    than creating a second one. No foreign key into executions or attempts -
    the envelope references terminal state and must never be able to reach it.
    """

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS provenance_envelopes (
            execution_id     TEXT PRIMARY KEY CHECK(length(execution_id) BETWEEN 1 AND 256),
            envelope_version TEXT NOT NULL CHECK(envelope_version = '1'),
            envelope_digest  TEXT NOT NULL CHECK(length(envelope_digest) = 64),
            envelope_json    TEXT NOT NULL CHECK(length(envelope_json) <= 1048576),
            manifest_digest  TEXT CHECK(manifest_digest IS NULL OR length(manifest_digest) = 64),
            -- Reserved for a future detached signature over `envelope_digest`.
            -- Never populated by this code; see ADR 0017.
            signature            TEXT CHECK(signature IS NULL OR length(signature) <= 4096),
            signature_algorithm  TEXT CHECK(signature_algorithm IS NULL OR length(signature_algorithm) <= 64),
            created_at       REAL NOT NULL
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_provenance_envelopes_created "
        "ON provenance_envelopes(created_at, execution_id)"
    )
    con.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_provenance_envelopes_no_update
        BEFORE UPDATE ON provenance_envelopes
        BEGIN
            SELECT RAISE(ABORT, 'provenance envelopes are append-only');
        END
        """
    )
    con.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_provenance_envelopes_no_delete
        BEFORE DELETE ON provenance_envelopes
        BEGIN
            SELECT RAISE(ABORT, 'provenance envelopes are append-only');
        END
        """
    )


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


class ProvenanceEnvelopeStore:
    """Builds and stores one envelope per execution.

    The store assembles the identity chain from durable rows it reads itself, so
    no caller has to thread identity through the artifact path. The caller
    supplies only what it alone knows: the sealed manifest and the ordered list
    of validators that ran.
    """

    def __init__(self, path: str | Path = "events.db"):
        self.path = Path(path)
        self._lock = threading.RLock()

    def migrate(self) -> None:
        with self._lock, migration_lock(self.path), connection(
            self.path, row_factory=sqlite3.Row
        ) as con:
            ensure_provenance_schema(con)
            con.commit()

    # ── assembling the identity chain ────────────────────────────────

    def _descriptor_facts(
        self, con: sqlite3.Connection, enrollment_id: str | None, descriptor_hash: str | None
    ) -> tuple[dict[str, Any], list[str]]:
        """Executor and worker-protocol facts, from the immutable snapshot."""

        unknown: list[str] = []
        facts: dict[str, Any] = {
            "kind": None,
            "version": None,
            "worker_protocol_version": None,
        }
        if not enrollment_id or not descriptor_hash:
            unknown.append(UNKNOWN_DESCRIPTOR)
            return facts, unknown
        try:
            row = con.execute(
                "SELECT descriptor_json FROM node_capability_snapshots "
                "WHERE enrollment_id = ? AND descriptor_hash = ?",
                (enrollment_id, descriptor_hash),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row is None:
            unknown.append(UNKNOWN_DESCRIPTOR)
            return facts, unknown
        try:
            descriptor = json.loads(str(row["descriptor_json"]))
            executor = descriptor.get("executor") or {}
        except (ValueError, AttributeError):
            unknown.append(UNKNOWN_DESCRIPTOR)
            return facts, unknown
        facts["kind"] = _text(executor.get("kind"))
        facts["version"] = _text(executor.get("version"))
        facts["worker_protocol_version"] = _text(executor.get("worker_protocol_version"))
        if facts["version"] is None:
            unknown.append(UNKNOWN_EXECUTOR_VERSION)
        if facts["worker_protocol_version"] is None:
            unknown.append(UNKNOWN_WORKER_PROTOCOL_VERSION)
        return facts, unknown

    def _producers(
        self, con: sqlite3.Connection, execution_id: str
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Every accepted receipt for this execution, with its identity chain."""

        unknown: list[str] = []
        try:
            rows = con.execute(
                "SELECT * FROM accepted_result_receipts WHERE execution_id = ? "
                "ORDER BY accepted_at, attempt_id",
                (execution_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        producers: list[dict[str, Any]] = []
        for row in rows:
            enrollment_id = _text(row["assigned_enrollment_id"])
            descriptor_hash = _text(row["assigned_descriptor_hash"])
            executor, executor_unknown = self._descriptor_facts(
                con, enrollment_id, descriptor_hash
            )
            model_digest = _text(row["assigned_model_digest"])
            entry_unknown = list(executor_unknown)
            if enrollment_id is None:
                entry_unknown.append(UNKNOWN_ENROLLMENT)
            if model_digest is None:
                entry_unknown.append(UNKNOWN_MODEL_DIGEST)
            producers.append(
                {
                    "attempt_id": _text(row["attempt_id"]),
                    "receipt_id": _text(row["attempt_id"]),
                    "unit_id": _text(row["execution_unit_id"]),
                    "task_class": _text(row["execution_unit_kind"]),
                    "identity": {
                        "enrollment_id": enrollment_id,
                        # The label as it was at settlement. A label is display
                        # metadata and is never a trust key; it is recorded so a
                        # reader can recognise the machine, not authenticate it.
                        "node_id": _text(row["assigned_node_id"]),
                        "identity_class": "enrolled" if enrollment_id else "legacy",
                    },
                    "capability": {
                        "descriptor_version": _text(row["assigned_descriptor_version"]),
                        "descriptor_hash": descriptor_hash,
                    },
                    "executor": executor,
                    "model": {
                        "provider": _text(row["assigned_model_provider"]),
                        "name": _text(row["assigned_model_name"]),
                        "digest": model_digest,
                        "variant": None,
                    },
                    "settlement": {
                        "accepted_at": float(row["accepted_at"]),
                        "terminal_cause": _text(row["terminal_cause"]),
                        "result_hash": _text(row["result_hash"]),
                    },
                    "unknown_facts": sorted(set(entry_unknown)),
                }
            )
        if not producers:
            # A locally executed execution produced no distributed attempt. That
            # is a fact, not a gap to be filled in with the coordinator's own
            # identity.
            unknown.append(UNKNOWN_PRODUCER_IDENTITY)
        # Variant is not carried on the receipt; it is a capability-evidence
        # scope field resolved from the descriptor's model list, and inferring it
        # here would be a guess.
        if producers:
            unknown.append(UNKNOWN_MODEL_VARIANT)
        return producers, unknown

    # ── building and recording ───────────────────────────────────────

    @staticmethod
    def _sampling(
        sampling: Mapping[str, Any] | None, producers: Sequence[Mapping[str, Any]]
    ) -> tuple[dict[str, Any], list[str]]:
        """How the generator was configured, and how far that claim reaches.

        Three separate absences, kept separate because they mean different
        things. `sampling_parameters` means nothing was pinned at all — the
        shipping default, where Ollama applies its own temperature and seed.
        `sampling_seed_honoured` means a seed was set but has not been shown to
        be honoured by the runner for this model, so the generator is not
        actually fixed (`sampling.py`). `producer_sampling` means a distributed
        attempt settled this execution, and what that machine sampled with is
        not carried on its receipt — the same reason `model_variant` is
        unknown whenever there is a producer.
        """
        record = dict(sampling or {})
        temperature = record.get("temperature")
        seed = record.get("seed")
        honouring = record.get("seed_honouring")
        payload = {
            "temperature": None if temperature is None else float(temperature),
            "seed": None if seed is None else int(seed),
            "seed_honouring": honouring,
            "pinned": bool(record.get("pinned", False)),
            "scope": SAMPLING_SCOPE,
        }
        unknown: list[str] = []
        if payload["temperature"] is None and payload["seed"] is None:
            unknown.append(UNKNOWN_SAMPLING)
        elif not payload["pinned"]:
            unknown.append(UNKNOWN_SEED_HONOURED)
        if producers:
            unknown.append(UNKNOWN_PRODUCER_SAMPLING)
        return payload, unknown

    def build_payload(
        self,
        con: sqlite3.Connection,
        execution_id: str,
        *,
        manifest: Any,
        validators: Sequence[Mapping[str, Any]] | None,
        created_at: float,
        sampling: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        producers, unknown = self._producers(con, execution_id)
        sampling_record, sampling_unknown = self._sampling(sampling, producers)
        unknown.extend(sampling_unknown)

        validator_records: list[dict[str, Any]] = []
        for item in validators or ():
            validator_records.append(
                {
                    "name": _text(item.get("name")),
                    "version": _text(item.get("version")),
                    # The validator's own outcome. `passed` here means a
                    # mechanical check ran and did not fail; it is not a claim
                    # that the artifact does what its requester wanted.
                    "outcome": _text(item.get("outcome")),
                }
            )
        if not validator_records:
            unknown.append(UNKNOWN_VALIDATORS)

        entries = [
            {
                "relative_path": entry.relative_path,
                "sha256": entry.sha256,
                "size_bytes": int(entry.size_bytes),
                "role": str(entry.role),
            }
            for entry in sorted(manifest.entries, key=lambda item: item.relative_path)
        ]

        single = producers[0] if len(producers) == 1 else None
        if len(producers) > 1:
            unknown.append(UNKNOWN_SINGLE_PRODUCER)

        return {
            "envelope_version": PROVENANCE_ENVELOPE_VERSION,
            "execution_id": execution_id,
            "attempt_id": single["attempt_id"] if single else None,
            "receipt_id": single["receipt_id"] if single else None,
            "unit_id": single["unit_id"] if single else None,
            "producers": producers,
            "validators": validator_records,
            "artifacts": {
                "manifest_digest": manifest.manifest_hash,
                "integrity_mode": manifest.integrity_mode,
                "file_count": len(entries),
                "entries": entries,
            },
            "sampling": sampling_record,
            "created_at": created_at,
            "unknown_facts": sorted(set(unknown)),
            # Said in the record itself, not only in the docs, because an
            # envelope travels further than its documentation does.
            "establishes": (
                "how these artifacts were produced and under whose enrolled "
                "identity. It does not establish that the output is correct, "
                "useful, or honest."
            ),
        }

    def record(
        self,
        execution_id: str,
        *,
        manifest: Any,
        validators: Sequence[Mapping[str, Any]] | None = None,
        created_at: float | None = None,
        sampling: Mapping[str, Any] | None = None,
    ) -> ProvenanceEnvelopeRecord:
        """Create the envelope for one execution, idempotently."""

        self.migrate()
        moment = time.time() if created_at is None else float(created_at)
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                existing = con.execute(
                    "SELECT * FROM provenance_envelopes WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()
                if existing is not None:
                    con.commit()
                    return _record_from_row(existing)

                payload = self.build_payload(
                    con,
                    execution_id,
                    manifest=manifest,
                    validators=validators,
                    created_at=moment,
                    sampling=sampling,
                )
                envelope_json = canonical_json(payload)
                if len(envelope_json.encode("utf-8")) > MAX_ENVELOPE_JSON_BYTES:
                    raise ValueError("provenance envelope exceeds its byte bound")
                digest = envelope_digest(payload)
                con.execute(
                    """
                    INSERT OR IGNORE INTO provenance_envelopes (
                        execution_id, envelope_version, envelope_digest,
                        envelope_json, manifest_digest, signature,
                        signature_algorithm, created_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)
                    """,
                    (
                        execution_id,
                        PROVENANCE_ENVELOPE_VERSION,
                        digest,
                        envelope_json,
                        manifest.manifest_hash,
                        moment,
                    ),
                )
                row = con.execute(
                    "SELECT * FROM provenance_envelopes WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()
                con.commit()
                if row is None:  # pragma: no cover - SQLite insert contract
                    raise RuntimeError("provenance envelope disappeared after insertion")
                return _record_from_row(row)
            except Exception:
                if con.in_transaction:
                    con.rollback()
                raise

    def get(self, execution_id: str) -> ProvenanceEnvelopeRecord | None:
        self.migrate()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            row = con.execute(
                "SELECT * FROM provenance_envelopes WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        return None if row is None else _record_from_row(row)

    def count(self) -> int:
        self.migrate()
        with self._lock, connection(self.path, row_factory=sqlite3.Row) as con:
            row = con.execute("SELECT COUNT(*) AS n FROM provenance_envelopes").fetchone()
        return int(row["n"]) if row else 0


def _record_from_row(row: sqlite3.Row) -> ProvenanceEnvelopeRecord:
    payload = json.loads(str(row["envelope_json"]))
    if not isinstance(payload, dict):
        raise RuntimeError("stored provenance envelope is not an object")
    return ProvenanceEnvelopeRecord(
        execution_id=str(row["execution_id"]),
        envelope_version=str(row["envelope_version"]),
        envelope_digest=str(row["envelope_digest"]),
        payload=payload,
        signature=_text(row["signature"]),
        signature_algorithm=_text(row["signature_algorithm"]),
        created_at=float(row["created_at"]),
    )


# ── offline checking ─────────────────────────────────────────────────


@dataclass(frozen=True)
class EnvelopeCheckResult:
    """What an offline check of a bundle can and cannot conclude."""

    digest_matches: bool
    files_checked: int
    files_matched: int
    mismatched_paths: tuple[str, ...]
    missing_paths: tuple[str, ...]

    @property
    def artifacts_match_envelope(self) -> bool:
        """Every file present matches the hash the envelope recorded.

        This says the bytes are the bytes that were sealed. It says nothing
        about whether those bytes are correct, and a recipient who concludes
        otherwise has been misled by something other than this field's name.
        """
        return (
            self.digest_matches
            and not self.mismatched_paths
            and not self.missing_paths
        )


def check_envelope_against_files(
    envelope: Mapping[str, Any], *, root: Path
) -> EnvelopeCheckResult:
    """Recompute the envelope digest and the per-file hashes, offline.

    Takes a directory of extracted files and the envelope that shipped with
    them. Needs no coordinator, no network, and no credential - which is the
    user-facing point of the envelope.
    """

    exported = dict(envelope)
    recorded_digest = exported.pop("envelope_digest", None)
    exported.pop("signature", None)
    exported.pop("signature_algorithm", None)
    digest_matches = bool(
        recorded_digest and envelope_digest(exported) == recorded_digest
    )

    entries = (exported.get("artifacts") or {}).get("entries") or []
    matched = 0
    mismatched: list[str] = []
    missing: list[str] = []
    for entry in entries:
        relative = str(entry.get("relative_path", ""))
        target = root / relative
        if not target.is_file():
            missing.append(relative)
            continue
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() == entry.get("sha256"):
            matched += 1
        else:
            mismatched.append(relative)
    return EnvelopeCheckResult(
        digest_matches=digest_matches,
        files_checked=len(entries),
        files_matched=matched,
        mismatched_paths=tuple(sorted(mismatched)),
        missing_paths=tuple(sorted(missing)),
    )
