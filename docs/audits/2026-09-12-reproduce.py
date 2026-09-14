"""Offline audit probes. No model, network, coordinator, or worker is started.

The only executed artifact is a fixed, authored one-line sentinel in a temporary
directory. These probes describe defects at 418a44d; after fixes their observed
values should change. They are evidence, not a replacement regression suite.
"""

import ast
import asyncio
import dataclasses
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "evals")]


async def probes():
    import corpus
    import memory
    import orchestrator
    import run_evals
    from execution.contracts import ExecutionRequestV1
    from scripts import eval_study_summary as summary

    results = {}
    with tempfile.TemporaryDirectory(prefix="mycelium_audit_") as directory:
        temporary = Path(directory)
        old_cwd = Path.cwd()
        os.chdir(temporary)
        try:
            projects = temporary / "projects"
            projects.mkdir()
            outside = temporary / "outside"
            outside.mkdir()
            (outside / "memory.md").write_text("AUDIT_OUTSIDE_PROJECT_ROOT", encoding="utf-8")
            (outside / "meta.json").write_text(json.dumps({"iteration_count": 0}), encoding="utf-8")
            with patch.object(memory, "PROJECTS_DIR", projects):
                request = ExecutionRequestV1(task="audit", project_id="../outside", strategy="dag")
                read = memory.get_memory_context(request.project_id)
                memory.add_iteration(request.project_id, {"project_dir": str(temporary / "nonexistent")}, "audit")
                results["project_traversal"] = {
                    "accepted_project_id": request.project_id,
                    "outside_memory_read": read == "AUDIT_OUTSIDE_PROJECT_ROOT",
                    "outside_metadata_modified": json.loads((outside / "meta.json").read_text())["iteration_count"] == 1,
                }

            sentinel = temporary / "authored_sentinel.txt"
            artifact = temporary / "main.py"
            artifact.write_text("from pathlib import Path\nPath(" + repr(str(sentinel)) + ").write_text('audit')\n", encoding="utf-8")
            item = corpus.CorpusItem("audit", "script", "algorithmic_kernel", "audit", "taxonomy", "development", {"artifact": "python"})
            args = SimpleNamespace(orchestrator=None, no_exec=True, no_judge=True, exec_timeout=5)
            fake = {"code_files": [str(artifact)], "final_output": "audit", "plan": []}
            with patch.object(run_evals, "run_pipeline", AsyncMock(return_value=fake)):
                record = await run_evals.run_one(item, args, temporary)
            results["no_exec"] = {"reported_legacy_outcome": record["exec_outcome"], "artifact_executed": sentinel.exists()}

            changed = dataclasses.replace(item, expect={"artifact": "python", "checks": [{"kind": "stdout_contains", "substrings": ["different"]}]})
            results["corpus_digest"] = {"changed_checks_same_digest": corpus.corpus_digest([item]) == corpus.corpus_digest([changed])}

            rows = [{"item_id": "only_observed_item", "arm": a, "replicate": 0, "graded": True, "passed": True} for a in ["a", "b"]]
            summary.check_complete(rows, ["a", "b"])
            replicas = [dict(rows[0], replicate=0, passed=True), dict(rows[0], replicate=1, passed=False)]
            results["study_summary"] = {
                "one_item_prefix_accepted_without_planned_manifest": True,
                "replicates_forward": summary.outcomes_for(replicas, "a"),
                "replicates_reversed": summary.outcomes_for(list(reversed(replicas)), "a"),
            }

            # Execute the actual pipeline through revision, cutting off its save
            # phase with a return. Planning, building, reviewing and accounting
            # are mocked; concurrency and verdict control flow are unchanged.
            tree = ast.parse((ROOT / "orchestrator.py").read_text(encoding="utf-8"))
            function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_pipeline")
            stop = next(i for i, n in enumerate(function.body) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name) and n.value.func.id == "make_run_dir")
            function.body = function.body[:stop] + ast.parse("return {'rating': rating, 'revision': revision}").body
            ns = dict(vars(orchestrator))
            ns["log_contribution"] = lambda *a, **k: None
            tasks = [{"id": 1, "title": "one", "prompt": "one", "depends_on": []}]
            ns["plan"] = AsyncMock(return_value=tasks)
            broken = "This deliverable still contains the exact known defect. " * 4
            ns["review"] = AsyncMock(return_value="## Quality Rating\nFAIL\n\n## Issues Found\nKnown defect\n\n## Final Assembled Output\n" + broken)
            ns["revise"] = AsyncMock(return_value=broken)
            exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), "<audit-pipeline-prefix>", "exec"), ns)
            verdict = await ns["run_pipeline"]("audit", build_fn=AsyncMock(return_value=broken))
            results["review_rating"] = {"unchanged_bad_revision_rating": verdict["rating"], "missing_rating": orchestrator._extract_rating("unstructured reviewer reply")}

            ns["plan"] = AsyncMock(return_value=tasks + [{"id": 2, "title": "two", "prompt": "two", "depends_on": []}])
            started = asyncio.Event()
            release = asyncio.Event()
            finished = asyncio.Event()

            async def failing_builder(task, context):
                if task["id"] == 1:
                    await started.wait()
                    raise RuntimeError("authored audit failure")
                started.set()
                await release.wait()
                finished.set()
                return "audit sibling"

            try:
                await ns["run_pipeline"]("audit", build_fn=failing_builder)
            except RuntimeError:
                pass
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
            release.set()
            await asyncio.wait_for(finished.wait(), 2)
            await asyncio.sleep(0)
            results["builder_failure"] = {"pending_siblings_after_failure": len(pending), "sibling_completed_after_pipeline_failed": finished.is_set()}
        finally:
            os.chdir(old_cwd)
    return results


if __name__ == "__main__":
    print(json.dumps(asyncio.run(probes()), indent=2))
