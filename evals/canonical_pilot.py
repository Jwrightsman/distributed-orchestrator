"""Bounded experiment composition over the canonical service, never a strategy.

This module does not discover/start runtimes or execute generated artifacts.
Activation requires an externally observed identity and a separately approved
checker. The CLI exposes offline preparation only.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import math
from pathlib import Path
import random
import re
import subprocess
import threading
import time
import uuid

from evals import corpus, grading, runrecord
from execution.call_capture import CallCollector, capture_calls
from execution.contracts import ExecutionRequestV1
from execution.experiment_scope import single_attempt_calls
from execution.validator_process import ValidatorProcessExecutor
from execution.validators import ValidatorRegistry

ITEM_IDS = (
    "extract-log-errors", "extract-markdown-headings", "extract-jsonl-fields",
    "extract-table-from-text", "transform-csv-to-jsonl", "transform-dedupe-orders",
    "transform-word-frequencies", "transform-merge-two-files", "classify-orders-by-size",
    "classify-sentiment-keywords", "classify-readings-anomalies", "classify-sales-performance",
)
ARMS = ("direct", "direct-with-repair", "dag")
BUDGET = {"cells": 36, "generated_tokens": 147456, "inference_seconds": 21600,
          "checker_seconds": 3600, "cell_tokens": 4096, "cell_inference_seconds": 600,
          "cell_elapsed_seconds": 600}
SETTINGS = {"context_tokens": 8192, "temperature": 0, "seed": 20260913,
            "planner_attempts": 1, "builder_attempts": 1, "schema_retry": False,
            "think": False, "maximum_subtasks": 2, "revision_enabled": False,
            "dag_unused_reserve": 512, "runtime_calls_serialized": True}
REQUIRED_IDENTITIES = ("model_digest", "runtime_version", "runtime_binary_sha256",
                       "quantization", "hardware_id", "os", "provider_settings_sha256",
                       "acceptance_fixture_sha256", "rubric_sha256", "blinding_map_sha256",
                       "isolation_approval_id", "owner_approval_id")
DIGEST_IDENTITIES = ("model_digest", "runtime_binary_sha256", "provider_settings_sha256",
                     "acceptance_fixture_sha256", "rubric_sha256", "blinding_map_sha256")
PUBLIC_DIAGNOSTICS = {
    "syntax": "The candidate does not satisfy the public syntax requirement.",
    "artifact": "The candidate does not satisfy the requested artifact format.",
    "requirement": "The candidate does not satisfy a stated task requirement.",
}


class PreflightError(ValueError):
    pass


class BudgetStopped(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _is_sha256(value):
    if not isinstance(value, str):
        return False
    return re.fullmatch(r"(?:sha256:)?[0-9a-fA-F]{64}", value.strip()) is not None


def local_identity():
    """Bind actual source bytes, including new adapter files, without inference."""
    root = Path(__file__).resolve().parents[1]
    paths = set(root.glob("*.py"))
    for directory in ("execution", "evals", "scripts", "prompts"):
        paths.update((root / directory).rglob("*.py"))
    paths.update((root / "evals" / "fixtures").rglob("*"))
    paths.add(root / "evals" / "prompts.json")
    files = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(paths) if p.is_file() and "__pycache__" not in p.parts}
    items = corpus.load_corpus()
    import orchestrator
    prompt_identity = {"name": orchestrator.active_prompt_set().name,
                       "sha256": digest([orchestrator.PLANNER_SYSTEM, orchestrator.BUILDER_SYSTEM,
                                         orchestrator.REVIEWER_SYSTEM, orchestrator.REVISER_SYSTEM])}
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root).decode().strip()
    diff_bytes = subprocess.check_output(["git", "diff", "--binary", "HEAD", "--"], cwd=root, stderr=subprocess.DEVNULL)
    return {"measurement_identity_version": "2",
            "measurement_digest": corpus.measurement_digest(items, grader_version=grading.GRADER_VERSION),
            "corpus_digest": corpus.corpus_digest(items),
            "grader_version": grading.GRADER_VERSION,
            "checker_source_sha256": files["evals/grading.py"],
            "repository_commit": head, "tracked_diff_sha256": hashlib.sha256(diff_bytes).hexdigest(),
            "prompt_identity": prompt_identity, "strategy_versions": {"dag": "1", "ensemble": "1"},
            "source_tree_sha256": digest(files), "source_files": files}


def planned_cells():
    rng = random.Random(20260913)
    cells = []
    for item_id in ITEM_IDS:
        arms = list(ARMS)
        rng.shuffle(arms)
        cells.extend({"item_id": item_id, "arm": arm, "replicate": 0} for arm in arms)
    return cells


def prepare_manifest(study_id, identities=None):
    identities = identities or {}
    identity = {**local_identity(), "model_name": "qwen3.5:4b", "provider": "ollama",
                "model_seed_honoured": None,
                **{name: identities.get(name) for name in REQUIRED_IDENTITIES}}
    items = {item.id: item for item in corpus.load_corpus()}
    if any(name not in items or items[name].split != "development" for name in ITEM_IDS):
        raise PreflightError("planned development membership changed")
    manifest = {"version": "1", "study_id": study_id, "item_ids": list(ITEM_IDS),
                "arms": list(ARMS), "replicates": [0], "aggregation": "single_replicate",
                "identity": identity, "settings": dict(SETTINGS), "budget": dict(BUDGET),
                "schedule": planned_cells(), "status": "prepared_not_activated",
                "budget_policy": {"metric": "homogeneous_hardware_seconds_v1",
                                  "hardware_id": identity["hardware_id"], "relative_tolerance": 0.25}}
    manifest["activation_blockers"] = [name for name in REQUIRED_IDENTITIES if not identity[name]]
    return manifest


def validate_activation(manifest, observed):
    expected = prepare_manifest(manifest.get("study_id"), observed)
    for key in ("identity", "settings", "budget", "schedule", "item_ids", "arms",
                "replicates", "aggregation", "budget_policy", "version"):
        if manifest.get(key) != expected[key]:
            raise PreflightError(f"frozen {key} differs from observed preflight")
    if expected["activation_blockers"]:
        raise PreflightError("unresolved identities: " + ", ".join(expected["activation_blockers"]))
    for name in REQUIRED_IDENTITIES:
        value = observed.get(name)
        if not isinstance(value, str) or not value.strip() or value.lower() in {"unknown", "unresolved"}:
            raise PreflightError(f"unavailable identity: {name}")
    for name in DIGEST_IDENTITIES:
        if not _is_sha256(observed.get(name)):
            raise PreflightError(f"identity is not a SHA-256 digest: {name}")
    runrecord.validate_manifest(manifest)


def repair_task(task, candidate, diagnostic):
    if diagnostic not in PUBLIC_DIAGNOSTICS:
        raise PreflightError("repair diagnostic must be an allowlisted public code")
    text = f"{task}\n\nRepair this candidate:\n{candidate}\n\n{PUBLIC_DIAGNOSTICS[diagnostic]}"
    if len(text) > 1000:
        raise PreflightError("unsupported repair: complete input exceeds canonical 1000-character task bound")
    return text


class BudgetLedger:
    """Serial runtime admission with conservative reservations, actual cost separate."""

    def __init__(self, journal=None, clock=time.monotonic):
        self.clock = clock
        self.journal = journal
        self.stopped = None
        self.tokens = 0
        self.inference_reserved_seconds = 0.0
        self.checker_seconds = 0.0
        self.checker_capture_complete = True
        self.cells = []
        self.current = None
        self.lock = asyncio.Lock()
        self.checker_lock = threading.Lock()

    def event(self, kind, **data):
        if self.journal:
            import os
            with Path(self.journal).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"event": kind, **data}, allow_nan=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def ensure(self):
        if self.stopped:
            raise BudgetStopped(self.stopped)
        if self.current and self.clock() >= self.current["deadline"]:
            self.stopped = "cell elapsed deadline exhausted"
            raise BudgetStopped(self.stopped)

    def begin_cell(self, cell):
        self.ensure()
        if self.current is not None or len(self.cells) >= 36:
            raise BudgetStopped("cell already active or planned cells exhausted")
        if cell != planned_cells()[len(self.cells)]:
            raise PreflightError("cell differs from frozen schedule; no replacements or retries")
        self.current = {**cell, "start": self.clock(), "deadline": self.clock() + 600,
                        "tokens": 0, "reserved_tokens": 0, "inference_reserved_seconds": 0.0,
                        "calls_by_role": {}, "checker_start_seconds": self.checker_seconds,
                        "checker_capture_complete_start": self.checker_capture_complete}
        self.event("cell_started", cell=cell)

    async def before_call(self, record, payload, provider):
        await self.lock.acquire()
        try:
            self.ensure()
            cell = self.current
            if cell is None or provider != "ollama":
                raise PreflightError("pilot permits only local Ollama calls inside a planned cell")
            role = record["role"]
            limits = {"planner": (1, 512), "builder": (2, 768), "reviewer": (1, 1536)} if cell["arm"] == "dag" else {"direct": (2 if cell["arm"] == "direct-with-repair" else 1, 2048 if cell["arm"] == "direct-with-repair" else 4096)}
            count = cell["calls_by_role"].get(role, 0)
            if role not in limits or count >= limits[role][0]:
                raise BudgetStopped("unplanned role, retry, or repair call")
            cap = limits[role][1]
            if self.tokens + cap > 147456 or cell["reserved_tokens"] + cap > 4096:
                raise BudgetStopped("generated token reservation exhausted")
            remaining = min(cell["deadline"] - self.clock(),
                            600 - cell["inference_reserved_seconds"],
                            21600 - self.inference_reserved_seconds)
            if remaining <= 0:
                raise BudgetStopped("inference hardware-time budget exhausted")
            cell["reserved_tokens"] += cap
            cell["calls_by_role"][role] = count + 1
            record.update(timeout_seconds=remaining, token_ceiling=cap)
            payload["options"] = {"num_ctx": 8192, "temperature": 0, "seed": 20260913, "num_predict": cap}
            payload["think"] = False
            self.event("call_admitted", call=record)
        except BaseException as exc:
            if isinstance(exc, (BudgetStopped, PreflightError)):
                self.stopped = str(exc)
            self.lock.release()
            raise

    async def after_call(self, record):
        try:
            cell = self.current
            tokens = record["output_tokens"]
            seconds = record["inference_seconds"]
            if tokens is None or seconds is None or record["input_tokens"] is None or not record["termination_confirmed"]:
                self.stopped = "runtime cost or termination unknown"
            else:
                self.tokens += tokens
                cell["tokens"] += tokens
                # Runtime-reported work remains separate from a conservative
                # admission charge which includes transport/queue wall time.
                charge = max(seconds, record["end_monotonic"] - record["start_monotonic"])
                self.inference_reserved_seconds += charge
                cell["inference_reserved_seconds"] += charge
                if tokens > record["token_ceiling"] or cell["tokens"] > 4096 or self.tokens > 147456 or cell["inference_reserved_seconds"] > 600 or self.inference_reserved_seconds > 21600:
                    self.stopped = "runtime exceeded frozen budget or per-call ceiling"
            self.event("call_finished", call=record)
        finally:
            self.lock.release()

    def finish_cell(self, outcome):
        cell = self.current
        if cell is None:
            raise RuntimeError("no active cell")
        cell = {**cell, "outcome": outcome, "elapsed_seconds": self.clock() - cell["start"]}
        if self.clock() >= cell["deadline"]:
            self.stopped = "cell elapsed deadline exhausted"
        self.cells.append(cell)
        self.current = None
        self.event("cell_finished", cell=cell)


_checking = ContextVar("pilot_checker_active", default=False)


@contextmanager
def checker_work(ledger, kwargs):
    if _checking.get():
        yield
        return
    with ledger.checker_lock:
        ledger.ensure()
        start = ledger.clock()
        remaining = 3600 - ledger.checker_seconds
        if remaining <= 0:
            ledger.stopped = "checker budget exhausted"
            raise BudgetStopped(ledger.stopped)
        deadlines = [start + remaining]
        if ledger.current:
            deadlines.append(ledger.current["deadline"])
        if kwargs.get("deadline_monotonic") is not None:
            deadlines.append(kwargs["deadline_monotonic"])
        kwargs["deadline_monotonic"] = min(deadlines)
        token = _checking.set(True)
        try:
            yield
        except BaseException:
            ledger.checker_capture_complete = False
            if not ledger.stopped:
                ledger.stopped = "checker failed or termination unknown"
            raise
        finally:
            elapsed = ledger.clock() - start
            ledger.checker_seconds += elapsed
            if ledger.clock() >= kwargs["deadline_monotonic"] and not ledger.stopped:
                ledger.stopped = "checker deadline exhausted"
            ledger.event(
                "checker_finished",
                seconds_observed=elapsed,
                seconds_cumulative_observed=ledger.checker_seconds,
                capture_complete=ledger.checker_capture_complete,
                stop_reason=ledger.stopped,
            )
            _checking.reset(token)


class PilotProcessExecutor(ValidatorProcessExecutor):
    def __init__(self, ledger, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ledger = ledger

    def execute(self, **kwargs):
        with checker_work(self.ledger, kwargs):
            return super().execute(**kwargs)

    def record_cleanup_failure(self):
        self.ledger.stopped = "checker termination unknown"
        self.ledger.checker_capture_complete = False
        super().record_cleanup_failure()


class PilotValidators(ValidatorRegistry):
    def __init__(self, ledger):
        super().__init__(process_executor=PilotProcessExecutor(ledger))
        for validator in ValidatorRegistry.default()._validators.values():
            self.register(validator)
        self.ledger = ledger

    def validate(self, *args, **kwargs):
        with checker_work(self.ledger, kwargs):
            return super().validate(*args, **kwargs)


class CanonicalPilot:
    def __init__(self, service, manifest, observed_identity, checker, directory, *, warm_cold):
        observed = observed_identity()
        validate_activation(manifest, observed)
        if not callable(warm_cold) and warm_cold not in {"warm", "cold"}:
            raise PreflightError("record observed warm/cold status")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        if (self.directory / "pilot-events.jsonl").exists():
            raise PreflightError("existing pilot journal: interrupted pilots are not resumed")
        runrecord.write_manifest(self.directory, manifest)
        self.manifest = json.loads(json.dumps(manifest))
        self.observed_identity = observed_identity
        self.service = service
        self.checker = checker
        self.ledger = BudgetLedger(self.directory / "pilot-events.jsonl")
        self.service.validators = PilotValidators(self.ledger)
        self.collector = CallCollector(observed["hardware_id"], warm_cold,
                                       before_dispatch=self.before_call,
                                       after_finish=self.ledger.after_call)

    async def before_call(self, record, payload, provider):
        validate_activation(self.manifest, self.observed_identity())
        if payload.get("model") != "qwen3.5:4b":
            raise PreflightError("runtime model tag differs from frozen model")
        if record.get("warm_cold") not in {"warm", "cold"}:
            raise PreflightError("runtime warm/cold observation is unavailable")
        await self.ledger.before_call(record, payload, provider)

    async def run_cell(self, cell):
        validate_activation(self.manifest, self.observed_identity())
        self.ledger.begin_cell(cell)
        start_call = len(self.collector.records)
        item = next(item for item in corpus.load_corpus() if item.id == cell["item_id"])
        outcome = "operational_failure"
        executions = []
        candidate = ""
        accepted = None
        try:
            task = item.task
            for attempt in range(2 if cell["arm"] == "direct-with-repair" else 1):
                self.ledger.ensure()
                remaining = self.ledger.current["deadline"] - self.ledger.clock()
                if remaining < 1:
                    raise BudgetStopped("less than one canonical deadline second remains")
                request = ExecutionRequestV1(
                    task=task, strategy="dag" if cell["arm"] == "dag" else "direct",
                    strategy_options={"kind": "dag", "maximum_subtasks": 2,
                                      "review_enabled": True, "revision_enabled": False} if cell["arm"] == "dag" else {"kind": "ensemble", "candidates": 1, "concurrency": 1},
                    placement="local", confidentiality="local_only", timeout_seconds=math.floor(remaining),
                    output_contract={"kind": "code", "format": item.artifact},
                )
                execution_id = uuid.uuid4().hex
                with single_attempt_calls(), capture_calls(self.collector, execution_id=execution_id,
                                                          role="direct", request_attempt=attempt,
                                                          attempt_id=None):
                    async with asyncio.timeout(remaining):
                        run = await self.service.execute(request, execution_id=execution_id)
                executions.append(execution_id)
                if run.result.lifecycle_status != "completed":
                    break
                candidate = run.legacy_payload.get("final_output") or ""
                self.ledger.ensure()
                kwargs = {}
                # Checker is an explicitly supplied, bounded, isolated endpoint.
                # Only its boolean and public diagnostic enum cross this boundary.
                try:
                    with checker_work(self.ledger, kwargs):
                        async with asyncio.timeout_at(kwargs["deadline_monotonic"]):
                            accepted, diagnostic = await self.checker(item.id, candidate, **kwargs)
                except BaseException:
                    if not self.ledger.stopped:
                        self.ledger.stopped = "checker failed or termination unknown"
                    raise
                if type(accepted) is not bool:
                    raise PreflightError("checker did not return a measured boolean")
                if accepted or cell["arm"] != "direct-with-repair" or attempt == 1:
                    outcome = "accepted" if accepted else "rejected"
                    break
                task = repair_task(item.task, candidate, diagnostic)
            if self.collector.dispatch_blocked:
                self.ledger.stopped = "runtime cost or termination unknown"
        except PreflightError:
            outcome = "unsupported_preflight"
            if not self.ledger.stopped:
                self.ledger.stopped = "preflight failed; preserve missing planned cells"
            raise
        except BaseException:
            if not self.ledger.stopped:
                self.ledger.stopped = "cell interrupted; preserve missing planned cells"
            raise
        finally:
            calls = self.collector.records[start_call:]
            complete = bool(calls) and self.collector.cost_capture_complete
            cell_state = self.ledger.current
            if self.ledger.clock() >= cell_state["deadline"] and not self.ledger.stopped:
                self.ledger.stopped = "cell elapsed deadline exhausted"
            checker_seconds_observed = max(
                0.0, self.ledger.checker_seconds - cell_state["checker_start_seconds"]
            )
            checker_complete = bool(
                cell_state["checker_capture_complete_start"]
                and self.ledger.checker_capture_complete
            )
            checker_seconds_total = checker_seconds_observed if checker_complete else None
            cell_state.update(
                checker_seconds_observed=checker_seconds_observed,
                checker_seconds_total=checker_seconds_total,
                checker_capture_complete=checker_complete,
                stop_reason=self.ledger.stopped,
            )
            unit_costs = [{**call, "execution_unit_id": call.get("unit_id"),
                           "unit_id": call["call_id"],
                           "tokens": {"prompt": call["input_tokens"], "completion": call["output_tokens"]}
                           if call["input_tokens"] is not None and call["output_tokens"] is not None else None}
                          for call in calls]
            self.ledger.event("cell_result", cell=cell, executions=executions,
                              outcome=outcome, accepted=accepted, unit_costs=calls,
                              cost_capture_complete=complete,
                              checker_seconds_observed=checker_seconds_observed,
                              checker_seconds_total=checker_seconds_total,
                              checker_seconds_cumulative_observed=self.ledger.checker_seconds,
                              checker_seconds_cumulative_total=(self.ledger.checker_seconds
                                                                if checker_complete else None),
                              checker_capture_complete=checker_complete,
                              stop_reason=self.ledger.stopped,
                              candidate_sha256=hashlib.sha256(candidate.encode()).hexdigest() if candidate else None)
            identity = self.manifest["identity"]
            runrecord.append_run(self.directory, runrecord.RunRecord(
                study_id=self.manifest["study_id"], run_id=uuid.uuid4().hex,
                item_id=item.id, arm=cell["arm"], strategy="dag" if cell["arm"] == "dag" else "direct",
                strategy_config=dict(SETTINGS), corpus_version=corpus.corpus_version(),
                corpus_digest=identity["corpus_digest"], band=item.band_label,
                model=runrecord.ModelIdentity("ollama", "qwen3.5:4b", identity["model_digest"], 0, 20260913),
                descriptor_hash=None, wall_clock_seconds=self.ledger.clock() - self.ledger.current["start"],
                tokens={"prompt": sum(c["input_tokens"] for c in calls), "completion": sum(c["output_tokens"] for c in calls)} if complete else None,
                artifact_sha256=hashlib.sha256(candidate.encode()).hexdigest() if candidate else None,
                artifact_paths=[], grading={"grader_version": grading.GRADER_VERSION, "outcome": outcome},
                graded=outcome in {"accepted", "rejected"}, passed=outcome == "accepted",
                measurement_identity_version="2", measurement_digest=identity["measurement_digest"],
                measurement_series="canonical-development-pilot-v1", unit_costs=unit_costs,
                cost_capture_complete=complete,
                checker_seconds_observed=checker_seconds_observed,
                checker_seconds_total=checker_seconds_total,
                checker_capture_complete=checker_complete,
                operational_stop_reason=self.ledger.stopped,
            ))
            self.ledger.finish_cell(outcome)
        return outcome
