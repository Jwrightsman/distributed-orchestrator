"""Scoring for the eval harness.

Five dimensions per run, deliberately kept mechanical where possible so the
number means something:

  a. extracted   — did the extractor produce any files at all
  b. parses      — do those files parse (reuses the *production* checker in
                   extract.py, so the eval can never be kinder than the pipeline)
  c. executes    — does it actually run: Python in a subprocess, HTML in a
                   headless browser when one is available, static checks otherwise
  d. judged      — reviewer-model rating 1-5 of "does this satisfy the request"
  e. cost        — wall-clock seconds and subtask count (collected by the runner)

`success` (the headline number) requires a, b, c and a judge score >= 4, plus
any keywords the prompt declared. A run that produces beautiful prose and no
runnable file scores zero, which is the entire point.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# Reuse the production checker so eval standards track shipping standards.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from extract import check_code_files  # noqa: E402

EXEC_TIMEOUT = 15
JUDGE_CHAR_BUDGET = 6000
PASS_JUDGE_SCORE = 4


# ── (a)(b) artifact-level checks ────────────────────────────────────────────

def classify_files(code_files: list[str]) -> dict[str, list[str]]:
    """Bucket extracted files by the kind of check they need."""
    buckets: dict[str, list[str]] = {"python": [], "html": [], "other": []}
    for f in code_files:
        suffix = Path(f).suffix.lower()
        if suffix == ".py":
            buckets["python"].append(f)
        elif suffix in (".html", ".htm"):
            buckets["html"].append(f)
        else:
            buckets["other"].append(f)
    return buckets


def check_parses(code_files: list[str]) -> tuple[bool, list[str]]:
    """True when every checkable file is structurally sound."""
    problems = check_code_files(code_files)
    return (not problems), problems


def check_keywords(code_files: list[str], keywords: list[str]) -> tuple[bool, list[str]]:
    """Every declared keyword must appear somewhere in the extracted code."""
    if not keywords:
        return True, []
    blob = ""
    for f in code_files:
        try:
            blob += Path(f).read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
    missing = [k for k in keywords if k.lower() not in blob]
    return (not missing), missing


def matches_expected_artifact(code_files: list[str], artifact: str) -> bool:
    """Did the swarm produce the *kind* of thing that was asked for."""
    if artifact == "any":
        return bool(code_files)
    buckets = classify_files(code_files)
    return bool(buckets.get(artifact))


# ── (c) execution ───────────────────────────────────────────────────────────

_SERVER_HINTS = ("uvicorn.run", "app.run(", "serve_forever", "mainloop()")


def _python_entrypoint(paths: list[str]) -> str | None:
    """Pick the file most likely meant to be run.

    Prefers an explicit __main__ guard, then a module that defines an app or a
    main(), then the largest file — model output rarely says which is which.
    """
    if not paths:
        return None
    scored: list[tuple[int, int, str]] = []
    for p in paths:
        try:
            src = Path(p).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rank = 0
        if "__main__" in src:
            rank = 3
        elif re.search(r"^\s*def main\s*\(", src, re.M):
            rank = 2
        elif any(h in src for h in _SERVER_HINTS):
            rank = 2
        elif re.search(r"^\s*app\s*=\s*FastAPI", src, re.M):
            rank = 1
        scored.append((rank, len(src), p))
    if not scored:
        return None
    scored.sort(reverse=True)
    return scored[0][2]


def _classify_python_failure(stderr: str) -> str:
    if "ModuleNotFoundError" in stderr or "ImportError" in stderr:
        return "missing_dependency"
    if "EOFError" in stderr and "input" in stderr:
        # Ran fine, then asked for interactive input we did not supply.
        return "needs_stdin"
    return "error"


def execute_python(paths: list[str], timeout: int = EXEC_TIMEOUT) -> dict:
    """Run the entrypoint in a subprocess and report what happened.

    NOTE: this executes model-generated code. It runs in a scratch directory
    with a scrubbed environment and a hard timeout, which is a speed bump, not
    a sandbox. Use --no-exec when running an untrusted prompt set.
    """
    entry = _python_entrypoint(paths)
    if not entry:
        return {"ok": False, "outcome": "no_entrypoint", "detail": ""}

    src_dir = Path(entry).parent
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": tempfile.gettempdir(),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    with tempfile.TemporaryDirectory(prefix="eval_exec_") as workdir:
        try:
            proc = subprocess.run(
                [sys.executable, str(Path(entry).resolve())],
                cwd=workdir,
                env={**env, "PYTHONPATH": str(src_dir.resolve())},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            # Long-running by design (server, game loop) — it did not crash.
            return {"ok": True, "outcome": "ran_until_timeout", "detail": ""}
        except OSError as e:
            return {"ok": False, "outcome": "error", "detail": str(e)}

    if proc.returncode == 0:
        return {"ok": True, "outcome": "exited_clean", "detail": ""}

    stderr = (proc.stderr or "").strip()
    outcome = _classify_python_failure(stderr)
    # A script that only wanted stdin did run — count it, but label it.
    return {
        "ok": outcome == "needs_stdin",
        "outcome": outcome,
        "detail": stderr.splitlines()[-1][:300] if stderr else f"exit {proc.returncode}",
    }


def _html_static_check(path: str) -> dict:
    """Structural sanity when no browser is available."""
    try:
        src = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"ok": False, "outcome": "unreadable", "detail": str(e)}
    low = src.lower()
    if "<html" not in low and "<!doctype html" not in low:
        return {"ok": False, "outcome": "not_html", "detail": "no <html> or doctype"}
    if low.count("<script") != low.count("</script>"):
        return {"ok": False, "outcome": "unbalanced_script", "detail": "truncated <script>"}
    return {"ok": True, "outcome": "static_ok", "detail": "no browser — structure only"}


def _chromium_launch_kwargs() -> dict:
    """Point Playwright at a pre-installed Chromium when one is around.

    Environments that ship a browser but not Playwright's own download (CI
    images, sandboxes) leave the bundled-revision lookup pointing at nothing.
    EVAL_CHROMIUM_PATH wins; otherwise we look where such images put it.
    """
    explicit = os.environ.get("EVAL_CHROMIUM_PATH")
    if explicit and Path(explicit).exists():
        return {"executable_path": explicit}

    roots = [Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))]
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in ("chromium-*/chrome-linux/chrome", "chromium_headless_shell-*/chrome-linux/headless_shell"):
            found = sorted(root.glob(pattern))
            if found:
                return {"executable_path": str(found[-1])}
    return {}


def execute_html(paths: list[str], timeout: int = EXEC_TIMEOUT) -> dict:
    """Load the page and fail on uncaught JS errors, when Playwright exists.

    Falls back to static structure checks, which is what Jett's machine will do
    unless Playwright is installed. The outcome field records which ran, so a
    summary never implies a browser check that did not happen.
    """
    if not paths:
        return {"ok": False, "outcome": "no_entrypoint", "detail": ""}
    page_path = max(paths, key=lambda p: Path(p).stat().st_size if Path(p).exists() else 0)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return _html_static_check(page_path)

    errors: list[str] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(**_chromium_launch_kwargs())
            page = browser.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on(
                "console",
                lambda m: errors.append(m.text) if m.type == "error" else None,
            )
            page.goto(f"file://{Path(page_path).resolve()}", timeout=timeout * 1000)
            page.wait_for_timeout(1500)  # let the game loop / init actually run
            has_body = page.evaluate("document.body && document.body.innerHTML.length > 0")
            browser.close()
    except Exception as e:  # browser missing, launch failure — fall back honestly
        static = _html_static_check(page_path)
        static["detail"] = f"browser unavailable ({type(e).__name__}) — {static['detail']}"
        return static

    if errors:
        return {"ok": False, "outcome": "js_error", "detail": errors[0][:300]}
    if not has_body:
        return {"ok": False, "outcome": "empty_page", "detail": "body rendered empty"}
    return {"ok": True, "outcome": "browser_ok", "detail": "no JS errors"}


def execute_artifacts(code_files: list[str], timeout: int = EXEC_TIMEOUT) -> dict:
    """Run whichever artifact kind dominates the deliverable."""
    buckets = classify_files(code_files)
    if buckets["html"]:
        return execute_html(buckets["html"], timeout=timeout)
    if buckets["python"]:
        return execute_python(buckets["python"], timeout=timeout)
    return {"ok": False, "outcome": "nothing_executable", "detail": ""}


# ── (d) model judgment ──────────────────────────────────────────────────────

JUDGE_PROMPT = """You are grading whether a delivered artifact satisfies a user's request.

USER'S REQUEST:
{task}

DELIVERED ARTIFACT:
{deliverable}

Grade on this scale:
5 = fully satisfies the request; a user would be happy
4 = satisfies the core request with minor gaps
3 = partially satisfies it; significant pieces missing
2 = barely related; mostly fails the request
1 = does not address the request at all

Reply with ONE digit (1-5) and nothing else."""


def build_judge_prompt(task: str, deliverable: str) -> str:
    text = deliverable or ""
    if len(text) > JUDGE_CHAR_BUDGET:
        head = text[: JUDGE_CHAR_BUDGET // 2]
        tail = text[-JUDGE_CHAR_BUDGET // 2:]
        text = f"{head}\n\n...[truncated for grading]...\n\n{tail}"
    return JUDGE_PROMPT.format(task=task, deliverable=text)


def parse_judge_score(response: str) -> int | None:
    """First standalone 1-5 in the response wins; None when unparseable."""
    if not response:
        return None
    m = re.search(r"\b([1-5])\b", response)
    return int(m.group(1)) if m else None


# ── aggregation ─────────────────────────────────────────────────────────────

def is_success(record: dict, require_judge: bool = True) -> bool:
    """The headline definition of 'runnable, on-spec output'.

    require_judge=False drops the model-judgment gate, for runs made with
    --no-judge (no local model available). That is a *different, weaker*
    measure — mechanical checks only — so runs scored the two ways must never
    be compared with each other. The summary records which was used.
    """
    judge = record.get("judge_score")
    mechanical = bool(
        record.get("extracted")
        and record.get("parses")
        and record.get("executes")
        and record.get("artifact_match")
        and record.get("keywords_ok")
    )
    if not require_judge:
        return mechanical
    return mechanical and judge is not None and judge >= PASS_JUDGE_SCORE


def summarize(records: list[dict]) -> dict:
    """Roll per-prompt records into the numbers that go in the README."""
    total = len(records)
    if not total:
        return {"total": 0, "success": 0, "success_rate": 0.0, "by_category": {}, "by_stage": {}}

    successes = [r for r in records if r.get("success")]
    judged = [r["judge_score"] for r in records if r.get("judge_score") is not None]
    durations = [r["seconds"] for r in records if r.get("seconds") is not None]
    subtasks = [r["subtask_count"] for r in records if r.get("subtask_count") is not None]

    by_category: dict[str, dict] = {}
    for r in records:
        cat = r.get("category", "uncategorized")
        entry = by_category.setdefault(cat, {"total": 0, "success": 0})
        entry["total"] += 1
        entry["success"] += 1 if r.get("success") else 0
    for entry in by_category.values():
        entry["rate"] = round(entry["success"] / entry["total"], 3) if entry["total"] else 0.0

    # Where runs die, so tuning has somewhere to aim.
    by_stage = {
        "no_files_extracted": sum(1 for r in records if not r.get("extracted")),
        "parse_failed": sum(1 for r in records if r.get("extracted") and not r.get("parses")),
        "execution_failed": sum(
            1 for r in records if r.get("parses") and not r.get("executes")
        ),
        "wrong_artifact_kind": sum(
            1 for r in records if r.get("extracted") and not r.get("artifact_match")
        ),
        "missing_keywords": sum(
            1 for r in records if r.get("extracted") and not r.get("keywords_ok")
        ),
        "judged_below_bar": sum(
            1
            for r in records
            if r.get("executes")
            and r.get("judge_score") is not None
            and r["judge_score"] < PASS_JUDGE_SCORE
        ),
        # A judge that rambles instead of answering costs the run a pass, so it
        # has to be visible rather than hiding inside the failure count.
        "judge_unparseable": sum(
            1 for r in records if not r.get("error") and r.get("judge_score") is None
        ),
        "pipeline_error": sum(1 for r in records if r.get("error")),
    }

    return {
        "total": total,
        "success": len(successes),
        "success_rate": round(len(successes) / total, 3),
        "mean_judge_score": round(sum(judged) / len(judged), 2) if judged else None,
        "mean_seconds": round(sum(durations) / len(durations), 1) if durations else None,
        "total_seconds": round(sum(durations), 1) if durations else None,
        "mean_subtasks": round(sum(subtasks) / len(subtasks), 2) if subtasks else None,
        "by_category": by_category,
        "by_stage": by_stage,
    }


def render_markdown(summary: dict, records: list[dict], meta: dict) -> str:
    """Human-readable summary table — this is what gets committed and compared."""
    lines: list[str] = []
    rate = summary.get("success_rate", 0.0)
    lines.append(f"# Eval run — {meta.get('run_id', 'unknown')}")
    lines.append("")
    lines.append(f"**Model:** `{meta.get('model', '?')}` · **Mode:** {meta.get('mode', '?')}")
    lines.append("")
    lines.append(
        f"**Success rate: {rate:.0%}** ({summary.get('success', 0)}/{summary.get('total', 0)}) "
        f"— target is 80%"
    )
    lines.append("")
    lines.append(
        f"Mean judge score: {summary.get('mean_judge_score')} · "
        f"Mean wall clock: {summary.get('mean_seconds')}s · "
        f"Mean subtasks: {summary.get('mean_subtasks')}"
    )
    lines.append("")

    lines.append("## By category")
    lines.append("")
    lines.append("| Category | Pass | Total | Rate |")
    lines.append("| --- | ---: | ---: | ---: |")
    for cat in sorted(summary.get("by_category", {})):
        e = summary["by_category"][cat]
        lines.append(f"| {cat} | {e['success']} | {e['total']} | {e['rate']:.0%} |")
    lines.append("")

    lines.append("## Where runs failed")
    lines.append("")
    lines.append("| Failure stage | Count |")
    lines.append("| --- | ---: |")
    for stage, count in summary.get("by_stage", {}).items():
        lines.append(f"| {stage.replace('_', ' ')} | {count} |")
    lines.append("")

    lines.append("## Per prompt")
    lines.append("")
    lines.append("| Prompt | Category | Files | Parses | Executes | Judge | Secs | Pass |")
    lines.append("| --- | --- | ---: | :---: | :---: | :---: | ---: | :---: |")
    for r in records:
        tick = "✅" if r.get("success") else "❌"
        parses = "✓" if r.get("parses") else "✗"
        exe = "✓" if r.get("executes") else "✗"
        judge = r.get("judge_score")
        secs = r.get("seconds")
        lines.append(
            f"| `{r.get('id', '?')}` | {r.get('category', '?')} | {len(r.get('code_files', []))} "
            f"| {parses} | {exe} | {judge if judge is not None else '–'} "
            f"| {round(secs) if secs is not None else '–'} | {tick} |"
        )
    lines.append("")

    failures = [r for r in records if not r.get("success")]
    if failures:
        lines.append("## Failure detail")
        lines.append("")
        for r in failures:
            reasons = []
            if r.get("error"):
                reasons.append(f"pipeline error: {r['error']}")
            if not r.get("extracted"):
                reasons.append("no files extracted")
            if r.get("problems"):
                reasons.append("; ".join(r["problems"][:2]))
            if not r.get("artifact_match"):
                reasons.append(f"wrong artifact kind (wanted {r.get('expected_artifact')})")
            if r.get("missing_keywords"):
                reasons.append(f"missing keywords: {', '.join(r['missing_keywords'])}")
            if not r.get("executes"):
                reasons.append(
                    f"exec {r.get('exec_outcome', '?')}: {r.get('exec_detail', '')}".strip()
                )
            if r.get("judge_score") is not None and r["judge_score"] < PASS_JUDGE_SCORE:
                reasons.append(f"judge {r['judge_score']}/5")
            lines.append(f"- **{r.get('id')}** — {' · '.join(reasons) or 'unknown'}")
        lines.append("")

    return "\n".join(lines)
