"""Contract tests for the six extension seams (ADR 0016).

There is no plugin framework here, and building one to host a single
implementation would have been the wrong answer. What exists instead is a
described boundary per seam and a test that the one implementation honours it.
A second implementation would have to satisfy the same assertions; that is the
whole of the extension point.

Each test states what crosses the boundary and what must never cross. Where the
current implementation does not honour its own boundary, the reproducer is here
too, marked xfail with the finding it demonstrates — the fix is a design change
and does not belong in this PR.
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
import textwrap
from pathlib import Path

import pytest

import capability_evidence
import ledger
import node_capabilities
import routes_events
import routes_nodes
import server_state as state
import verification_evidence
from execution import dispatch
from execution.artifacts import ArtifactStore
from execution.validator_protocol import ValidatorRunnerRequestV2
from node_enrollments import NodeEnrollmentStore
from tests.protocol_harness import CREDENTIALS, CoordinatorHarness


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def harness(tmp_path):
    coordinator = CoordinatorHarness(Path(tmp_path) / "state")
    try:
        yield coordinator
    finally:
        coordinator.close()


def _source(module) -> str:
    return inspect.getsource(module)


# ── Seam 1: scheduler backend ────────────────────────────────────────
#
# Crosses: a typed execution request, the live node registry, and a capability
# match result. Out: a placement decision and an ordered set of eligible nodes.
#
# Must never cross: observed evidence of any kind, a reputation or score, a
# storage handle, or a node label used as anything but a key into the registry.
# A second scheduler must be able to decide placement from the request and the
# claims alone.


def test_placement_decides_from_the_request_and_claims_only():
    matcher = inspect.signature(node_capabilities.match_node_requirements)
    parameter_names = set(matcher.parameters)

    assert not parameter_names & {"connection", "con", "store", "db", "path", "evidence"}, (
        f"the capability matcher takes a storage or evidence handle: {parameter_names}"
    )
    for name in ("qualifying_nodes", "select_placement"):
        function_source = inspect.getsource(getattr(dispatch, name))
        for forbidden in ("evidence_store", "capability_evidence", "verification_evidence"):
            assert forbidden not in function_source, (
                f"dispatch.{name} consults {forbidden}; evidence must not decide placement"
            )
        # Checking for the word "SELECT" would match `select_placement` itself.
        # What the boundary forbids is reaching a store, so look for that.
        for storage in ("execute(", "connection(", "sqlite3", "_DB_PATH"):
            assert storage not in function_source, (
                f"dispatch.{name} reaches storage directly via {storage!r}"
            )


def test_the_matcher_is_a_pure_function_of_its_arguments():
    descriptor = node_capabilities.NodeCapabilityDescriptorV1.model_validate(
        CoordinatorHarness._descriptor()
    )
    first = node_capabilities.match_node_requirements(
        None, [], descriptor, ["code"], required_output_capacity_bytes=1024
    )
    second = node_capabilities.match_node_requirements(
        None, [], descriptor, ["code"], required_output_capacity_bytes=1024
    )
    assert first.eligible == second.eligible
    assert first.eligible is True


def test_recording_evidence_is_not_deciding_with_it():
    """`dispatch` writes evidence. The boundary is that it never reads it back
    into a decision, which is a different statement from "never imports it"."""
    source = _source(dispatch)
    assert "verification_evidence_store" in source, (
        "this test is checking the wrong module if dispatch records no evidence"
    )
    for decision_marker in ("aggregate(", "list_scope_aggregates(", "list_scope_summaries("):
        assert decision_marker not in source, (
            f"dispatch reads aggregated evidence via {decision_marker}"
        )


# ── Seam 2: enrollment and identity provider ─────────────────────────
#
# Crosses: a bootstrap admission secret, a worker-proposed credential, and a
# normalized display label. Out: an opaque immutable enrollment id and a
# credential version.
#
# Must never cross: a plaintext credential into storage, or a node label used as
# a trust key. A second identity provider would have to mint opaque stable ids
# and expose only digests.


def test_enrollment_storage_holds_no_plaintext_credential(tmp_path):
    store = NodeEnrollmentStore(Path(tmp_path) / "enrollments.db")
    store.migrate()
    credential = CREDENTIALS[0]
    record = store.bootstrap("node-a", credential).record

    assert credential not in repr(record)
    assert "credential" not in record.public_metadata()
    assert credential.encode() not in (Path(tmp_path) / "enrollments.db").read_bytes()
    with sqlite3.connect(Path(tmp_path) / "enrollments.db") as con:
        columns = {
            row[1] for row in con.execute("PRAGMA table_info(node_enrollments)")
        }
    assert "credential_digest" in columns
    assert not {"credential", "enrollment_credential", "secret"} & columns


def test_a_node_label_is_never_the_trust_key_for_accounting():
    """Standings group by immutable enrollment, then by explicit legacy session.
    A label only ever groups historical rows that predate both."""
    source = inspect.getsource(ledger.get_standings)
    assert 'f"enrollment:{enrollment_id}"' in source
    assert 'f"legacy-session:{session_id}"' in source
    assert 'f"historical-node:' in source
    # And the ordering matters: enrollment first, label last.
    assert source.index("enrollment:") < source.index("historical-node:")


def test_settlement_authority_requires_more_than_a_label(harness):
    """The label selects a session; the session and enrollment authorize."""
    assert harness.register("n0", CREDENTIALS[0], "bootstrap").status_code == 200
    assert harness.register("n1", CREDENTIALS[1], "bootstrap").status_code == 200
    execution = harness.submit_execution(
        host="10.0.0.1", task="synthetic-task-alpha", idempotency_key=None
    )
    execution_id = execution.json()["execution_id"]
    harness.enqueue_unit("u0", execution_id=execution_id, unit_id="candidate-u0")
    handout = harness.poll("n0")
    assert handout is not None

    stolen = harness.result_body(handout, node_id="n1")
    refused = harness.submit(
        "u0", stolen, label="n1", token=harness.session_tokens["n1"]
    )
    assert refused.status_code in (401, 403), refused.text
    assert harness.durable_receipts() == {}


# ── Seam 3: discovery and transport ──────────────────────────────────
#
# Crosses: inbound worker-initiated HTTP carrying a session bearer. Out: task
# handouts on the worker's own poll.
#
# Must never cross: a coordinator-initiated connection to a worker, or a worker
# address used for transport. A second transport (a queue, a relay, a different
# protocol) must be substitutable without changing attempt or settlement
# semantics, which is only true while the coordinator never dials out.


def test_the_coordinator_never_dials_a_worker():
    coordinator_modules = [
        "routes_nodes.py",
        "server_state.py",
        "execution/dispatch.py",
        "execution/attempts.py",
        "node_sessions.py",
        "node_enrollments.py",
    ]
    for relative in coordinator_modules:
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        for outbound in ("httpx.AsyncClient", "httpx.Client", "requests.", "urlopen("):
            assert outbound not in source, (
                f"{relative} opens an outbound connection; the coordinator must "
                "never dial a worker"
            )


def test_no_durable_worker_record_carries_a_transport_address(harness):
    assert harness.register("n0", CREDENTIALS[0], "bootstrap").status_code == 200
    execution = harness.submit_execution(
        host="10.0.0.1", task="synthetic-task-alpha", idempotency_key=None
    )
    harness.enqueue_unit(
        "u0", execution_id=execution.json()["execution_id"], unit_id="candidate-u0"
    )
    handout = harness.poll("n0")
    assert handout is not None

    for table in ("attempts", "node_enrollments"):
        rows = harness.rows(f"SELECT * FROM {table}")
        assert rows, f"{table} is empty; this test proves nothing"
        columns = set(rows[0].keys())
        # Whole-word segments, not substrings: "descriptor" contains "ip" and
        # "provider" contains "id", and a check that loose reports leaks that
        # are not there.
        segments = {segment for column in columns for segment in column.split("_")}
        for address_shaped in (
            "url",
            "uri",
            "address",
            "addr",
            "host",
            "hostname",
            "endpoint",
            "port",
            "ip",
        ):
            assert address_shaped not in segments, (
                f"{table} carries a transport address column ({address_shaped})"
            )


# ── Seam 4: validator executor ───────────────────────────────────────
#
# Crosses: a bounded control message naming a built-in validator, its version,
# a validated logical filename or an output reference, and parent-clamped limits.
#
# Must never cross: coordinator configuration, a credential, a database path, a
# module or executable name, or a callable. A second executor (a container, a
# microVM) would receive the same control message and nothing more.


def test_the_validator_control_message_carries_control_metadata_only():
    fields = set(ValidatorRunnerRequestV2.model_fields)
    for forbidden in (
        "config",
        "settings",
        "credential",
        "secret",
        "token",
        "nonce",
        "database",
        "db_path",
        "module",
        "command",
        "executable",
        "callable",
        "entrypoint",
    ):
        assert not any(forbidden in name for name in fields), (
            f"the validator runner request exposes {forbidden!r}: {sorted(fields)}"
        )


def test_no_coordinator_config_key_reaches_the_validator_child():
    """Config informs parent-side limits. It does not cross the process boundary."""
    import config

    child_source = (REPO_ROOT / "execution" / "validator_runner.py").read_text(
        encoding="utf-8"
    )
    assert "get_config" not in child_source, (
        "the validator child reads coordinator configuration"
    )
    for key in config.DEFAULTS:
        assert f'"{key}"' not in child_source, (
            f"the validator child names the coordinator config key {key!r}"
        )


# ── Seam 5: artifact provenance signer and publisher ─────────────────
#
# Crosses: a sealed manifest hash committed with terminal execution state, and
# artifact-root ownership. Out: an authenticated download or an explicit share.
#
# Must never cross: publication without the committed seal. A future signer
# would add a signature over the same manifest; it would not become the thing
# that decides whether publication may happen.


def test_artifact_publication_requires_a_committed_seal():
    source = inspect.getsource(ArtifactStore)
    assert "manifest" in source
    for marker in ("sealed", "manifest_hash", "manifest_sha256"):
        if marker in source:
            break
    else:  # pragma: no cover - the seal must be nameable
        pytest.fail("the artifact store names no sealed manifest")


def test_no_signing_key_or_signature_field_is_assumed_anywhere():
    """There is no provenance signer, and no boundary here pretends there is."""
    import config

    assert not any("sign" in key for key in config.DEFAULTS), (
        "a signing configuration key exists; the seam description is stale"
    )
    artifacts_source = (REPO_ROOT / "execution" / "artifacts.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("private_key", "signing_key", "sign(", "verify_signature"):
        assert forbidden not in artifacts_source, (
            f"artifacts.py references {forbidden!r}; provenance signing is deferred"
        )


# ── Seam 6: reputation, accounting, and future payment policy ────────
#
# Crosses: an accepted receipt's output/error and its enrollment attribution.
# Out: a fixed non-monetary point value.
#
# Must never cross: evidence, reputation, or any signal about past behaviour into
# what work is worth; and no monetary meaning, ever. A future payment policy
# would replace `compute_contribution_points`, not read history inside it.


def test_the_accounting_policy_is_a_pure_function_of_one_result():
    signature = inspect.signature(ledger.compute_contribution_points)
    assert set(signature.parameters) == {"output", "error"}, (
        "accounting reads something other than the result it is paying for"
    )
    assert ledger.compute_contribution_points(output="x", error=None) == 5
    assert ledger.compute_contribution_points(output="", error=None) == 0
    assert ledger.compute_contribution_points(output="x", error="boom") == 0

    # Scan the body, not the docstring: the docstring says the policy reads no
    # evidence, and a naive text search would read that as it reading some.
    tree = ast.parse(textwrap.dedent(inspect.getsource(ledger.compute_contribution_points)))
    function = tree.body[0]
    if (
        function.body
        and isinstance(function.body[0], ast.Expr)
        and isinstance(function.body[0].value, ast.Constant)
    ):
        function.body = function.body[1:]
    body_source = ast.unparse(function)
    for forbidden in ("evidence", "reputation", "score", "history", "aggregate"):
        assert forbidden not in body_source, (
            f"the accounting policy consults {forbidden}"
        )


def test_points_can_never_be_monetary(tmp_path):
    database = Path(tmp_path) / "events.db"
    with sqlite3.connect(database) as con:
        ledger.ensure_contribution_schema(con)
        schema = con.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'contributions'"
        ).fetchone()[0]
        assert "points_are_monetary = 0" in schema.replace("==", "=")
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO contributions (contribution_id, contributor, "
                "contribution_type, points, task, details, basis, "
                "points_are_monetary, created_at) VALUES "
                "('x', 'n0', 'compute', 5, 'compute_contribution', 'd', "
                "'compute_contribution', 1, 0)"
            )


def test_no_evidence_path_writes_a_contribution():
    for module in (capability_evidence, verification_evidence):
        source = _source(module)
        assert "insert_contribution_in_transaction" not in source, (
            f"{module.__name__} writes contributions; evidence must not pay anyone"
        )
        assert "contributions" not in source, (
            f"{module.__name__} touches the contributions table"
        )


# ── the boundary the current implementation does not honour ──────────


@pytest.mark.xfail(
    reason=(
        "FINDING (Theme 4A): accounting executes inside the settlement "
        "transaction. ledger.compute_contribution_points is now a named policy "
        "that settlement applies rather than defines, but the contribution INSERT "
        "still runs inside AttemptStore.settle's BEGIN IMMEDIATE. A second "
        "accounting or payment policy could not be substituted without threading "
        "one through settlement, which would be exactly the indirection this PR "
        "was told not to build. Deferred: see ADR 0016."
    ),
    strict=True,
)
def test_settlement_does_not_execute_accounting_inline():
    settle_source = inspect.getsource(
        __import__("execution.attempts", fromlist=["AttemptStore"]).AttemptStore.settle
    )
    assert "insert_contribution_in_transaction" not in settle_source


@pytest.mark.xfail(
    reason=(
        "FINDING (Theme 4A): the legacy registration accepts a worker-supplied "
        "`hostname` and stores it on the process-local node record, where a "
        "protected operator view displays it. It is never dialled - the transport "
        "boundary holds - but it is transport-shaped data crossing a boundary "
        "that says addresses are not needed, and the typed capability descriptor "
        "deliberately excludes hostnames for exactly that reason. Removing the "
        "field is a protocol change, not a Theme 4A cleanup. Deferred: see ADR 0016."
    ),
    strict=True,
)
def test_registration_does_not_accept_a_worker_supplied_hostname():
    assert "hostname" not in state.NodeRegistration.model_fields


# ── the boundary this PR fixed ───────────────────────────────────────


def test_routes_do_not_speak_sql():
    """Fixed in this PR. Routes ask server_state for events; which store backs
    them is not an HTTP handler's business."""
    for relative in sorted(REPO_ROOT.glob("routes_*.py")):
        source = relative.read_text(encoding="utf-8")
        assert "import sqlite3" not in source, f"{relative.name} imports sqlite3"
        for statement in ("SELECT ", "INSERT INTO", "UPDATE ", "DELETE FROM"):
            assert statement not in source, (
                f"{relative.name} contains raw SQL ({statement.strip()})"
            )


def test_the_event_reader_is_bounded_and_validated():
    for bad in (-1, "0", True):
        with pytest.raises(ValueError):
            state.read_persisted_events(since=bad)
    for bad in (0, 1001, True):
        with pytest.raises(ValueError):
            state.read_persisted_events(since=0, limit=bad)
    assert routes_events.get_events is not None
    assert routes_nodes.router is not None
