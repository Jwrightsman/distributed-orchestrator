"""Provenance envelopes (ADR 0017).

An envelope binds an artifact set to the identity chain that produced it. It
establishes nothing about correctness, and these tests are as concerned with
what it must not claim as with what it records.

The constraint that shapes all of it: ADR 0009 makes terminal execution state
monotonic, and the envelope is created at seal time. So it references the
execution and can never write to it.
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

import sampling
import server_state as state
from execution.artifacts import (
    PROVENANCE_BUNDLE_FILENAME,
    ArtifactEntryV1,
    ArtifactManifestV1,
    ArtifactStore,
)
from execution.contracts import ExecutionRequestV1
from execution.persistence import ExecutionStore
from execution.registry import StrategyOutcome, StrategyRegistry
from execution.service import ExecutionService
from provenance import (
    PROVENANCE_ENVELOPE_VERSION,
    UNKNOWN_MODEL_DIGEST,
    UNKNOWN_PRODUCER_IDENTITY,
    UNKNOWN_PRODUCER_SAMPLING,
    UNKNOWN_SAMPLING,
    UNKNOWN_SEED_HONOURED,
    ProvenanceEnvelopeStore,
    canonical_json,
    check_envelope_against_files,
    envelope_digest,
)


EXECUTION = "e" * 32


def _manifest(execution_id=EXECUTION, *, sha="b" * 64, path="deliverable.txt"):
    return ArtifactManifestV1(
        execution_id=execution_id,
        created_at="2026-09-03T00:00:00Z",
        file_count=1,
        aggregate_size_bytes=3,
        integrity_mode="sealed",
        manifest_hash="a" * 64,
        sealed_at="2026-09-03T00:00:00Z",
        entries=[
            ArtifactEntryV1(
                relative_path=path,
                media_type="text/plain",
                size_bytes=3,
                sha256=sha,
                created_at="2026-09-03T00:00:00Z",
            )
        ],
    )


@pytest.fixture
def store(tmp_path):
    envelope_store = ProvenanceEnvelopeStore(Path(tmp_path) / "events.db")
    envelope_store.migrate()
    return envelope_store


class ArtifactStrategy:
    identifier = "dag"
    version = "provenance-test"

    def __init__(self, root: Path):
        self.root = root

    async def execute(self, request, options, context) -> StrategyOutcome:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "deliverable.txt").write_text("hey", encoding="utf-8")
        context.artifact_root_path = self.root
        context.artifacts.register_root(
            context.execution_id, self.root, strategy=self.identifier, active=True
        )
        return StrategyOutcome(
            status="completed",
            validation_outcome="passed",
            assurance_level="structural",
            output_preview="hey",
        )


def _service(tmp_path) -> ExecutionService:
    database = Path(tmp_path) / "events.db"
    registry = StrategyRegistry()
    registry.register(ArtifactStrategy(Path(tmp_path) / "artifacts"))
    service = ExecutionService(
        store=ExecutionStore(database),
        registry=registry,
        artifacts=ArtifactStore(database, allowed_roots=[Path(tmp_path)]),
    )
    service.store.migrate()
    service.artifacts.migrate()
    service._emit = lambda *args, **kwargs: None
    return service


# ── created at seal time, and never authoritative ────────────────────


@pytest.mark.asyncio
async def test_an_envelope_is_created_when_the_manifest_seals(tmp_path, monkeypatch):
    database = Path(tmp_path) / "events.db"
    envelopes = ProvenanceEnvelopeStore(database)
    monkeypatch.setattr(state, "provenance_envelope_store", envelopes)
    service = _service(tmp_path)

    queued = service.submit(ExecutionRequestV1(task="Produce a file", strategy="dag"))
    await _drain(service, queued.execution_id)

    record = envelopes.get(queued.execution_id)
    assert record is not None, "sealing produced no envelope"
    assert record.envelope_version == PROVENANCE_ENVELOPE_VERSION
    assert record.payload["artifacts"]["manifest_digest"]
    assert record.payload["artifacts"]["entries"][0]["relative_path"] == "deliverable.txt"
    assert "correct" not in record.payload["establishes"].split("does not establish")[0]


@pytest.mark.asyncio
async def test_recording_an_envelope_changes_nothing_terminal(tmp_path, monkeypatch):
    database = Path(tmp_path) / "events.db"
    envelopes = ProvenanceEnvelopeStore(database)
    monkeypatch.setattr(state, "provenance_envelope_store", envelopes)
    service = _service(tmp_path)
    queued = service.submit(ExecutionRequestV1(task="Produce a file", strategy="dag"))
    await _drain(service, queued.execution_id)

    before = service.store.get(queued.execution_id).model_dump(mode="json")
    with sqlite3.connect(database) as con:
        sealed_before = con.execute(
            "SELECT manifest_state, manifest_hash, sealed_at FROM artifact_roots "
            "WHERE execution_id = ?",
            (queued.execution_id,),
        ).fetchone()

    envelopes.record(queued.execution_id, manifest=_manifest(queued.execution_id))
    envelopes.record(queued.execution_id, manifest=_manifest(queued.execution_id))

    after = service.store.get(queued.execution_id).model_dump(mode="json")
    assert after == before, "an envelope write changed terminal execution state"
    assert after["lifecycle_status"] == "completed"
    with sqlite3.connect(database) as con:
        assert (
            con.execute(
                "SELECT manifest_state, manifest_hash, sealed_at FROM artifact_roots "
                "WHERE execution_id = ?",
                (queued.execution_id,),
            ).fetchone()
            == sealed_before
        ), "an envelope write changed the artifact seal"


@pytest.mark.asyncio
async def test_a_provenance_failure_never_fails_the_execution(tmp_path, monkeypatch):
    class Exploding:
        def record(self, *args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(state, "provenance_envelope_store", Exploding())
    service = _service(tmp_path)

    queued = service.submit(ExecutionRequestV1(task="Produce a file", strategy="dag"))
    await _drain(service, queued.execution_id)

    result = service.store.get(queued.execution_id)
    assert result.lifecycle_status == "completed", (
        "a provenance failure reached the terminal path"
    )
    assert result.sealed_manifest_hash


async def _drain(service, execution_id, attempts: int = 400) -> None:
    import asyncio

    for _ in range(attempts):
        if service.get(execution_id).lifecycle_status in (
            "completed",
            "failed",
            "cancelled",
            "interrupted",
        ):
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the execution never reached a terminal state")


# ── replay ───────────────────────────────────────────────────────────


def test_replay_resolves_to_the_identical_envelope(store):
    first = store.record(EXECUTION, manifest=_manifest(), created_at=1000.0)
    for moment in (2000.0, 3000.0):
        again = store.record(EXECUTION, manifest=_manifest(), created_at=moment)
        assert again.envelope_digest == first.envelope_digest
        assert again.created_at == first.created_at
    assert store.count() == 1


def test_envelopes_are_append_only(store):
    store.record(EXECUTION, manifest=_manifest())
    with sqlite3.connect(store.path) as con:
        for statement in (
            "UPDATE provenance_envelopes SET envelope_digest = 'x' WHERE execution_id = ?",
            "DELETE FROM provenance_envelopes WHERE execution_id = ?",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                con.execute(statement, (EXECUTION,))


# ── deterministic digest ─────────────────────────────────────────────


def test_the_digest_ignores_key_order_and_whitespace():
    assert envelope_digest({"a": 1, "b": {"c": 2, "d": 3}}) == envelope_digest(
        {"b": {"d": 3, "c": 2}, "a": 1}
    )
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


@pytest.mark.parametrize(
    "mutation",
    [
        {"execution_id": "f" * 32},
        {"attempt_id": "changed"},
        {"validators": [{"name": "code_parse", "version": "2", "outcome": "passed"}]},
        {"artifacts": {"manifest_digest": "c" * 64, "entries": []}},
        {"unknown_facts": ["model_digest"]},
    ],
)
def test_any_material_production_fact_changes_the_digest(mutation):
    base = {
        "execution_id": EXECUTION,
        "attempt_id": "a" * 32,
        "validators": [{"name": "code_parse", "version": "1", "outcome": "passed"}],
        "artifacts": {"manifest_digest": "a" * 64, "entries": []},
        "unknown_facts": [],
    }
    assert envelope_digest({**base, **mutation}) != envelope_digest(base)


def test_the_digest_excludes_the_reserved_signature_slot(store):
    record = store.record(EXECUTION, manifest=_manifest())
    exported = record.as_export()

    assert "signature" in exported and exported["signature"] is None
    assert "signature_algorithm" in exported and exported["signature_algorithm"] is None
    assert record.is_signed is False
    # A signature is over the digest, so the digest cannot include it.
    assert "signature" not in record.payload
    assert envelope_digest(record.payload) == record.envelope_digest


# ── absence is explicit ──────────────────────────────────────────────


def test_an_execution_with_no_distributed_producer_records_that_as_unknown(store):
    record = store.record(EXECUTION, manifest=_manifest())

    assert record.payload["producers"] == []
    assert UNKNOWN_PRODUCER_IDENTITY in record.payload["unknown_facts"]
    assert record.payload["attempt_id"] is None
    assert record.payload["receipt_id"] is None


def test_a_missing_model_digest_is_unknown_and_never_inferred(tmp_path):
    database = Path(tmp_path) / "events.db"
    _seed_receipt(database, model_digest=None)
    store = ProvenanceEnvelopeStore(database)

    record = store.record(EXECUTION, manifest=_manifest())
    producer = record.payload["producers"][0]

    assert producer["model"]["digest"] is None
    assert UNKNOWN_MODEL_DIGEST in producer["unknown_facts"]
    assert producer["model"]["name"] == "qwen3.5:4b", (
        "the model name should still be recorded; only the digest is absent"
    )


def test_a_legacy_producer_is_classified_not_backfilled(tmp_path):
    database = Path(tmp_path) / "events.db"
    _seed_receipt(database, enrollment_id=None)
    store = ProvenanceEnvelopeStore(database)

    producer = store.record(EXECUTION, manifest=_manifest()).payload["producers"][0]

    assert producer["identity"]["identity_class"] == "legacy"
    assert producer["identity"]["enrollment_id"] is None, (
        "a legacy producer's enrollment was inferred from its node label"
    )
    assert producer["identity"]["node_id"] == "n0"
    assert "enrollment_id" in producer["unknown_facts"]


def test_a_pre_envelope_execution_simply_has_none(store):
    assert store.get("never" + "0" * 27) is None


def _seed_receipt(database: Path, *, enrollment_id="1" * 32, model_digest="sha256:" + "c" * 64):
    """One accepted receipt, written directly, without a coordinator."""
    from execution.attempts import AttemptStore

    AttemptStore(database).migrate()
    with sqlite3.connect(database) as con:
        con.execute(
            """
            INSERT INTO accepted_result_receipts (
                attempt_id, task_id, execution_id, execution_unit_id,
                execution_unit_kind, assigned_node_id, assigned_enrollment_id,
                assigned_descriptor_version, assigned_descriptor_hash,
                assigned_model_provider, assigned_model_name, assigned_model_digest,
                evidence_role, requirement_version, requirement_digest,
                contract_version, result_hash, accepted_at, output, error,
                elapsed_seconds, terminal_cause
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "a" * 32, "u0", EXECUTION, "candidate-u0", "candidate", "n0",
                enrollment_id, "1", "d" * 64, "ollama", "qwen3.5:4b", model_digest,
                "production", "1", "e" * 64, "1", "f" * 64, 1000.0, "out", None,
                1.0, "settled_output",
            ),
        )
        con.commit()


# ── the bundle, checked offline ──────────────────────────────────────


@pytest.mark.asyncio
async def test_the_audit_bundle_carries_an_envelope_checkable_offline(tmp_path, monkeypatch):
    database = Path(tmp_path) / "events.db"
    envelopes = ProvenanceEnvelopeStore(database)
    monkeypatch.setattr(state, "provenance_envelope_store", envelopes)
    service = _service(tmp_path)
    queued = service.submit(ExecutionRequestV1(task="Produce a file", strategy="dag"))
    await _drain(service, queued.execution_id)

    prepared = service.artifacts.prepare_archive(queued.execution_id)
    extracted = Path(tmp_path) / "extracted"
    with zipfile.ZipFile(prepared.path) as archive:
        names = set(archive.namelist())
        assert PROVENANCE_BUNDLE_FILENAME in names, "the bundle carries no envelope"
        archive.extractall(extracted)

    envelope = json.loads((extracted / PROVENANCE_BUNDLE_FILENAME).read_text("utf-8"))
    result = check_envelope_against_files(envelope, root=extracted)

    assert result.digest_matches, "the shipped envelope does not match its own digest"
    assert result.artifacts_match_envelope
    assert result.files_checked == result.files_matched == 1
    assert envelope["signature"] is None


@pytest.mark.asyncio
async def test_a_tampered_artifact_fails_the_offline_check(tmp_path, monkeypatch):
    database = Path(tmp_path) / "events.db"
    monkeypatch.setattr(state, "provenance_envelope_store", ProvenanceEnvelopeStore(database))
    service = _service(tmp_path)
    queued = service.submit(ExecutionRequestV1(task="Produce a file", strategy="dag"))
    await _drain(service, queued.execution_id)

    prepared = service.artifacts.prepare_archive(queued.execution_id)
    extracted = Path(tmp_path) / "extracted"
    with zipfile.ZipFile(prepared.path) as archive:
        archive.extractall(extracted)
    (extracted / "deliverable.txt").write_text("tampered", encoding="utf-8")

    envelope = json.loads((extracted / PROVENANCE_BUNDLE_FILENAME).read_text("utf-8"))
    result = check_envelope_against_files(envelope, root=extracted)

    assert result.artifacts_match_envelope is False
    assert result.mismatched_paths == ("deliverable.txt",)
    assert result.digest_matches, (
        "the envelope itself was untouched; only the artifact changed"
    )


def test_an_edited_envelope_fails_its_own_digest_check(store, tmp_path):
    record = store.record(EXECUTION, manifest=_manifest())
    envelope = record.as_export()
    envelope["execution_id"] = "f" * 32

    result = check_envelope_against_files(envelope, root=Path(tmp_path))
    assert result.digest_matches is False


# ── what it must not claim ───────────────────────────────────────────


def test_no_field_or_statement_claims_correctness_or_proof(store):
    record = store.record(EXECUTION, manifest=_manifest())
    rendered = json.dumps(record.as_export())

    for forbidden in ("verified", "tamper_proof", "tamperproof", "trustless", "proof_of"):
        assert forbidden not in rendered.lower(), f"the envelope says {forbidden!r}"
    establishes = record.payload["establishes"]
    assert "does not establish that the output is correct" in establishes


# ── the sampling block, scoped rather than asserted ──────────────────


@pytest.mark.asyncio
async def test_the_envelope_records_the_sampling_it_was_configured_with(
    tmp_path, monkeypatch
):
    """A configured temperature and seed reach the envelope, scoped as the
    coordinator's own configuration and never as the producer's."""
    database = Path(tmp_path) / "events.db"
    envelopes = ProvenanceEnvelopeStore(database)
    monkeypatch.setattr(state, "provenance_envelope_store", envelopes)
    monkeypatch.setattr(
        sampling,
        "from_config",
        lambda settings=None: sampling.Sampling(
            temperature=0.0, seed=42, seed_honouring=sampling.SEED_HONOURING_ASSUMED
        ),
    )
    service = _service(tmp_path)

    queued = service.submit(ExecutionRequestV1(task="Produce a file", strategy="dag"))
    await _drain(service, queued.execution_id)

    record = envelopes.get(queued.execution_id)
    assert record is not None, "sealing produced no envelope"
    block = record.payload["sampling"]
    assert block["temperature"] == 0.0
    assert block["seed"] == 42
    # Set is not honoured, so the envelope does not call it pinned.
    assert block["pinned"] is False
    assert UNKNOWN_SEED_HONOURED in record.payload["unknown_facts"]
    assert "coordinator configuration" in block["scope"]


@pytest.mark.asyncio
async def test_an_unconfigured_generator_is_recorded_as_unknown(tmp_path, monkeypatch):
    """The shipping default. Ollama's own 0.8 and 0 are not written in."""
    database = Path(tmp_path) / "events.db"
    envelopes = ProvenanceEnvelopeStore(database)
    monkeypatch.setattr(state, "provenance_envelope_store", envelopes)
    service = _service(tmp_path)

    queued = service.submit(ExecutionRequestV1(task="Produce a file", strategy="dag"))
    await _drain(service, queued.execution_id)

    record = envelopes.get(queued.execution_id)
    assert record is not None, "sealing produced no envelope"
    block = record.payload["sampling"]
    assert block["temperature"] is None and block["seed"] is None
    assert block["pinned"] is False
    assert UNKNOWN_SAMPLING in record.payload["unknown_facts"]


def test_a_distributed_producers_sampling_is_never_inferred(tmp_path):
    """The coordinator knows its own configuration and nothing about a worker's.

    Same rule as `model_variant`: the receipt does not carry it, so it is
    unknown rather than filled in with the coordinator's value. A locally
    executed execution has no producer and so no such gap.
    """
    store = ProvenanceEnvelopeStore(Path(tmp_path) / "events.db")
    store.migrate()
    pinned = sampling.Sampling(
        temperature=0.0, seed=1, seed_honouring=sampling.SEED_HONOURING_VERIFIED
    ).as_record()

    local, unknown_local = store._sampling(pinned, [])
    assert local["pinned"] is True
    assert unknown_local == []

    distributed, unknown_distributed = store._sampling(
        pinned, [{"attempt_id": "a" * 32}]
    )
    assert distributed["pinned"] is True
    assert UNKNOWN_PRODUCER_SAMPLING in unknown_distributed


def test_the_sampling_block_does_not_break_an_older_envelopes_digest(tmp_path):
    """Additive within version 1, on ADR 0017's own reasoning.

    An envelope sealed before this field existed keeps its stored payload, and
    its digest still verifies — the digest is recomputed over what was stored,
    not over what the current code would build.
    """
    legacy = {
        "envelope_version": "1",
        "execution_id": EXECUTION,
        "producers": [],
        "unknown_facts": ["producer_identity"],
    }
    digest = envelope_digest(legacy)
    assert "sampling" not in legacy
    assert envelope_digest(json.loads(canonical_json(legacy))) == digest
