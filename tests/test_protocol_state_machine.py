"""Property-based adversarial campaign over the protocol state machines.

Generated operation sequences drive the real coordinator — its worker routes,
its canonical execution router, its durable stores, its janitor — and every
sequence is checked against `tests/protocol_model.py` and against a fixed set of
global invariants that must hold no matter what order the operations arrive in,
which of them fail, and how often the coordinator restarts underneath them.

Two profiles:

* CI (default) is derandomized, bounded, and has no example database, so a red
  build is reproducible from the source tree alone.
* `MYCELIUM_CAMPAIGN_PROFILE=extended` explores far more sequences locally.

One deliberate asymmetry: when an injected persistence fault fires, the model
stops *predicting* that operation's outcome — it describes fault-free semantics —
but it still *learns* what the coordinator actually did, from the coordinator's
own response and durable state. The global invariants below are asserted after
every step regardless, and those are where the value is.

Findings, classifications, and the scenarios this does *not* cover are recorded
in `docs/adversarial-campaign.md`.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

import server_state as state
from tests.protocol_harness import (
    CREDENTIALS,
    IDEMPOTENCY_KEYS,
    NODE_LABELS,
    REQUESTER_HOSTS,
    TASK_TEXTS,
    WORKER_OUTPUTS,
    CoordinatorHarness,
    payload_digest,
)
from tests.protocol_model import (
    TERMINAL_LIFECYCLES,
    Bootstrap,
    ModelAttempt,
    ModelSubmissionMapping,
    ProtocolModel,
    Settlement,
    Submission,
)


CAMPAIGN_PROFILE = os.environ.get("MYCELIUM_CAMPAIGN_PROFILE", "ci")

# Leases are short so a bounded clock advance can expire one. The advance is in
# turn capped below `_NODE_TIMEOUT`, because past that the janitor also reclaims
# every node as stale — a real behaviour, covered explicitly and deterministically
# in tests/test_adversarial_scenarios.py rather than folded into the model.
UNIT_LEASE_SECONDS = 20
UNIT_OUTPUT_CAP = 4096
MAX_CLOCK_OFFSET = 80.0

LIFECYCLE_ORDER = {"queued": 0, "running": 1}

SUBMISSION_MUTATIONS = (
    "correct",
    "replay",
    "wrong_nonce",
    "wrong_attempt_id",
    "wrong_label",
    "wrong_session",
    "wrong_execution_id",
    "missing_contract_version",
    "oversized_output",
    "oversized_error",
)


class ProtocolCampaign(RuleBasedStateMachine):
    """One generated sequence against one isolated coordinator."""

    def __init__(self) -> None:
        super().__init__()
        self._root = Path(tempfile.mkdtemp(prefix="campaign-", dir=Path.cwd()))
        self.harness = CoordinatorHarness(self._root / "state")
        self.model = ProtocolModel()

        self.handouts: dict[str, object] = {}       # task_id -> Handout
        self.accepted_bodies: dict[str, dict] = {}  # task_id -> the settling body
        self.executions: list[str] = []
        self.observed: dict[str, set[str]] = {}     # execution_id -> lifecycles shown
        self.terminal_attempts: dict[str, str] = {}  # attempt_id -> terminal state
        # The credential last presented for a label, so a faulted registration
        # can be reconciled against the durable enrollment table.
        self.last_credential: dict[str, str] = {}
        # (scope, key) pairs whose durable mapping a fault made unknowable.
        self.ambiguous_keys: set[tuple[str, str]] = set()
        self.next_unit = 0
        self.pending_fault: tuple[int, str] | None = None
        # Restart and drain are bounded per sequence. Both are absorbing enough
        # that an unbounded number of them makes every sequence a sequence about
        # nothing: no session survives to reach settlement. The dedicated
        # scenarios in tests/test_adversarial_scenarios.py cover them directly.
        self.restarts = 0
        self.drains = 0

    @initialize()
    def open_the_network(self) -> None:
        """Start every sequence from a network that can actually do work.

        Reaching settlement takes an enrolled node, a queued execution, and a
        queued unit. Uniform rule selection almost never assembles that chain
        inside a bounded step budget, so the campaign would spend its whole
        budget on registration errors. The prerequisites are set up here; every
        adversarial ordering is still generated on top of them.
        """
        for index, label in enumerate(NODE_LABELS[:2]):
            credential = CREDENTIALS[index]
            self.last_credential[label] = credential
            response = self.harness.register(label, credential, "bootstrap")
            assert response.status_code == 200, response.text
            body = response.json()
            self.model.apply_bootstrap(body["enrollment_id"], label, credential)
            self._learn_session(label, body)

        response = self.harness.submit_execution(
            host=REQUESTER_HOSTS[0], task=TASK_TEXTS[0], idempotency_key=None
        )
        assert response.status_code == 202, response.text
        execution_id = response.json()["execution_id"]
        self.model.apply_submission(REQUESTER_HOSTS[0], None, TASK_TEXTS[0], execution_id)
        self.executions.append(execution_id)
        for _ in range(2):
            self._queue_one_unit(execution_id)

    def teardown(self) -> None:
        try:
            leaks = self.harness.scan_for_secrets()
        finally:
            self.harness.close()
            shutil.rmtree(self._root, ignore_errors=True)
        assert leaks == [], f"secret-class material became readable: {leaks}"

    # ── fault plumbing ───────────────────────────────────────────────

    def _with_pending_fault(self, call):
        """Run one driven operation, optionally under an armed persistence fault."""
        if self.pending_fault is None:
            return call(), False
        target_index, mode = self.pending_fault
        self.pending_fault = None
        self.harness.faults.arm(target_index=target_index, mode=mode)
        try:
            response = call()
        except Exception:
            response = None
        finally:
            self.harness.faults.disarm()
        fired = self.harness.faults.fired
        if fired:
            self._resync_from_durable()
        return response, fired

    def _resync_from_durable(self) -> None:
        """Re-read the facts a partial write may legitimately have moved.

        This only ever *forgets* or *copies*; it never invents an outcome the
        coordinator did not produce.
        """
        for attempt_id, row in self.harness.durable_attempts().items():
            modelled = self.model.attempts.get(attempt_id)
            if modelled is not None:
                modelled.state = row["state"]
        self.model.receipts = {
            attempt_id: self.model.receipts.get(attempt_id, "<unknown-payload>")
            for attempt_id in self.harness.durable_receipts()
        }
        self.model.credits = {
            attempt_id: row["enrollment_id"]
            for attempt_id, row in self.harness.durable_credits().items()
        }
        for execution_id in list(self.model.executions):
            row = self.harness.durable_execution(execution_id)
            if row is not None:
                self.model.apply_lifecycle(execution_id, row["lifecycle_status"])
        durable_mappings = {
            row["execution_id"]
            for row in self.harness.rows("SELECT * FROM execution_submissions")
        }
        for key, mapping in list(self.model.submissions.items()):
            if mapping.execution_id not in durable_mappings:
                self.model.submissions.pop(key, None)
        self._resync_enrollments()
        self._resync_sessions()

    def _resync_enrollments(self) -> None:
        durable = {record.node_id: record for record in state.enrollment_store.list()}
        for enrollment_id, enrollment in list(self.model.enrollments.items()):
            record = durable.get(enrollment.label)
            if record is None or record.enrollment_id != enrollment_id:
                self.model.enrollments.pop(enrollment_id, None)
            else:
                enrollment.status = record.status
                enrollment.credential_version = record.credential_version
        for label, record in durable.items():
            if record.enrollment_id in self.model.enrollments:
                continue
            credential = self.last_credential.get(label)
            if credential is None:
                continue
            learned = self.model.apply_bootstrap(record.enrollment_id, label, credential)
            learned.status = record.status
            learned.credential_version = record.credential_version

    def _resync_sessions(self) -> None:
        """Forget any session token the harness can no longer legitimately use.

        A registration whose response never arrived leaves a real worker in
        exactly this position: the coordinator may hold a new incarnation whose
        plaintext token nobody has.
        """
        for label in NODE_LABELS:
            held = self.harness.session_ids.get(label)
            if held is None:
                continue
            record = state.node_sessions.current(label)
            if record is None or record.session_id != held:
                modelled = self.model.sessions.get(held)
                if modelled is not None:
                    modelled.valid = False
                self.harness.session_tokens.pop(label, None)
                self.harness.session_ids.pop(label, None)

    # ── helpers ──────────────────────────────────────────────────────

    def _live_labels(self) -> list[str]:
        live = []
        for label in NODE_LABELS:
            session_id = self.harness.session_ids.get(label)
            if session_id is None or label not in self.harness.session_tokens:
                continue
            modelled = self.model.sessions.get(session_id)
            if modelled is not None and modelled.valid:
                live.append(label)
        return live

    def _session_id_for_token(self, token: str) -> str | None:
        for label, held in self.harness.session_tokens.items():
            if held == token:
                return self.harness.session_ids.get(label)
        return None

    def _record_observation(self, execution_id: str, lifecycle: str) -> None:
        self.observed.setdefault(execution_id, set()).add(lifecycle)
        self.model.observe(execution_id, lifecycle)

    def _learn_session(self, label: str, body: dict) -> None:
        self.model.apply_session(
            body["session_id"],
            label,
            body.get("enrollment_id"),
            int(body["credential_version"]) if body.get("credential_version") else None,
        )

    # ── enrollment and sessions ──────────────────────────────────────

    @rule(label_index=st.integers(0, len(NODE_LABELS) - 1),
          credential_index=st.integers(0, len(CREDENTIALS) - 1))
    def bootstrap_enrollment(self, label_index: int, credential_index: int) -> None:
        label = NODE_LABELS[label_index]
        credential = CREDENTIALS[credential_index]
        expected = self.model.expect_bootstrap(label, credential)
        self.last_credential[label] = credential

        response, fired = self._with_pending_fault(
            lambda: self.harness.register(label, credential, "bootstrap")
        )
        if response is None:
            return
        self._assert_no_secret_in(response)

        if response.status_code == 200:
            body = response.json()
            known = self.model.enrollments.get(body["enrollment_id"])
            if known is None:
                self.model.apply_bootstrap(body["enrollment_id"], label, credential)
            self._learn_session(label, body)
            if not fired:
                assert expected in (Bootstrap.CREATED, Bootstrap.IDEMPOTENT), (
                    f"a bootstrap the model refuses ({expected.value}) was admitted"
                )
                if expected is Bootstrap.IDEMPOTENT:
                    assert known is not None and body["enrollment_id"] == known.enrollment_id, (
                        "a bootstrap retry minted a second enrollment for one label"
                    )
            return

        if not fired:
            if expected is Bootstrap.REVOKED:
                assert response.status_code == 403, (
                    f"a revoked label was re-bootstrappable: {response.status_code}"
                )
                assert response.json()["detail"]["code"] == "node_enrollment_revoked"
                return
            assert response.status_code == 409, (
                f"duplicate identity registration was not a stable conflict: "
                f"{expected.value} -> {response.status_code}"
            )
            assert expected in (Bootstrap.LABEL_CONFLICT, Bootstrap.CREDENTIAL_CONFLICT)

    @rule(label_index=st.integers(0, len(NODE_LABELS) - 1))
    def returning_registration(self, label_index: int) -> None:
        label = NODE_LABELS[label_index]
        enrollment = self.model.enrollment_for_label(label)
        if enrollment is None:
            return
        active = enrollment.active
        credential = enrollment.credential
        self.last_credential[label] = credential
        response, fired = self._with_pending_fault(
            lambda: self.harness.register(label, credential, "returning")
        )
        if response is None:
            return
        self._assert_no_secret_in(response)
        if response.status_code == 200:
            body = response.json()
            assert body["enrollment_id"] == enrollment.enrollment_id, (
                "a returning registration changed the durable enrollment identity"
            )
            self._learn_session(label, body)
            if not fired:
                assert active, "a revoked enrollment obtained a fresh session"
        elif not fired:
            assert response.status_code in (401, 403, 409), response.text

    @rule(label_index=st.integers(0, len(NODE_LABELS) - 1))
    def revoke_enrollment(self, label_index: int) -> None:
        label = NODE_LABELS[label_index]
        enrollment = self.model.enrollment_for_label(label)
        if enrollment is None or not enrollment.active:
            return
        self.harness.revoke(enrollment.enrollment_id)
        self.model.apply_revoke(enrollment.enrollment_id)

    @rule(label_index=st.integers(0, len(NODE_LABELS) - 1),
          credential_index=st.integers(0, len(CREDENTIALS) - 1))
    def rotate_credential(self, label_index: int, credential_index: int) -> None:
        label = NODE_LABELS[label_index]
        enrollment = self.model.enrollment_for_label(label)
        credential = CREDENTIALS[credential_index]
        if enrollment is None or not enrollment.active:
            return
        if self.model.enrollment_for_credential(credential) is not None:
            return
        self.harness.rotate(
            enrollment.enrollment_id, credential, enrollment.credential_version
        )
        self.model.apply_rotate(enrollment.enrollment_id, credential)
        self.last_credential[label] = credential

    @precondition(lambda self: self.drains < 1)
    @rule(label_index=st.integers(0, len(NODE_LABELS) - 1))
    def drain_node(self, label_index: int) -> None:
        label = NODE_LABELS[label_index]
        if label not in self.harness.session_tokens:
            return
        self.drains += 1
        self.harness.drain(label)

    # ── work ─────────────────────────────────────────────────────────

    def _queue_one_unit(self, execution_id: str) -> None:
        task_id = f"u{self.next_unit}"
        self.next_unit += 1
        self.harness.enqueue_unit(
            task_id,
            execution_id=execution_id,
            unit_id=f"candidate-{task_id}",
            max_output_bytes=UNIT_OUTPUT_CAP,
            lease_seconds=UNIT_LEASE_SECONDS,
        )

    @precondition(lambda self: bool(self.executions))
    @rule(execution_index=st.integers(0, 7))
    def enqueue_unit(self, execution_index: int) -> None:
        self._queue_one_unit(self.executions[execution_index % len(self.executions)])

    @rule(label_index=st.integers(0, len(NODE_LABELS) - 1))
    def poll_for_work(self, label_index: int) -> None:
        label = NODE_LABELS[label_index]
        if label not in self.harness.session_tokens:
            return
        handout, _fired = self._with_pending_fault(lambda: self.harness.poll(label))
        if handout is None:
            return
        session_id = self.harness.session_ids[label]
        enrollment = self.model.enrollment_for_label(label)
        self.model.apply_issue(
            ModelAttempt(
                attempt_id=handout.attempt_id,
                task_id=handout.task_id,
                execution_id=handout.execution_id,
                unit_id=handout.unit_id,
                unit_kind=handout.unit_kind,
                contract_version=handout.contract_version,
                label=label,
                session_id=session_id,
                enrollment_id=enrollment.enrollment_id if enrollment else None,
                credential_version=enrollment.credential_version if enrollment else None,
                nonce=handout.nonce,
                lease_expires_at=self.harness.clock.now() + UNIT_LEASE_SECONDS,
                max_output_bytes=handout.max_output_bytes,
            )
        )
        self.handouts[handout.task_id] = handout
        assert handout.attempt_id in self.harness.durable_attempts(), (
            "a handout was published before its attempt was durable"
        )

    @precondition(lambda self: bool(self.handouts))
    @rule(
        unit_index=st.integers(0, 15),
        mutation=st.sampled_from(SUBMISSION_MUTATIONS),
        output_index=st.integers(0, len(WORKER_OUTPUTS) - 1),
    )
    def submit_result(self, unit_index: int, mutation: str, output_index: int) -> None:
        task_ids = sorted(self.handouts)
        task_id = task_ids[unit_index % len(task_ids)]
        handout = self.handouts[task_id]

        claimed_label = handout.label
        token = self.harness.session_tokens.get(handout.label)
        body = self.harness.result_body(handout, output=WORKER_OUTPUTS[output_index])

        if mutation == "replay":
            stored = self.accepted_bodies.get(task_id)
            if stored is None:
                return
            body = dict(stored)
        elif mutation == "wrong_nonce":
            body["nonce"] = "campaign-not-the-nonce"
        elif mutation == "wrong_attempt_id":
            body["attempt_id"] = "0" * 32
        elif mutation == "wrong_label":
            other = next((label for label in self._live_labels() if label != handout.label), None)
            if other is None:
                return
            claimed_label = other
            body["node_id"] = other
            token = self.harness.session_tokens.get(other)
        elif mutation == "wrong_session":
            other = next((label for label in self._live_labels() if label != handout.label), None)
            if other is None:
                return
            token = self.harness.session_tokens.get(other)
        elif mutation == "wrong_execution_id":
            body["execution_id"] = "e" * 32
        elif mutation == "missing_contract_version":
            body["contract_version"] = None
        elif mutation == "oversized_output":
            body["output"] = "x" * (UNIT_OUTPUT_CAP + 64)
        elif mutation == "oversized_error":
            body["error"] = "e" * 4096

        session_id = self._session_id_for_token(token) if token else None
        expected = self.model.expect_settlement(
            task_id=task_id,
            claimed_attempt_id=body.get("attempt_id"),
            claimed_nonce=body.get("nonce"),
            claimed_label=str(body.get("node_id")),
            claimed_execution_id=body.get("execution_id"),
            claimed_unit_id=body.get("execution_unit_id"),
            claimed_unit_kind=body.get("execution_unit_kind"),
            claimed_contract_version=body.get("contract_version"),
            session_id=session_id,
            payload_digest=payload_digest(body),
            output_bytes=len((body.get("output") or "").encode("utf-8")),
            error_bytes=len((body.get("error") or "").encode("utf-8")),
            now=self.harness.clock.now(),
        )

        before = self._side_effect_counts()
        receipts_before = set(self.harness.durable_receipts())
        response, fired = self._with_pending_fault(
            lambda: self.harness.submit(task_id, body, label=claimed_label, token=token)
        )
        receipts_after = set(self.harness.durable_receipts())
        settled_now = receipts_after - receipts_before

        if settled_now:
            # Learn the payload that settled, whether or not the caller was told.
            assert settled_now == {handout.attempt_id}, (
                f"settlement committed a receipt for an unexpected attempt: {settled_now}"
            )
            self.model.apply_settlement(handout.attempt_id, payload_digest(body))
            self.accepted_bodies[task_id] = dict(body)

        if fired or response is None:
            return
        self._assert_no_secret_in(response)

        if expected is Settlement.ACCEPT:
            assert response.status_code == 200, (
                f"a valid bound submission was refused: {response.status_code} {response.text}"
            )
            assert settled_now == {handout.attempt_id}, (
                "acceptance did not commit exactly one receipt"
            )
        elif expected is Settlement.REPLAY:
            self.check_exact_replay_creates_nothing(response, settled_now, before)
        elif expected is Settlement.PAYLOAD_LIMIT:
            assert response.status_code == 413, (
                f"an oversized payload was not bounded: {response.status_code}"
            )
            assert not settled_now
            self.model.apply_payload_limit(handout.attempt_id)
        else:
            self.check_stale_authority_never_settles(response, settled_now, mutation, before)

    def check_exact_replay_creates_nothing(self, response, settled_now, before) -> None:
        """Resubmitting an accepted result returns the identical response and
        creates no additional receipt, credit, quarantine row, or observation."""
        assert response.status_code == 200, (
            f"an exact replay was not honoured: {response.status_code} {response.text}"
        )
        assert response.json().get("status") == "accepted"
        assert not settled_now, "a replay created a second receipt"
        assert self._side_effect_counts() == before, (
            f"a replay changed durable side effects: {before} -> {self._side_effect_counts()}"
        )

    def check_stale_authority_never_settles(self, response, settled_now, mutation, before) -> None:
        """Expired, superseded, revoked, mismatched-session, mismatched-enrollment,
        mismatched-label and mismatched-nonce submissions are refused, and any
        quarantine they leave is bounded and carries no secret."""
        assert response.status_code in (401, 403, 409, 413, 422), (
            f"an invalid submission returned {response.status_code}: {response.text}"
        )
        assert not settled_now, f"a submission the model rejects ({mutation}) still settled"
        after = self._side_effect_counts()
        assert after["credits"] == before["credits"], "a rejected submission earned credit"
        assert after["quarantine"] - before["quarantine"] <= 1

    def _side_effect_counts(self) -> dict[str, int]:
        return {
            "receipts": len(self.harness.durable_receipts()),
            "credits": len(self.harness.durable_credits()),
            "quarantine": len(self.harness.quarantine_rows()),
            "observations": len(self.harness.capability_observations()),
        }

    @precondition(lambda self: bool(self.handouts))
    @rule(unit_index=st.integers(0, 15), size=st.integers(1, 3))
    def stream_tokens(self, unit_index: int, size: int) -> None:
        task_ids = sorted(self.handouts)
        task_id = task_ids[unit_index % len(task_ids)]
        handout = self.handouts[task_id]
        if handout.label not in self.harness.session_tokens:
            return
        receipts_before = set(self.harness.durable_receipts())
        response = self.harness.stream(task_id, handout, "t" * (size * 512))
        assert response.status_code in (200, 401, 403, 409, 413, 429), response.text
        assert set(self.harness.durable_receipts()) == receipts_before, (
            "streaming produced an accepted result"
        )
        self._assert_no_secret_in(response)

    @precondition(lambda self: bool(self.handouts))
    @rule(unit_index=st.integers(0, 15))
    def supersede_attempt(self, unit_index: int) -> None:
        task_ids = sorted(self.handouts)
        task_id = task_ids[unit_index % len(task_ids)]
        handout = self.handouts[task_id]
        if self.harness.supersede(handout.attempt_id):
            self.model.apply_terminal(handout.attempt_id, "superseded")

    # ── time and the janitor ─────────────────────────────────────────

    @rule(seconds=st.integers(5, 40))
    def advance_clock(self, seconds: int) -> None:
        self.harness.clock.offset = min(
            MAX_CLOCK_OFFSET, self.harness.clock.offset + seconds
        )

    @rule(seconds=st.integers(5, 40))
    def rewind_clock(self, seconds: int) -> None:
        self.harness.clock.offset = max(
            -MAX_CLOCK_OFFSET, self.harness.clock.offset - seconds
        )

    @rule()
    def run_janitor(self) -> None:
        self.harness.janitor()
        self.model.expire_leases(self.harness.clock.now())

    # ── canonical executions ─────────────────────────────────────────

    @rule(
        host_index=st.integers(0, len(REQUESTER_HOSTS) - 1),
        task_index=st.integers(0, len(TASK_TEXTS) - 1),
        key_index=st.integers(-1, len(IDEMPOTENCY_KEYS) - 1),
    )
    def submit_execution(self, host_index: int, task_index: int, key_index: int) -> None:
        host = REQUESTER_HOSTS[host_index]
        task = TASK_TEXTS[task_index]
        key = IDEMPOTENCY_KEYS[key_index] if key_index >= 0 else None
        expected = self.model.expect_submission(host, key, task)
        ambiguous = key is not None and (host, key) in self.ambiguous_keys

        response, fired = self._with_pending_fault(
            lambda: self.harness.submit_execution(
                host=host, task=task, idempotency_key=key
            )
        )
        if response is None:
            if key is not None:
                self.ambiguous_keys.add((host, key))
            return
        self._assert_no_secret_in(response)
        strict = not fired and not ambiguous

        if response.status_code == 409:
            assert response.json()["detail"]["code"] == "idempotency_conflict"
            if strict:
                assert expected is Submission.CONFLICT, (
                    "a submission the model considers convergent was refused as a conflict"
                )
            elif key is not None:
                # A mapping exists holding some other request; which one is
                # exactly what the fault made unknowable.
                self.ambiguous_keys.add((host, key))
            return

        if response.status_code == 503:
            if key is not None:
                self.ambiguous_keys.add((host, key))
            return

        assert response.status_code == 202, response.text
        execution_id = response.json()["execution_id"]
        replayed = response.headers.get("Idempotency-Replayed")

        if strict:
            assert expected is not Submission.CONFLICT, (
                "a differing canonical request under one key was not a conflict"
            )
            if key is not None:
                assert replayed == ("true" if expected is Submission.REPLAYED else "false"), (
                    f"Idempotency-Replayed was {replayed} for a {expected.value} submission"
                )
            if expected is Submission.REPLAYED:
                assert execution_id == self.model.submissions[(host, key)].execution_id, (
                    "one scope, key, and canonical request produced two execution IDs"
                )

        if execution_id not in self.model.executions:
            self.model.apply_submission(host, key, task, execution_id)
            self.executions.append(execution_id)
        elif key is not None:
            self.model.submissions[(host, key)] = ModelSubmissionMapping(task, execution_id)
        if key is not None:
            self.ambiguous_keys.discard((host, key))
        self._sync_execution(execution_id)

    @precondition(lambda self: bool(self.executions))
    @rule(execution_index=st.integers(0, 7))
    def cancel_execution(self, execution_index: int) -> None:
        execution_id = self.executions[execution_index % len(self.executions)]
        response, fired = self._with_pending_fault(
            lambda: self.harness.cancel_execution(execution_id)
        )
        if response is None:
            return
        if not fired:
            assert response.status_code in (200, 503), response.text
        if response.status_code == 200:
            self._record_observation(execution_id, response.json()["lifecycle_status"])
        self._sync_execution(execution_id)

    @precondition(lambda self: bool(self.executions))
    @rule(execution_index=st.integers(0, 7))
    def read_execution(self, execution_index: int) -> None:
        execution_id = self.executions[execution_index % len(self.executions)]
        self._sync_execution(execution_id)

    def _sync_execution(self, execution_id: str) -> None:
        response = self.harness.get_execution(execution_id)
        if response.status_code != 200:
            return
        lifecycle = response.json()["lifecycle_status"]
        self._record_observation(execution_id, lifecycle)
        self.model.apply_lifecycle(execution_id, lifecycle)

    # ── faults and restart ───────────────────────────────────────────

    @rule(
        target_index=st.integers(0, 12),
        mode=st.sampled_from(("io", "commit", "busy")),
    )
    def arm_persistence_fault(self, target_index: int, mode: str) -> None:
        self.pending_fault = (target_index, mode)

    @precondition(lambda self: self.restarts < 2)
    @rule()
    def restart_coordinator(self) -> None:
        self.pending_fault = None
        self.harness.faults.disarm()
        self.restarts += 1
        self.harness.restart()
        self.model.restart()
        # Handouts are deliberately *kept*. A worker that was holding a lease
        # when the coordinator died still holds it, and its late submission is
        # the whole point of the restart-mid-submission scenario: it must fail
        # closed, while an exact replay of an already-settled attempt must still
        # be recoverable by a fresh session of the same enrollment.
        for label in NODE_LABELS:
            enrollment = self.model.enrollment_for_label(label)
            if enrollment is None or not enrollment.active:
                continue
            response = self.harness.register(label, enrollment.credential, "returning")
            if response.status_code == 200:
                self._learn_session(label, response.json())
        for execution_id in list(self.executions):
            self._sync_execution(execution_id)

    # ── invariants ───────────────────────────────────────────────────

    def _assert_no_secret_in(self, response) -> None:
        """Identity material may be *delivered to its owner* — that is the
        protocol; a registration grant carries the session token and a handout
        carries the nonce. What may never happen is a credential being echoed
        at all, or any identity material appearing in an error body."""
        text = response.text
        for needle in CREDENTIALS:
            assert needle not in text, "a response echoed an enrollment credential"
        if response.status_code < 400:
            return
        for needle in self.harness.minted_secrets:
            assert needle not in text, "an error body echoed identity material"

    @invariant()
    def settlement_is_at_most_once(self) -> None:
        """At most one accepted settlement per attempt and per execution unit."""
        receipts = self.harness.durable_receipts()
        by_task: dict[str, int] = {}
        for row in receipts.values():
            by_task[row["task_id"]] = by_task.get(row["task_id"], 0) + 1
        assert all(count == 1 for count in by_task.values()), (
            f"one execution unit accepted more than one settlement: {by_task}"
        )
        assert set(receipts) == set(self.model.accepted_attempts()), (
            "durable receipts and the reference model disagree on what settled"
        )

    @invariant()
    def credit_matches_acceptance_exactly(self) -> None:
        """Contribution records are one-to-one with accepted receipts."""
        receipts = self.harness.durable_receipts()
        credits = self.harness.durable_credits()
        assert set(credits) == set(receipts), (
            f"credit and acceptance diverged: "
            f"credited-not-accepted={sorted(set(credits) - set(receipts))} "
            f"accepted-not-credited={sorted(set(receipts) - set(credits))}"
        )
        for attempt_id, row in credits.items():
            assert row["enrollment_id"] == receipts[attempt_id]["assigned_enrollment_id"], (
                "credit was attributed to a different enrollment than the receipt"
            )
            assert row["basis"] == "compute_contribution"
            assert not row["points_are_monetary"]

    @invariant()
    def terminal_attempts_never_reopen(self) -> None:
        """A terminal attempt stays terminal, with the same classification."""
        for attempt_id, row in self.harness.durable_attempts().items():
            state_now = row["state"]
            previous = self.terminal_attempts.get(attempt_id)
            if previous is not None:
                assert previous == state_now, (
                    f"attempt {attempt_id[:8]} moved from terminal {previous} to {state_now}"
                )
            elif state_now != "active":
                self.terminal_attempts[attempt_id] = state_now

    @invariant()
    def execution_lifecycle_is_monotonic(self) -> None:
        """Terminal executions stay terminal and keep their classification."""
        for execution_id, seen in self.observed.items():
            terminal = {value for value in seen if value in TERMINAL_LIFECYCLES}
            assert len(terminal) <= 1, (
                f"execution {execution_id[:8]} was shown two terminal states: {sorted(terminal)}"
            )

    @invariant()
    def durable_state_is_never_behind_public_state(self) -> None:
        """Nothing a caller was shown may be ahead of the last durable commit."""
        for execution_id, seen in self.observed.items():
            row = self.harness.durable_execution(execution_id)
            assert row is not None, (
                f"execution {execution_id[:8]} was published before it was durable"
            )
            durable = row["lifecycle_status"]
            for lifecycle in seen:
                if lifecycle in TERMINAL_LIFECYCLES:
                    assert durable == lifecycle, (
                        f"caller saw terminal {lifecycle} but durable state is {durable}"
                    )
                else:
                    assert (
                        durable in TERMINAL_LIFECYCLES
                        or LIFECYCLE_ORDER[durable] >= LIFECYCLE_ORDER[lifecycle]
                    ), f"caller saw {lifecycle} but durable state is only {durable}"

        receipts = self.harness.durable_receipts()
        for event in state.pipeline_events:
            if event.get("type") != "attempt_completed":
                continue
            attempt_id = event.get("data", {}).get("attempt_id")
            if attempt_id:
                assert attempt_id in receipts, (
                    "an attempt_completed event was published without a durable receipt"
                )

    @invariant()
    def quarantine_is_bounded_and_carries_no_identity(self) -> None:
        rows = self.harness.quarantine_rows()
        assert len(rows) <= 500, "quarantine exceeded its documented bound"
        blob = "\n".join(" ".join(str(value) for value in tuple(row)) for row in rows)
        for needle in tuple(CREDENTIALS) + tuple(self.harness.minted_secrets):
            assert needle not in blob, "quarantine retained identity material"

    @invariant()
    def capability_observations_are_not_double_counted(self) -> None:
        identifiers = [
            row["observation_id"] for row in self.harness.capability_observations()
        ]
        assert len(identifiers) == len(set(identifiers)), (
            "a capability observation was recorded twice"
        )

    @invariant()
    def shadow_evidence_never_reaches_routing(self) -> None:
        """`capability_evidence_mode` is off here, so nothing may be recorded in
        a way that could order, exclude, or prefer a node."""
        assert self.harness.settings["capability_evidence_mode"] in ("off", "shadow")
        assert self.harness.rows("SELECT * FROM capability_shadow_decisions") == [], (
            "a shadow decision was persisted with evidence mode off"
        )

    @invariant()
    def only_settled_attempts_hold_receipts(self) -> None:
        """Stale authority, stated durably: nothing that expired, was reclaimed,
        superseded, cancelled, or interrupted ever produced an accepted receipt."""
        attempts = self.harness.durable_attempts()
        for attempt_id in self.harness.durable_receipts():
            assert attempts[attempt_id]["state"] == "settled", (
                f"attempt {attempt_id[:8]} holds a receipt while "
                f"{attempts[attempt_id]['state']}"
            )

    @invariant()
    def enrollment_isolation_holds(self) -> None:
        """No enrollment holds a receipt for an attempt issued to another."""
        attempts = self.harness.durable_attempts()
        for attempt_id, receipt in self.harness.durable_receipts().items():
            attempt = attempts.get(attempt_id)
            assert attempt is not None
            assert receipt["assigned_enrollment_id"] == attempt["assigned_enrollment_id"]
            assert receipt["assigned_node_id"] == attempt["assigned_node_id"]


_COMMON = {
    "deadline": None,
    "database": None,
    "suppress_health_check": [
        HealthCheck.too_slow,
        HealthCheck.filter_too_much,
        HealthCheck.data_too_large,
    ],
}

_CI_SETTINGS = settings(
    max_examples=15, stateful_step_count=14, derandomize=True, **_COMMON
)
_EXTENDED_SETTINGS = settings(
    max_examples=250, stateful_step_count=40, derandomize=False, **_COMMON
)

ProtocolCampaign.TestCase.settings = (
    _EXTENDED_SETTINGS if CAMPAIGN_PROFILE == "extended" else _CI_SETTINGS
)

TestProtocolCampaign = ProtocolCampaign.TestCase


@pytest.mark.parametrize(
    "table",
    ["attempts", "accepted_result_receipts", "contributions", "result_quarantine"],
)
def test_campaign_reads_the_tables_it_asserts_on(tmp_path, table):
    """Verify the negative result before trusting it (ROADMAP §2).

    Every invariant above is a query. A typo in a table name would make each of
    them pass vacuously forever, which is exactly the failure mode §2 warns
    about, so assert the tables exist and that the reader reaches them.
    """
    harness = CoordinatorHarness(Path(tmp_path) / "state")
    try:
        names = {
            row[0]
            for row in harness.rows("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert table in names, f"{table} is missing; the invariants would pass vacuously"
        with sqlite3.connect(harness.database) as con:
            con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    finally:
        harness.close()
