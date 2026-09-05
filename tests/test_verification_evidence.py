"""Durable post-hoc verification evidence (ADR 0014).

The constraint that shapes every test here: ADR 0009 makes terminal execution
state monotonic and never reclassified, and post-hoc verification happens after
terminal. So evidence is a separate append-only record that references an
execution, attempt, and receipt without mutating any of them. If a verification
result could move a lifecycle state, a settlement, or a contribution, the design
would be wrong regardless of what these tests said.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

import server_state as state
import verification
from execution import dispatch
from execution.artifacts import ArtifactStore
from execution.contracts import ExecutionRequestV1
from execution.persistence import ExecutionStore
from execution.registry import StrategyRegistry
from execution.service import ExecutionService
from tests.deadline_guards import await_condition
from tests.protocol_harness import (
    CREDENTIALS,
    REQUESTER_HOSTS,
    TASK_TEXTS,
    CoordinatorHarness,
)
from verification_evidence import (
    STRUCTURAL_VALIDATOR_NAMES,
    VerificationEvidenceProcessCounters,
    VerificationEvidenceStore,
    evidence_id_for,
)


DESCRIPTOR_A = "a" * 64
DESCRIPTOR_B = "b" * 64
ENROLLMENT_A = "1" * 32
ENROLLMENT_B = "2" * 32
EXECUTION = "e" * 32
ATTEMPT = "c" * 32


@pytest.fixture
def store(tmp_path):
    evidence = VerificationEvidenceStore(Path(tmp_path) / "events.db")
    evidence.migrate()
    return evidence


@pytest.fixture
def harness(tmp_path):
    coordinator = CoordinatorHarness(Path(tmp_path) / "state")
    try:
        yield coordinator
    finally:
        coordinator.close()


def _check(**overrides):
    body = {
        "execution_id": EXECUTION,
        "unit_id": "candidate-1",
        "attempt_id": ATTEMPT,
        "receipt_id": ATTEMPT,
        "subject_enrollment_id": ENROLLMENT_A,
        "subject_node_id": "n0",
        "descriptor_hash": DESCRIPTOR_A,
        "task_class": "candidate",
        "verifier_kind": "deterministic_check",
        "verifier_name": "code_parse",
        "verifier_version": "1",
        "outcome": "passed",
        "fault_attribution": "subject_output",
    }
    body.update(overrides)
    return body


def _open_network(harness: CoordinatorHarness) -> str:
    assert harness.register("n0", CREDENTIALS[0], "bootstrap").status_code == 200
    submission = harness.submit_execution(
        host=REQUESTER_HOSTS[0], task=TASK_TEXTS[0], idempotency_key=None
    )
    assert submission.status_code == 202, submission.text
    return submission.json()["execution_id"]


def _settle_one_unit(harness: CoordinatorHarness, execution_id: str, task_id: str = "u0"):
    harness.enqueue_unit(task_id, execution_id=execution_id, unit_id=f"candidate-{task_id}")
    handout = harness.poll("n0")
    assert handout is not None
    response = harness.submit(task_id, harness.result_body(handout), label="n0")
    assert response.status_code == 200, response.text
    return handout


def _execution_row(harness: CoordinatorHarness, execution_id: str) -> tuple:
    row = harness.durable_execution(execution_id)
    assert row is not None
    return tuple(row)


# ── evidence is never authoritative over terminal state ──────────────


def test_evidence_never_mutates_the_execution_it_references(harness):
    execution_id = _open_network(harness)
    handout = _settle_one_unit(harness, execution_id)

    before_execution = _execution_row(harness, execution_id)
    before_attempts = harness.durable_attempts()
    before_receipts = harness.durable_receipts()
    before_credits = harness.durable_credits()

    for version, outcome in (("1", "passed"), ("2", "failed")):
        state.verification_evidence_store.record(
            **_check(
                execution_id=execution_id,
                attempt_id=handout.attempt_id,
                receipt_id=handout.attempt_id,
                subject_enrollment_id=state.enrollment_store.get_by_node("n0").enrollment_id,
                verifier_version=version,
                outcome=outcome,
            )
        )

    assert _execution_row(harness, execution_id) == before_execution, (
        "a verification result changed the terminal execution row"
    )
    assert harness.durable_attempts() == before_attempts
    assert harness.durable_receipts() == before_receipts
    assert harness.durable_credits() == before_credits
    assert len(harness.verification_evidence()) == 2


def test_a_failing_verification_leaves_lifecycle_and_assurance_alone(harness):
    execution_id = _open_network(harness)
    handout = _settle_one_unit(harness, execution_id)
    before = harness.get_execution(execution_id).json()

    state.verification_evidence_store.record(
        **_check(
            execution_id=execution_id,
            attempt_id=handout.attempt_id,
            receipt_id=handout.attempt_id,
            outcome="failed",
        )
    )

    after = harness.get_execution(execution_id).json()
    assert after["lifecycle_status"] == before["lifecycle_status"]
    assert after["validation_outcome"] == before["validation_outcome"]
    assert after["assurance_level"] == before["assurance_level"]
    assert after == before, "a failed verification changed the published result"


def test_contribution_points_are_unaffected_by_any_evidence_outcome(harness):
    execution_id = _open_network(harness)
    handout = _settle_one_unit(harness, execution_id)
    credits_before = {
        attempt_id: tuple(row) for attempt_id, row in harness.durable_credits().items()
    }

    for outcome in ("passed", "failed"):
        state.verification_evidence_store.record(
            **_check(
                execution_id=execution_id,
                attempt_id=handout.attempt_id,
                receipt_id=handout.attempt_id,
                verifier_version=f"v-{outcome}",
                outcome=outcome,
            )
        )

    credits_after = {
        attempt_id: tuple(row) for attempt_id, row in harness.durable_credits().items()
    }
    assert credits_after == credits_before, "evidence changed contribution accounting"


# ── replay safety ────────────────────────────────────────────────────


def test_identical_verifier_runs_produce_exactly_one_row(store):
    first = store.record(**_check())
    for _ in range(4):
        again = store.record(**_check())
        assert again.evidence_id == first.evidence_id
    assert store.count() == 1


def test_a_new_verifier_version_appends_without_overwriting(store):
    first = store.record(**_check(verifier_version="1", outcome="passed"))
    second = store.record(**_check(verifier_version="2", outcome="failed"))

    assert first.evidence_id != second.evidence_id
    assert store.count() == 2
    unchanged = store.get(first.evidence_id)
    assert unchanged is not None and unchanged.outcome == "passed", (
        "a re-run at a new version overwrote the original record"
    )


def test_evidence_survives_restart_and_reinitialization(harness):
    execution_id = _open_network(harness)
    handout = _settle_one_unit(harness, execution_id)
    record = state.verification_evidence_store.record(
        **_check(
            execution_id=execution_id,
            attempt_id=handout.attempt_id,
            receipt_id=handout.attempt_id,
        )
    )

    harness.restart()

    assert state.verification_evidence_store.get(record.evidence_id) is not None
    # And the same verifier re-run after the restart is still the same row.
    state.verification_evidence_store.record(
        **_check(
            execution_id=execution_id,
            attempt_id=handout.attempt_id,
            receipt_id=handout.attempt_id,
        )
    )
    assert len(harness.verification_evidence()) == 1


def test_the_append_only_table_refuses_updates_and_deletes(store):
    record = store.record(**_check())
    with sqlite3.connect(store.path) as con:
        for statement in (
            "UPDATE verification_evidence SET outcome = 'failed' WHERE evidence_id = ?",
            "DELETE FROM verification_evidence WHERE evidence_id = ?",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                con.execute(statement, (record.evidence_id,))


def test_schema_initialization_is_idempotent_on_fresh_and_existing_databases(tmp_path):
    database = Path(tmp_path) / "events.db"
    first = VerificationEvidenceStore(database)
    first.migrate()
    first.record(**_check())
    second = VerificationEvidenceStore(database)
    second.migrate()
    second.migrate()
    assert second.count() == 1


# ── fault attribution ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("attribution", "version"),
    [
        ("requester_cancelled", "v1"),
        ("coordinator_shutdown", "v2"),
        ("coordinator_persistence_failure", "v3"),
        ("pre_assignment_deadline", "v4"),
        ("verifier_unavailable", "v5"),
        ("unattributed", "v6"),
    ],
)
def test_a_non_subject_attribution_can_only_say_the_run_did_not_happen(
    store, attribution, version
):
    """None of these is a statement about the node's work, so none may claim one."""
    record = store.record(
        **_check(
            outcome="not_run",
            fault_attribution=attribution,
            verifier_version=version,
        )
    )
    assert record.outcome == "not_run"
    assert record.fault_attribution == attribution

    for forbidden in ("failed", "passed"):
        with pytest.raises(ValueError, match="only subject_output"):
            store.record(
                **_check(
                    outcome=forbidden,
                    fault_attribution=attribution,
                    verifier_version=f"{version}x",
                )
            )


def test_a_subject_outcome_cannot_be_recorded_as_a_non_result(store):
    with pytest.raises(ValueError, match="only subject_output"):
        store.record(**_check(outcome="not_run", fault_attribution="subject_output"))


@pytest.mark.parametrize(
    "attribution",
    ["malformed_authority_credentials", "authority_mismatch", "unauthenticated"],
)
def test_an_authority_failure_is_not_a_verification_outcome(store, attribution):
    """Attempt authority handles these. Recording one here would turn an
    authentication rejection into evidence against a node."""
    with pytest.raises(ValueError, match="security events"):
        store.record(**_check(outcome="not_run", fault_attribution=attribution))


def test_a_structural_validator_is_not_recordable_as_post_hoc_verification(store):
    for name in sorted(STRUCTURAL_VALIDATOR_NAMES):
        with pytest.raises(ValueError, match="structural"):
            store.record(**_check(verifier_name=name, outcome="failed"))


def test_outcome_vocabularies_do_not_cross_verifier_kinds(store):
    with pytest.raises(ValueError, match="vocabulary"):
        store.record(**_check(verifier_kind="sampled_reexecution", outcome="passed"))
    with pytest.raises(ValueError, match="vocabulary"):
        store.record(**_check(verifier_kind="deterministic_check", outcome="agreed"))


# ── scoping ──────────────────────────────────────────────────────────


def _summary_for(store, **query):
    return {
        (
            summary.scope.verifier_kind,
            summary.scope.verifier_name,
            summary.scope.descriptor_hash,
            summary.scope.model_name,
            summary.scope.task_class,
        ): summary
        for summary in store.list_scope_summaries(**query)
    }


def test_agreement_and_deterministic_checks_never_share_a_scope(store):
    store.record(**_check(outcome="passed"))
    store.record(
        **_check(
            verifier_kind="sampled_reexecution",
            verifier_name="output_shape",
            outcome="disagreed",
        )
    )

    summaries = _summary_for(store)
    assert len(summaries) == 2, "agreement and deterministic evidence were merged"
    kinds = {key[0] for key in summaries}
    assert kinds == {"deterministic_check", "sampled_reexecution"}
    for key, summary in summaries.items():
        counts = summary.outcome_counts.as_dict()
        if key[0] == "deterministic_check":
            assert counts["passed"] == 1 and counts["agreed"] == 0
        else:
            assert counts["disagreed"] == 1 and counts["failed"] == 0


def test_a_descriptor_change_starts_a_cold_scope(store):
    store.record(**_check(descriptor_hash=DESCRIPTOR_A, outcome="passed"))
    store.record(
        **_check(descriptor_hash=DESCRIPTOR_B, outcome="failed", attempt_id="d" * 32)
    )

    summaries = _summary_for(store)
    assert len(summaries) == 2, "history was inherited across a descriptor change"
    for summary in summaries.values():
        assert summary.observed.sample_count == 1


def test_a_model_change_starts_a_cold_scope(store):
    store.record(**_check(model_name="qwen3.5:4b", outcome="passed"))
    store.record(
        **_check(model_name="gemma3:4b", outcome="failed", attempt_id="d" * 32)
    )

    summaries = _summary_for(store)
    assert len(summaries) == 2, "history was inherited across a model change"


def test_task_classes_do_not_contaminate_one_another(store):
    store.record(**_check(task_class="candidate", outcome="passed"))
    store.record(
        **_check(task_class="dag_subtask", outcome="failed", attempt_id="d" * 32)
    )

    candidate = store.list_scope_summaries(task_class="candidate")
    subtask = store.list_scope_summaries(task_class="dag_subtask")
    assert len(candidate) == 1 and len(subtask) == 1
    assert candidate[0].outcome_counts.passed == 1
    assert candidate[0].outcome_counts.failed == 0
    assert subtask[0].outcome_counts.failed == 1
    assert subtask[0].outcome_counts.passed == 0


def test_one_enrollments_evidence_is_not_visible_in_anothers_scope(store):
    store.record(**_check(subject_enrollment_id=ENROLLMENT_A, outcome="passed"))
    store.record(
        **_check(
            subject_enrollment_id=ENROLLMENT_B, outcome="failed", attempt_id="d" * 32
        )
    )

    first = store.list_scope_summaries(subject_enrollment_id=ENROLLMENT_A)
    assert len(first) == 1
    assert first[0].outcome_counts.failed == 0


def test_rows_without_enrolled_identity_are_legacy_and_never_guessed_at(store):
    store.record(**_check(subject_enrollment_id=None, subject_node_id="n0"))

    assert store.legacy_count() == 1
    summaries = store.list_scope_summaries()
    assert len(summaries) == 1
    assert summaries[0].scope.identity_class == "legacy"
    assert summaries[0].scope.subject_enrollment_id is None, (
        "a legacy row inferred an enrollment from a reusable node label"
    )


def test_a_low_sample_scope_reports_insufficient_evidence_not_a_bad_rate(store):
    store.record(**_check(outcome="failed"))

    summary = store.list_scope_summaries(minimum_samples=5)[0]
    assert summary.insufficient_evidence is True
    assert summary.observed.sample_count == 1
    assert summary.minimum_samples == 5
    # The rate is still reported, but never without its sample count beside it.
    assert summary.observed.rate == 0.0
    assert summary.observed.wilson_low is not None


def test_non_result_records_are_excluded_from_the_attributable_sample(store):
    store.record(**_check(outcome="passed"))
    store.record(
        **_check(
            outcome="not_run",
            fault_attribution="requester_cancelled",
            verifier_version="2",
        )
    )

    by_version = {
        summary.scope.verifier_version: summary
        for summary in store.list_scope_summaries()
    }
    assert by_version["1"].observed.sample_count == 1
    assert by_version["2"].observed.sample_count == 0, (
        "a cancelled run was counted as a sample about the node"
    )
    assert by_version["2"].observed.rate is None


# ── containment ──────────────────────────────────────────────────────


def test_a_store_failure_increments_only_the_fallback_counter(harness, monkeypatch):
    execution_id = _open_network(harness)
    handout = _settle_one_unit(harness, execution_id)

    receipts_before = harness.durable_receipts()
    credits_before = harness.durable_credits()
    queue_before = list(state.task_queue)

    def explode(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(state.verification_evidence_store, "record", explode)
    state.verification_evidence_counters.reset()

    attempt = state.attempt_store.get(handout.attempt_id)
    dispatch._record_agreement_evidence(
        execution_id=execution_id,
        unit_id="candidate-u0",
        agreed=True,
        subjects=(attempt, attempt),
    )

    counters = state.verification_evidence_counters.snapshot()
    assert counters["record_failed"] == 2, counters
    assert harness.durable_receipts() == receipts_before, "settlement changed"
    assert harness.durable_credits() == credits_before, "credit changed"
    assert list(state.task_queue) == queue_before, "the queue changed"
    assert harness.verification_evidence() == []


def test_best_effort_reports_failure_without_raising(store, monkeypatch):
    def explode(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    result = store.best_effort(explode)
    assert result.succeeded is False
    assert result.error_code == "verification_evidence_write_failed"


def test_process_counters_reject_unknown_names():
    counters = VerificationEvidenceProcessCounters()
    with pytest.raises(ValueError):
        counters.increment("not_a_counter")
    assert set(counters.snapshot()) == {"record_failed", "scope_unresolved", "read_failed"}


# ── defaults are unchanged by this PR ────────────────────────────────


def test_verify_rate_remains_off_by_default(monkeypatch):
    import config

    assert float(config.DEFAULTS["verify_rate"]) == 0.0
    settings = dict(config.DEFAULTS)
    monkeypatch.setattr(state, "get_config", lambda: settings)
    assert state._refresh_verify_rate() == 0.0


def test_trusted_alpha_still_disables_sampled_verification(monkeypatch):
    """Durability now exists. Whether to switch this on is a separate decision."""
    import config

    settings = dict(config.DEFAULTS)
    settings.update({"deployment_mode": "trusted_alpha", "verify_rate": 1.0})
    monkeypatch.setattr(state, "get_config", lambda: settings)

    assert state._refresh_verify_rate() == 0.0
    assert state.verification_pool.verify_rate == 0.0
    assert verification.VerificationPool(verify_rate=0.0).should_verify(10) is False


# ── secret hygiene and naming ────────────────────────────────────────


def test_no_content_or_identity_material_can_enter_the_evidence_store(store):
    for forbidden in (
        {"prompt": "synthetic prompt"},
        {"output": "synthetic output"},
        {"json_schema": "{}"},
        {"enrollment_credential": "credential"},
        {"session_token": "token"},
        {"nonce": "nonce"},
        {"error": "worker error text"},
    ):
        with pytest.raises(ValueError, match="not allowed"):
            store.record(**_check(metadata=forbidden))


def test_stored_rows_and_summaries_carry_no_free_text(store):
    store.record(**_check(metadata={"check_name": "code_parse"}))
    with sqlite3.connect(store.path) as con:
        blob = "\n".join(
            " ".join(str(value) for value in tuple(row))
            for row in con.execute("SELECT * FROM verification_evidence")
        )
    for secret in ("synthetic prompt", "synthetic output", "credential", "token"):
        assert secret not in blob


def test_nothing_in_the_evidence_model_is_named_as_correctness_or_reputation(store):
    store.record(**_check())
    summary = store.list_scope_summaries()[0]
    surface = {
        *summary.outcome_counts.as_dict(),
        *summary.attribution_counts,
        *vars(summary.scope),
        "observed_affirmative",
        "insufficient_evidence",
    }
    for forbidden in ("correct", "reputation", "score", "rank", "trust", "quality"):
        assert not any(forbidden in name for name in surface), (
            f"the evidence surface names something {forbidden!r}"
        )


def test_the_protected_route_declares_what_it_is_not(harness):
    execution_id = _open_network(harness)
    handout = _settle_one_unit(harness, execution_id)
    state.verification_evidence_store.record(
        **_check(
            execution_id=execution_id,
            attempt_id=handout.attempt_id,
            receipt_id=handout.attempt_id,
        )
    )

    response = harness.client.get("/v1/operator/verification-evidence")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 1
    semantics = body["semantics"]
    assert semantics["is_reputation"] is False
    assert semantics["is_correctness"] is False
    assert semantics["influences_routing"] is False
    assert semantics["influences_settlement"] is False
    assert semantics["influences_contribution_points"] is False
    assert "legacy_row_count" in body
    scope = body["scopes"][0]
    assert scope["observed_affirmative"]["sample_count"] == 1
    assert scope["insufficient_evidence"] is True
    # The `semantics` block exists to say what this is *not*, so it is the one
    # place those words may appear. Nothing describing an actual scope may.
    rendered_scopes = json.dumps(body["scopes"])
    for forbidden in ("score", "rank", "reputation", "leaderboard", "correct"):
        assert forbidden not in rendered_scopes


def test_the_protected_route_is_not_public(harness):
    """Configure a viewer key and the operator surface stops answering.

    The settings dict is mutated in place rather than monkeypatched. `harness`
    already patches `state.get_config` and `access_control.get_config` to return
    this dict, and `monkeypatch` is set up before it (the autouse `isolated_cwd`
    fixture depends on it), so monkeypatch finalizes *last* and would reinstall
    the harness's lambda over the harness's own restore - leaking the campaign's
    `node_secret` into every later test in the session.
    """
    harness.settings["viewer_key"] = "a-configured-viewer-key-long-enough-to-pass"

    response = harness.client.get("/v1/operator/verification-evidence")
    assert response.status_code in (401, 403), response.text


# ── deterministic identity ───────────────────────────────────────────


def test_the_evidence_id_is_a_pure_function_of_authoritative_identity():
    base = {
        "execution_id": EXECUTION,
        "unit_id": "candidate-1",
        "attempt_id": ATTEMPT,
        "receipt_id": ATTEMPT,
        "subject_enrollment_id": ENROLLMENT_A,
        "verifier_kind": "deterministic_check",
        "verifier_name": "code_parse",
        "verifier_version": "1",
        "subject_key": "default",
    }
    assert evidence_id_for(**base) == evidence_id_for(**base)
    for field, value in (
        ("attempt_id", "d" * 32),
        ("subject_enrollment_id", ENROLLMENT_B),
        ("verifier_version", "2"),
        ("verifier_name", "structured_json"),
        ("subject_key", "other"),
    ):
        assert evidence_id_for(**{**base, field: value}) != evidence_id_for(**base), (
            f"{field} does not participate in evidence identity"
        )


# ── cancellation, end to end, against a genuinely running execution ──


class _ParkedStrategy:
    """Stays running until cancelled, so `cancelled` is a real terminal state."""

    identifier = "dag"
    version = "verification-evidence-test"

    async def execute(self, request, options, context):  # pragma: no cover - cancelled
        await asyncio.sleep(30)
        raise AssertionError("the parked strategy was never cancelled")


@pytest.mark.asyncio
async def test_cancelling_a_running_execution_is_never_the_subjects_fault(tmp_path):
    """The rule Part 5 exists for, exercised against a real terminal `cancelled`.

    The campaign cannot reach this state - a background execution task does not
    outlive the TestClient request that created it - so it is covered here, with
    the async service driven directly.
    """
    database = Path(tmp_path) / "events.db"
    registry = StrategyRegistry()
    registry.register(_ParkedStrategy())
    service = ExecutionService(
        store=ExecutionStore(database),
        registry=registry,
        artifacts=ArtifactStore(database, allowed_roots=[Path(tmp_path)]),
    )
    service.store.migrate()
    service.artifacts.migrate()
    service._emit = lambda *args, **kwargs: None

    queued = service.submit(ExecutionRequestV1(task="Park until cancelled", strategy="dag"))
    await await_condition(
        lambda: service.get(queued.execution_id).lifecycle_status == "running",
        what="the execution to reach 'running' before cancelling it",
    )
    assert service.get(queued.execution_id).lifecycle_status == "running"

    cancelled = await service.cancel(queued.execution_id, "requester changed their mind")
    assert cancelled.lifecycle_status == "cancelled"
    before = service.store.get(queued.execution_id).model_dump(mode="json")

    evidence = VerificationEvidenceStore(database)
    # A cancellation is recordable, but only as "no evidence about the subject".
    record = evidence.record(
        execution_id=queued.execution_id,
        task_class="candidate",
        verifier_kind="deterministic_check",
        verifier_name="code_parse",
        verifier_version="1",
        outcome="not_run",
        fault_attribution="requester_cancelled",
        subject_enrollment_id=ENROLLMENT_A,
        descriptor_hash=DESCRIPTOR_A,
    )
    assert record.outcome == "not_run"

    # And it can never become a statement about the node that held the work.
    with pytest.raises(ValueError, match="only subject_output"):
        evidence.record(
            execution_id=queued.execution_id,
            task_class="candidate",
            verifier_kind="deterministic_check",
            verifier_name="code_parse",
            verifier_version="2",
            outcome="failed",
            fault_attribution="requester_cancelled",
            subject_enrollment_id=ENROLLMENT_A,
            descriptor_hash=DESCRIPTOR_A,
        )

    after = service.store.get(queued.execution_id).model_dump(mode="json")
    assert after == before, "recording evidence changed a terminal cancelled execution"
    assert after["lifecycle_status"] == "cancelled"
    summary = evidence.list_scope_summaries()[0]
    assert summary.observed.sample_count == 0, (
        "a cancellation was counted as a sample about the node"
    )
    assert summary.attribution_counts == {"requester_cancelled": 1}
