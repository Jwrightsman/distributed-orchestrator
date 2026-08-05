"""Run the eval set through the real pipeline and score every result.

This is the instrument for SPRINT_PHASE2 §1: it produces the number that
prompt changes are judged against. Nothing here is allowed to be kinder than
the shipping pipeline — it calls `run_pipeline` exactly as the CLI does and
reuses the production file checker.

Typical use:

    python evals/run_evals.py                      # full set, real inference
    python evals/run_evals.py --only web_app       # one category
    python evals/run_evals.py --limit 5            # smoke test
    python evals/run_evals.py --resume <run_id>    # continue an interrupted run
    python evals/run_evals.py --fake               # plumbing self-test, no model

A full run on CPU hardware takes hours — every prompt is written to disk as it
finishes, so an interrupted run resumes without losing work.

WARNING: scoring executes model-generated code in a subprocess. It runs in a
scratch directory with a scrubbed environment and a hard timeout, which is a
speed bump rather than a sandbox. Use --no-exec for prompt sets you do not
trust.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import httpx

EVALS_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVALS_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

import config  # noqa: E402
import ollama_client  # noqa: E402
from extract import extract_code_files  # noqa: E402
from orchestrator import run_pipeline  # noqa: E402

from scoring import (  # noqa: E402
    build_judge_prompt,
    check_keywords,
    check_parses,
    execute_artifacts,
    is_success,
    matches_expected_artifact,
    parse_judge_score,
    render_markdown,
    summarize,
)

PROMPTS_FILE = EVALS_DIR / "prompts.json"
RESULTS_DIR = EVALS_DIR / "results"


def load_prompts() -> list[dict]:
    data = json.loads(PROMPTS_FILE.read_text(encoding="utf-8"))
    return data["prompts"]


def select_prompts(prompts: list[dict], args) -> list[dict]:
    chosen = prompts
    if args.only:
        chosen = [p for p in chosen if p.get("category") == args.only]
    if args.id:
        wanted = set(args.id)
        chosen = [p for p in chosen if p["id"] in wanted]
    if args.limit:
        chosen = chosen[: args.limit]
    return chosen


async def judge(task: str, deliverable: str) -> int | None:
    """Ask the model whether the deliverable satisfies the request (1-5)."""
    if not deliverable.strip():
        return 1
    prompt = build_judge_prompt(task, deliverable)
    for _ in range(2):
        try:
            response = await ollama_client.generate(
                prompt,
                system="You are a strict grader. Reply with a single digit 1-5 and nothing else.",
                role="reviewer",
            )
        except Exception:
            return None
        score = parse_judge_score(response)
        if score is not None:
            return score
    return None


async def run_remote(task: str, args) -> dict:
    """Pitch to a running orchestrator and return a run_pipeline-shaped result.

    Lets the eval set run on whatever machine has the model — a 24/7 server, a
    spare desktop — instead of pinning a laptop for the whole run. The
    deliverable comes back as text and is re-extracted locally, so scoring is
    identical either way.
    """
    headers = {"X-Pitch-Key": args.pitch_key} if args.pitch_key else {}
    base = args.orchestrator.rstrip("/")

    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=60) as client:
        resp = await client.post("/pitch/async", json={"task": task})
        if resp.status_code != 200:
            raise RuntimeError(f"pitch rejected: HTTP {resp.status_code} {resp.text[:200]}")
        job_id = resp.json()["job_id"]

        deadline = time.time() + args.remote_timeout
        while time.time() < deadline:
            job = (await client.get(f"/jobs/{job_id}")).json()
            status = job.get("status")
            if status == "failed":
                raise RuntimeError(job.get("error") or "job failed")
            if status == "complete":
                break
            await asyncio.sleep(args.poll_interval)
        else:
            raise RuntimeError(f"job did not finish within {args.remote_timeout}s")

        timestamp = PurePosixPath(str(job.get("project_dir") or "")).name
        if not timestamp:
            raise RuntimeError("job completed without a project_dir")
        detail = (await client.get(f"/history/{timestamp}")).json()

    return {
        "final_output": detail.get("final_output", ""),
        "review": detail.get("review", ""),
        "plan": detail.get("plan", []),
        "rating": detail.get("rating"),
        "project_dir": f"{base}/history/{timestamp}",
        "code_problems": detail.get("code_problems", []),
        "_remote_timestamp": timestamp,
    }


def extract_remote_files(result: dict, run_dir: Path, prompt_id: str) -> list[str]:
    """Re-extract the remote deliverable locally so it can be parsed and run."""
    target = run_dir / "artifacts" / prompt_id
    target.mkdir(parents=True, exist_ok=True)
    text = result.get("final_output") or result.get("review", "")
    return [str(p) for p in extract_code_files(text, target)]


async def run_one(prompt: dict, args, run_dir: Path) -> dict:
    """Run a single pitch end-to-end and score it."""
    expect = prompt.get("expect", {})
    record: dict = {
        "id": prompt["id"],
        "category": prompt.get("category", "uncategorized"),
        "task": prompt["task"],
        "expected_artifact": expect.get("artifact", "any"),
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    start = time.time()
    try:
        if args.orchestrator:
            result = await run_remote(prompt["task"], args)
        else:
            result = await run_pipeline(prompt["task"])
    except Exception as e:
        record.update(
            {
                "error": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc()[-1500:],
                "seconds": round(time.time() - start, 1),
                "extracted": False,
                "parses": False,
                "executes": False,
                "artifact_match": False,
                "keywords_ok": False,
                "judge_score": None,
                "code_files": [],
                "success": False,
            }
        )
        return record

    seconds = round(time.time() - start, 1)
    if args.orchestrator:
        code_files = extract_remote_files(result, run_dir, prompt["id"])
    else:
        code_files = [str(f) for f in result.get("code_files", [])]

    parses, problems = check_parses(code_files)
    keywords_ok, missing = check_keywords(code_files, expect.get("keywords", []))
    artifact_match = matches_expected_artifact(code_files, expect.get("artifact", "any"))

    if args.no_exec or not code_files:
        exec_result = {
            "ok": False,
            "outcome": "skipped" if args.no_exec else "no_files",
            "detail": "",
        }
    else:
        exec_result = await asyncio.to_thread(execute_artifacts, code_files, args.exec_timeout)

    judge_score = None if args.no_judge else await judge(
        prompt["task"], result.get("final_output") or result.get("review", "")
    )

    record.update(
        {
            "seconds": seconds,
            "subtask_count": len(result.get("plan", [])),
            "rating": result.get("rating"),
            "project_dir": result.get("project_dir"),
            "code_files": code_files,
            "extracted": bool(code_files),
            "parses": parses,
            "problems": problems,
            "keywords_ok": keywords_ok,
            "missing_keywords": missing,
            "artifact_match": artifact_match,
            "executes": exec_result["ok"],
            "exec_outcome": exec_result["outcome"],
            "exec_detail": exec_result["detail"],
            "judge_score": judge_score,
            "pipeline_code_problems": result.get("code_problems", []),
        }
    )
    record["success"] = is_success(record, require_judge=not args.no_judge)
    return record


def write_outputs(run_dir: Path, records: list[dict], meta: dict) -> dict:
    """Refresh summary.json/summary.md — called after every prompt."""
    summary = summarize(records)
    (run_dir / "summary.json").write_text(
        json.dumps({"meta": meta, "summary": summary}, indent=2), encoding="utf-8"
    )
    (run_dir / "summary.md").write_text(
        render_markdown(summary, records, meta), encoding="utf-8"
    )
    return summary


def load_existing(run_dir: Path) -> list[dict]:
    path = run_dir / "results.jsonl"
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def install_fake_backend() -> None:
    """Deterministic stand-in for the model, so the harness can be tested.

    Returns plausible planner JSON, a builder file and a reviewer assembly, in
    the same shapes the real pipeline parses. Exercises every step except model
    quality — which is exactly what a plumbing self-test should do.
    """
    fake_html = (
        "<!DOCTYPE html>\n<html><head><title>Fake</title><style>body{margin:0}</style>"
        "</head><body><canvas id='c'></canvas><div id='score'>0</div>"
        "<script>document.addEventListener('keydown',function(e){});"
        "let s=0;setInterval(function(){s++;},1000);</script></body></html>"
    )

    # Dispatch on the caller's system prompt, not on keywords in the task —
    # matching loose words made tasks containing "JSON" look like planner calls
    # and "previewer" look like a reviewer call.
    from orchestrator import BUILDER_SYSTEM, PLANNER_SYSTEM, REVIEWER_SYSTEM, REVISER_SYSTEM

    async def fake_generate(prompt, system="", model=None, role=None, format=None):
        if system == PLANNER_SYSTEM:
            return json.dumps(
                [
                    {
                        "id": 1,
                        "title": "Build the page",
                        "prompt": "Write the HTML shell and styling.",
                        "depends_on": [],
                    },
                    {
                        "id": 2,
                        "title": "Add controls",
                        "prompt": "Wire up the keyboard handlers and scoring.",
                        "depends_on": [1],
                    },
                ]
            )
        if system in (REVIEWER_SYSTEM, REVISER_SYSTEM):
            return (
                "RATING: PASS\n\nISSUES:\nNone\n\n"
                "## Final Assembled Output\n\n"
                f"```html\n{fake_html}\n```\n"
            )
        if system == BUILDER_SYSTEM:
            return f"```html\n{fake_html}\n```"
        # Anything else is the eval's own judge call.
        return "5"

    ollama_client.generate = fake_generate
    import orchestrator

    orchestrator.generate = fake_generate

    async def fake_stream(*a, **k):
        yield ""

    orchestrator.generate_stream = fake_stream


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run the orchestrator eval set.")
    parser.add_argument("--only", help="restrict to one category")
    parser.add_argument("--id", action="append", help="run specific prompt id(s)")
    parser.add_argument("--limit", type=int, help="cap the number of prompts")
    parser.add_argument("--resume", help="continue an existing run id")
    parser.add_argument("--retry-failed", action="store_true",
                        help="with --resume, also re-run prompts that errored")
    parser.add_argument("--label", default="", help="label recorded in the summary")
    parser.add_argument("--no-exec", action="store_true", help="skip executing generated code")
    parser.add_argument("--no-judge", action="store_true", help="skip the model judgment step")
    parser.add_argument("--exec-timeout", type=int, default=15)
    parser.add_argument("--fake", action="store_true", help="stubbed model — plumbing self-test")
    parser.add_argument(
        "--orchestrator",
        help="run pitches on a running orchestrator (e.g. http://1.2.3.4:8000) "
             "instead of locally — lets the eval set run on whatever machine has the model",
    )
    parser.add_argument("--pitch-key", default="", help="X-Pitch-Key for --orchestrator")
    parser.add_argument("--remote-timeout", type=int, default=3600,
                        help="seconds to wait for one remote job (default 1h)")
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--concurrency", type=int, default=1,
                        help="pitches in flight at once. Leave at 1 for a single local "
                             "Ollama; raise it only when the far end can genuinely "
                             "serve parallel requests")
    args = parser.parse_args()

    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    if args.pitch_key and not args.orchestrator:
        parser.error("--pitch-key only applies with --orchestrator")

    if args.fake:
        install_fake_backend()

    prompts = select_prompts(load_prompts(), args)
    if not prompts:
        print("No prompts matched the selection.")
        return 1

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    run_id = args.resume or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_dir = RESULTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    records = load_existing(run_dir) if args.resume else []
    if args.resume and args.retry_failed:
        # A prompt that died on a network blip three hours in should not be
        # silently dropped from the run. Forget those records and re-run them.
        retrying = [r["id"] for r in records if r.get("error")]
        records = [r for r in records if not r.get("error")]
        with (run_dir / "results.jsonl").open("w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")
        if retrying:
            print(f"Retrying {len(retrying)} previously failed prompt(s): {', '.join(retrying)}")

    done_ids = {r["id"] for r in records}
    todo = [p for p in prompts if p["id"] not in done_ids]

    model = config.get().get("model", "?")
    if args.fake:
        where = "fake-backend"
    elif args.orchestrator:
        where = f"remote {args.orchestrator}"
    else:
        where = "local"
    meta = {
        "run_id": run_id,
        "model": "fake-backend" if args.fake else model,
        "mode": "fake" if args.fake else "real",
        "executed_on": where,
        "label": args.label,
        "prompt_count": len(prompts),
        "exec_enabled": not args.no_exec,
        "judge_enabled": not args.no_judge,
        "concurrency": args.concurrency,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    print(f"Run {run_id} — {len(todo)} prompt(s) to go ({len(done_ids)} already done), on {where}")
    if not args.fake:
        print("Expect hours on CPU. Every prompt is flushed as it finishes, so "
              f"interrupting is safe: resume with --resume {run_id}")
    if args.no_judge:
        print("NOTE: --no-judge — mechanical checks only. Do not compare this "
              "score against a judged run.")

    write_lock = asyncio.Lock()
    limiter = asyncio.Semaphore(args.concurrency)
    done_count = 0

    async def worker(index: int, prompt: dict) -> None:
        nonlocal done_count
        async with limiter:
            record = await run_one(prompt, args, run_dir)
        async with write_lock:
            records.append(record)
            with (run_dir / "results.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
            summary = write_outputs(run_dir, records, meta)
            done_count += 1
            verdict = "PASS" if record["success"] else "fail"
            detail = record.get("error") or record.get("exec_outcome", "")
            print(
                f"[{done_count}/{len(todo)}] {record['id']} … {verdict} "
                f"({record.get('seconds', 0)}s, judge {record.get('judge_score')}, "
                f"{detail}) — running {summary['success_rate']:.0%}",
                flush=True,
            )

    await asyncio.gather(*(worker(i, p) for i, p in enumerate(todo, 1)))

    summary = write_outputs(run_dir, records, meta)
    print()
    print(f"Success rate: {summary['success_rate']:.0%} ({summary['success']}/{summary['total']})")
    if args.no_judge:
        print("(mechanical checks only — --no-judge was set)")
    print(f"Summary: {run_dir / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
