"""The two routes Phase 3b adds, and the claim they both rest on.

Neither is a new contract. `GET /v1/executions/{id}/provenance` returns
`as_export()`, the same object the audit bundle already carries inside
`mycelium-provenance.json`, so the offline checker keeps working unchanged
against either copy. `GET /v1/operator/ledger-chain` returns
`verify_ledger_chain().as_dict()`, whose only caller until now was
`scripts/ledger_chain_admin.py verify`.

Two things are asserted here that no amount of prose can establish:

1. **The walk is never shortened.** No checkpoint, no "verified up to index N",
   no skipped prefix. The failure being detected is a rewrite of entries that
   were already walked once, so any of those would blind the check to exactly
   the case it exists for. What is bounded is the frequency, and the age of the
   walk behind a cached verdict is always served with it.

2. **Both responses are content-free.** A sentinel is pushed through a real run
   — the task text, the deliverable's bytes, the accepted receipt's output, and
   two of the ledger's own chained columns — and neither response is allowed to
   contain it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import ledger
from execution.contracts import ExecutionRequestV1, ExecutionResultV1
from server import app

RUN = "20260908_120000"
EXECUTION = "exec_" + "4d17b0ac" * 3

# Distinctive enough that a substring match cannot be a coincidence, and short
# enough to survive any field's length bound.
SENTINEL = "zqx7canary9téa"


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


# ── A real run, with a real envelope behind it ───────────────────────


def _run_directory(task: str, code: str) -> Path:
    run_dir = Path("output") / RUN
    (run_dir / "code").mkdir(parents=True, exist_ok=True)
    review = "## Quality Rating\nPASS\n\n## Final Output\nDone.\n"
    (run_dir / "full_log.json").write_text(
        json.dumps(
            {
                "task": task,
                "timestamp": RUN,
                "execution_id": EXECUTION,
                "plan": [{"id": 1, "title": "Build it", "description": task,
                          "depends_on": []}],
                "results": {"1": code},
                "review": review,
                "rating": "PASS",
                "code_files": [f"output/{RUN}/code/main.py"],
                "code_problems": [],
                "mode": "distributed",
                "nodes_used": 1,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "review.md").write_text(review, encoding="utf-8")
    (run_dir / "output.md").write_text(f"Built it.\n\n{code}\n", encoding="utf-8")
    (run_dir / "code" / "main.py").write_text(code, encoding="utf-8")
    return run_dir


def _receipt(*, output: str) -> None:
    from server_state import _DB_PATH

    with sqlite3.connect(_DB_PATH) as con:
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
                "a" * 32, "task-1", EXECUTION, "dag-1", "dag_subtask",
                "node-7c22", "enr_000001", "1", "d" * 64,
                "ollama", "qwen3.5:4b", "sha256:" + "c" * 64,
                "production", "1", "e" * 64, "1", "f" * 64, 1000.0,
                output, None, 1.0, "settled_output",
            ),
        )
        con.commit()


def _publish(*, task: str = "Add a flag", code: str = "x = 1\n",
             output: str = "done", envelope: bool = True) -> None:
    """A run past its publication boundary, the way the pipeline leaves one."""
    from execution.service import get_execution_service
    from server_state import attempt_store, provenance_envelope_store

    run_dir = _run_directory(task, code)
    service = get_execution_service()
    attempt_store.migrate()

    result = ExecutionResultV1(
        execution_id=EXECUTION,
        status="completed",
        lifecycle_status="completed",
        validation_outcome="passed",
        assurance_level="unverified",
        task=task,
        strategy_requested="dag",
        strategy_selected="dag",
        strategy_version="1",
        selector_reason="explicit request",
        selector_version="1",
        placement_requested="auto",
        placement_planned="distributed",
        placement_observed="distributed",
        created_at="2026-09-08T12:00:00Z",
        completed_at="2026-09-08T12:05:05Z",
    )
    request = ExecutionRequestV1(task=task, strategy="dag", placement="auto")
    service.store.create(request, result)
    service.artifacts.register_root(EXECUTION, str(run_dir), strategy="dag", active=True)
    manifest = service.artifacts.seal_manifest(EXECUTION)
    result.sealed_manifest_hash = manifest.manifest_hash
    result.artifact_integrity_mode = manifest.integrity_mode
    service.store.save(request, result)

    if envelope:
        _receipt(output=output)
        provenance_envelope_store.record(
            EXECUTION,
            manifest=manifest,
            validators=[{"name": "parse_precheck", "version": "3", "outcome": "passed"}],
        )


def _seed_chain(count: int) -> None:
    for index in range(count):
        ledger.log_contribution(
            f"node-{index:02d}",
            "compute",
            5,
            task="compute_contribution",
            contribution_id=f"contribution:{index:016d}",
            attempt_id=f"attempt-{index:04d}",
        )


# ── GET /v1/executions/{id}/provenance ───────────────────────────────


def test_the_provenance_route_returns_the_shape_the_bundle_already_carries(client):
    """`as_export()`, so this is not a new contract and the offline checker
    keeps working against either copy."""
    from server_state import provenance_envelope_store

    _publish()
    response = client.get(f"/v1/executions/{EXECUTION}/provenance")
    assert response.status_code == 200, response.text[:400]
    body = response.json()

    expected = provenance_envelope_store.get(EXECUTION).as_export()
    assert body == expected, "the route does not return as_export()"
    assert body["envelope_version"] == "1"
    assert isinstance(body["producers"], list)
    assert "envelope_digest" in body


def test_the_envelope_digest_recomputes_from_what_the_route_serves(client):
    """The point of the envelope is that a recipient can check it with no
    coordinator, no network and no credential. If the served object could not
    be re-hashed, it would be a report about an envelope rather than one."""
    from provenance import envelope_digest

    _publish()
    body = client.get(f"/v1/executions/{EXECUTION}/provenance").json()

    recorded = body.pop("envelope_digest")
    body.pop("signature", None)
    body.pop("signature_algorithm", None)
    assert envelope_digest(body) == recorded


def test_an_absent_envelope_is_a_404_and_never_an_empty_object(client):
    """A legacy run that predates envelopes has none.

    An empty object would say "there is one and it records nothing", which is
    a different and false statement, and the panel that reads this renders
    absent rather than empty on the strength of it.
    """
    _publish(envelope=False)
    response = client.get(f"/v1/executions/{EXECUTION}/provenance")
    assert response.status_code == 404, response.text[:300]
    assert response.json()["detail"]["code"] == "provenance_envelope_not_found"

    missing = client.get("/v1/executions/exec_does_not_exist/provenance")
    assert missing.status_code == 404


def test_the_provenance_route_is_viewer_gated(client, monkeypatch):
    import config

    _publish()
    monkeypatch.setitem(config.get(), "viewer_key", "k" * 32)
    refused = client.get(f"/v1/executions/{EXECUTION}/provenance")
    assert refused.status_code == 401, refused.text[:200]
    assert refused.headers.get("WWW-Authenticate") == "Bearer"

    allowed = client.get(
        f"/v1/executions/{EXECUTION}/provenance",
        headers={"X-Viewer-Key": "k" * 32},
    )
    assert allowed.status_code == 200


# ── GET /v1/operator/ledger-chain ────────────────────────────────────


def test_the_chain_route_returns_the_verdict_plus_the_age_of_its_walk(client):
    _seed_chain(4)
    response = client.get("/v1/operator/ledger-chain")
    assert response.status_code == 200, response.text[:300]
    body = response.json()

    assert body["ok"] is True
    assert body["chained_entries"] == 4
    assert body["genesis_unchained_entries"] == 0
    assert body["break_at_index"] is None
    assert body["walk_is_complete"] is True
    assert body["walk_ttl_seconds"] == ledger.LEDGER_CHAIN_WALK_TTL_SECONDS
    assert body["walk_age_seconds"] >= 0
    assert "walked_at" in body


def test_the_chain_route_is_operator_gated(client, monkeypatch):
    """Under `/v1/operator/`, which `deploy/Caddyfile.public` refuses at the
    edge. The handler also calls `require_viewer` itself, so the route is
    refused by its own code and not only by the middleware."""
    import config

    _seed_chain(2)
    monkeypatch.setitem(config.get(), "viewer_key", "k" * 32)
    refused = client.get("/v1/operator/ledger-chain")
    assert refused.status_code == 401, refused.text[:200]

    allowed = client.get(
        "/v1/operator/ledger-chain", headers={"X-Viewer-Key": "k" * 32}
    )
    assert allowed.status_code == 200


def test_the_operator_prefix_is_refused_at_the_public_edge():
    """The gating claim, read off the file that makes it rather than asserted.

    If this prefix ever stops being refused there, the chain panel's placement
    argument stops holding and the panel should be reconsidered along with it.
    """
    caddyfile = (
        Path(__file__).resolve().parent.parent / "deploy" / "Caddyfile.public"
    ).read_text(encoding="utf-8")
    assert "/v1/operator/*" in caddyfile, (
        "the operator prefix is no longer refused at the public edge, so "
        "`/v1/operator/ledger-chain` is now reachable with a viewer key alone"
    )


def test_a_real_break_is_reported_with_enough_to_act_on(client):
    """Produced by editing a chained column, not by mocking a verdict."""
    _seed_chain(5)
    with sqlite3.connect(ledger.LEDGER_DB_FILE) as con:
        con.execute(
            "UPDATE contributions SET contributor = ? WHERE entry_index = 2",
            ("edited-after-the-fact",),
        )
        con.commit()
    ledger.reset_ledger_chain_cache()

    body = client.get("/v1/operator/ledger-chain").json()
    assert body["ok"] is False
    assert body["break_at_index"] == 2
    assert body["break_entry_id"] == "contribution:0000000000000002"
    assert "does not match its recorded digest" in body["reason"]
    assert len(body["expected_digest"]) == 64
    assert len(body["observed_digest"]) == 64
    assert body["expected_digest"] != body["observed_digest"]


def test_the_genesis_count_is_reported_separately_and_is_not_a_break(client):
    with sqlite3.connect(ledger.LEDGER_DB_FILE) as con:
        ledger.ensure_contribution_schema(con)
        con.executemany(
            "INSERT INTO contributions (contribution_id, contributor, "
            "contribution_type, points, task, details, basis, "
            "points_are_monetary, created_at, entry_index, previous_digest, "
            "entry_digest) VALUES (?, ?, 'compute', 5, 'compute_contribution', "
            "'', 'pre_chain', 0, ?, NULL, NULL, NULL)",
            [(f"legacy:{i}", f"old-{i}", 100.0 + i) for i in range(3)],
        )
        con.commit()
    _seed_chain(2)
    ledger.reset_ledger_chain_cache()

    body = client.get("/v1/operator/ledger-chain").json()
    assert body["ok"] is True, "the genesis boundary is being reported as a break"
    assert body["genesis_unchained_entries"] == 3
    assert body["chained_entries"] == 2
    assert body["break_at_index"] is None


# ── The walk is never shortened ──────────────────────────────────────


def test_the_walk_reads_every_chained_entry_every_time_it_walks(client, monkeypatch):
    """No checkpoint, no prefix, no "verified up to index N".

    Instrumented rather than argued: the digest function is counted, and a walk
    of an N-entry chain must recompute N digests — starting at index 0 — however
    many walks came before it. A cache that remembered a verified prefix would
    show up here immediately as a short count.
    """
    _seed_chain(6)

    calls: list[int] = []
    original = ledger.chain_entry_digest

    def counting(*, entry_index, previous_digest, content):
        calls.append(entry_index)
        return original(
            entry_index=entry_index, previous_digest=previous_digest, content=content
        )

    monkeypatch.setattr(ledger, "chain_entry_digest", counting)

    for walk in range(3):
        calls.clear()
        client.get("/v1/operator/ledger-chain?fresh=1")
        assert calls == list(range(6)), (
            f"walk {walk} recomputed {calls}, not every index from 0. A walk "
            "that skips a prefix is blind to the rewrite of already-walked "
            "entries, which is the only thing this check exists to detect."
        )


def test_the_cache_bounds_frequency_and_never_stores_a_partial_walk(client):
    """One complete verdict, reused for a short TTL, served with its own age."""
    _seed_chain(3)
    first = client.get("/v1/operator/ledger-chain").json()
    second = client.get("/v1/operator/ledger-chain").json()

    assert second["walked_at"] == first["walked_at"], (
        "the walk re-ran inside its own TTL, so the TTL bounds nothing"
    )
    assert second["walk_age_seconds"] >= first["walk_age_seconds"]
    for body in (first, second):
        assert body["walk_is_complete"] is True
        assert body["chained_entries"] == 3


def test_fresh_forces_a_new_complete_walk(client):
    _seed_chain(3)
    cached = client.get("/v1/operator/ledger-chain").json()
    forced = client.get("/v1/operator/ledger-chain?fresh=1").json()

    assert forced["walked_at"] > cached["walked_at"], (
        "?fresh=1 did not walk again, so an operator has no way to force one"
    )
    assert forced["chained_entries"] == cached["chained_entries"]


def test_an_expired_ttl_walks_again_without_being_asked(client):
    """The age on screen has an upper bound, and it is the TTL."""
    _seed_chain(3)
    first = ledger.walk_ledger_chain(ledger.LEDGER_DB_FILE)
    later = ledger.walk_ledger_chain(
        ledger.LEDGER_DB_FILE, now=first.walked_at + ledger.LEDGER_CHAIN_WALK_TTL_SECONDS + 1
    )
    assert later.walked_at != first.walked_at


def test_the_projected_ledger_is_left_alone(client):
    """One way to walk the chain, because a second is a second thing that can
    disagree with the first. `/ledger` still projects entries without the chain
    columns, and that is deliberate."""
    _seed_chain(2)
    entries = client.get("/ledger").json()["entries"]
    assert entries
    for entry in entries:
        for column in ("entry_index", "previous_digest", "entry_digest"):
            assert column not in entry, (
                f"/ledger now projects {column}, which lets a client walk the "
                "chain itself and disagree with the endpoint that walks it"
            )


# ── Neither response may carry content ───────────────────────────────


def test_neither_response_can_carry_a_prompt_output_or_artifact_content(client):
    """A sentinel through a real run, then both responses are grepped for it.

    It is placed in every channel a leak could plausibly come from: the task
    text, the deliverable's own bytes, the accepted receipt's output column,
    and two of the ledger's *chained* columns — `contributor` and `basis` —
    which the chain digest genuinely covers. The chain response still cannot
    carry them, because it reports an index, an entry ID and two digests, and
    that is the claim being checked.
    """
    _publish(
        task=f"Write a parser for {SENTINEL} records",
        code=f"SECRET = {SENTINEL!r}\n",
        output=f"here is the answer: {SENTINEL}",
    )
    ledger.log_contribution(
        f"node-{SENTINEL}",
        "compute",
        5,
        task="compute_contribution",
        details=SENTINEL,
        basis=SENTINEL,
        contribution_id="contribution:0000000000000000",
        attempt_id="attempt-0000",
    )
    ledger.reset_ledger_chain_cache()

    # The sentinel really did reach the chained columns, or this test proves
    # nothing. Verified before the responses are read, not assumed.
    with sqlite3.connect(ledger.LEDGER_DB_FILE) as con:
        row = con.execute(
            "SELECT contributor, basis FROM contributions WHERE entry_index = 0"
        ).fetchone()
    assert row is not None and SENTINEL in row[0] and SENTINEL in row[1], (
        "the sentinel never reached a chained column, so grepping the response "
        "for it would have passed vacuously"
    )
    # And it really is in the artifacts the envelope was sealed over.
    assert SENTINEL in (Path("output") / RUN / "code" / "main.py").read_text(
        encoding="utf-8"
    )

    envelope = client.get(f"/v1/executions/{EXECUTION}/provenance")
    chain = client.get("/v1/operator/ledger-chain")
    assert envelope.status_code == 200 and chain.status_code == 200

    for name, response in (("provenance", envelope), ("ledger-chain", chain)):
        assert SENTINEL not in response.text, (
            f"the {name} response carries content from the run. Neither route "
            "may return a prompt, an output, a credential or an artifact's "
            "contents."
        )
