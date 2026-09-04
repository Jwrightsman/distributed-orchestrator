"""Grading: what "this item passed" is allowed to mean.

Grading validity dominates every other property of an eval instrument. A larger
corpus graded loosely is worse than a small one graded correctly, because it
produces confident wrong answers faster.

Four rules, and the audit finding that forced the third:

1. **Mechanical wherever possible.** Code must run. JSON must validate against
   a committed schema. A transform must produce the expected output. ROADMAP
   section 2 says a negative result is verified by running the artifact; the
   same applies to a positive one, and this module is where that is enforced
   rather than hoped for.

2. **Deterministic given an artifact.** The same files graded twice give the
   same verdict, and `tests/test_eval_grading.py` asserts it. The one
   exception is `html_behaviour`, which drives a real browser and is marked
   `deterministic=False` in its own result so a caller can never mistake it
   for a repeatable check.

3. **"Loads without throwing" is not "runs".** The original harness graded
   HTML with `browser_ok`: no uncaught JS error and a non-empty body. Under
   that check `web-snake` passed **5 times out of 5** across the committed
   runs — while `scripts/showcase_reliability.py`, asking the same model for
   the same artifact, measured it **2 out of 10** by also requiring that
   something got drawn, that the frame changed, and that arrow keys did
   anything. Both numbers are in this repository and they disagree because
   they are different checks, not because the model changed. `html_behaviour`
   is the strict one, generalised from the showcase checker.

4. **No model-judged primary endpoint.** A judge is a second correlated
   probabilistic system, and a judge sharing a family with the generator is
   worse than that. `judge_score` is still collected, still recorded, and
   labelled `exploratory`; it gates nothing. Removing it from the gate turned
   out to cost no power either — recomputing the five committed runs without
   the judge moves the discordant rate from 0.521 to 0.514, so the noise was
   never the judge's.

5. **Ungraded is not failed.** `GradeResult.graded` is a separate flag. A
   check that could not run — no browser, missing fixture, no artifact at all
   — is recorded as ungraded, and the summariser in
   `scripts/eval_study_summary.py` refuses to compute a statistic over a study
   containing one. Silently scoring an unrun check as a failure is how an
   instrument reports a result it did not measure.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

EVALS_DIR = Path(__file__).resolve().parent
FIXTURE_INPUTS = EVALS_DIR / "fixtures" / "inputs"
SCHEMAS_DIR = EVALS_DIR / "fixtures" / "schemas"

sys.path.insert(0, str(EVALS_DIR))
sys.path.insert(0, str(EVALS_DIR.parent))

import scoring  # noqa: E402

# Bumped whenever a check's verdict on an unchanged artifact could change.
# Recorded on every run so two results scored by different graders are never
# silently compared — the mistake that produced 0/14 for ensemble before the
# checker was repaired.
GRADER_VERSION = "2"

EXEC_TIMEOUT = 20

CHECK_KINDS = (
    "parses",
    "artifact_kind",
    "keywords",
    "runs",
    "stdout_contains",
    "stdout_json_schema",
    "html_behaviour",
)


@dataclass(frozen=True)
class CheckResult:
    kind: str
    graded: bool
    passed: bool
    detail: str = ""
    deterministic: bool = True


@dataclass
class GradeResult:
    """The verdict for one item, with every check that produced it."""

    item_id: str
    grader_version: str
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def graded(self) -> bool:
        """True only when every check actually ran."""
        return bool(self.checks) and all(c.graded for c in self.checks)

    @property
    def passed(self) -> bool:
        """The primary endpoint: every check ran and every check passed."""
        return self.graded and all(c.passed for c in self.checks)

    @property
    def ungraded_checks(self) -> list[str]:
        return [c.kind for c in self.checks if not c.graded]

    @property
    def failed_checks(self) -> list[str]:
        return [c.kind for c in self.checks if c.graded and not c.passed]

    @property
    def deterministic(self) -> bool:
        return all(c.deterministic for c in self.checks)

    def as_record(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "grader_version": self.grader_version,
            "graded": self.graded,
            "passed": self.passed,
            "deterministic": self.deterministic,
            "ungraded_checks": self.ungraded_checks,
            "failed_checks": self.failed_checks,
            "checks": [
                {
                    "kind": c.kind,
                    "graded": c.graded,
                    "passed": c.passed,
                    "detail": c.detail,
                    "deterministic": c.deterministic,
                }
                for c in self.checks
            ],
        }


# -- running the artifact ----------------------------------------------------

def _scrubbed_env(src_dir: Path) -> dict[str, str]:
    """The same speed bump `evals/scoring.py` uses, for the same reason.

    Not a sandbox. A scratch working directory, a scrubbed environment and a
    hard timeout. SystemRoot has to survive or anything touching a socket dies
    on Windows before running a line of its own logic, which once scored three
    correct API servers as broken code.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": tempfile.gettempdir(),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(src_dir.resolve()),
    }
    for winvar in ("SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"):
        if winvar in os.environ:
            env[winvar] = os.environ[winvar]
    return env


@dataclass(frozen=True)
class RunOutcome:
    ran: bool
    stdout: str
    stderr: str
    outcome: str


def run_python(
    code_files: Sequence[str],
    inputs: Sequence[str] = (),
    timeout: int = EXEC_TIMEOUT,
    cache: dict | None = None,
) -> RunOutcome:
    """Execute the entrypoint with the named fixture inputs beside it.

    `inputs` name files under `evals/fixtures/inputs/`. They are copied into
    the scratch working directory so a task phrased the way a user would phrase
    it ("read sales.csv and print the totals") can be graded on what it printed
    rather than on whether it contained the right words.

    `cache` memoises within one `grade()` call so an item whose execution and
    output checks want the same run pays for one subprocess rather than two.
    It is deliberately per-call: a cache that outlived a grading pass would
    make re-grading an edited artifact return the old artifact's output.
    """
    key = (tuple(code_files), tuple(inputs), timeout)
    if cache is not None and key in cache:
        return cache[key]
    outcome = _run_python_uncached(code_files, inputs, timeout)
    if cache is not None:
        cache[key] = outcome
    return outcome


def _run_python_uncached(
    code_files: Sequence[str], inputs: Sequence[str], timeout: int
) -> RunOutcome:
    entry = scoring._python_entrypoint(list(code_files))
    if not entry:
        return RunOutcome(False, "", "", "no_entrypoint")

    for name in inputs:
        if not (FIXTURE_INPUTS / name).is_file():
            return RunOutcome(False, "", "", f"missing_fixture:{name}")

    src_dir = Path(entry).parent
    with tempfile.TemporaryDirectory(prefix="eval_grade_") as workdir:
        for name in inputs:
            shutil.copy(FIXTURE_INPUTS / name, Path(workdir) / name)
        try:
            proc = subprocess.run(
                [sys.executable, str(Path(entry).resolve())],
                cwd=workdir,
                env=_scrubbed_env(src_dir),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or ""
            return RunOutcome(
                True,
                out if isinstance(out, str) else out.decode("utf-8", "replace"),
                "",
                "ran_until_timeout",
            )
        except OSError as exc:
            return RunOutcome(False, "", str(exc), "error")

    if proc.returncode == 0:
        return RunOutcome(True, proc.stdout or "", proc.stderr or "", "exited_clean")
    stderr = (proc.stderr or "").strip()
    outcome = scoring._classify_python_failure(stderr)
    return RunOutcome(
        outcome in ("needs_stdin", "needs_args"),
        proc.stdout or "",
        stderr,
        outcome,
    )


# -- individual checks -------------------------------------------------------

def check_parses(code_files: Sequence[str]) -> CheckResult:
    if not code_files:
        return CheckResult("parses", graded=True, passed=False, detail="no files extracted")
    ok, problems = scoring.check_parses(list(code_files))
    return CheckResult("parses", True, ok, "; ".join(problems[:2]))


def check_artifact_kind(code_files: Sequence[str], artifact: str) -> CheckResult:
    ok = scoring.matches_expected_artifact(list(code_files), artifact)
    return CheckResult("artifact_kind", True, ok, f"wanted {artifact}")


def check_keywords(code_files: Sequence[str], keywords: Sequence[str]) -> CheckResult:
    """A weak proxy, kept as a gate and labelled as one.

    A file containing the string `setInterval` tells you nothing about whether
    the loop it starts does anything — that exact reasoning produced a
    confident wrong answer twice in this project's history. Keywords stay
    because they cheaply catch the wrong artifact entirely; they are never the
    only check on an item.
    """
    ok, missing = scoring.check_keywords(list(code_files), list(keywords))
    return CheckResult("keywords", True, ok, f"missing: {', '.join(missing)}" if missing else "")


def check_runs(
    code_files: Sequence[str],
    artifact: str,
    timeout: int = EXEC_TIMEOUT,
    cache: dict | None = None,
    inputs: Sequence[str] = (),
) -> CheckResult:
    """Does the artifact actually execute — the check that cannot be skipped.

    `inputs` are the item's declared fixture files. They have to be present for
    this check too: running a script that was asked to read sales.csv in a
    directory with no sales.csv scores a correct program as broken, which is
    the same mistake that once failed three working API servers over a missing
    Windows environment variable.
    """
    if not code_files:
        return CheckResult("runs", graded=True, passed=False, detail="no files extracted")
    buckets = scoring.classify_files(list(code_files))
    if artifact == "html" or (artifact == "any" and buckets["html"]):
        result = scoring.execute_html(buckets["html"], timeout=timeout)
        # A static structure check is not an execution check. It is recorded as
        # ungraded rather than as a pass, because "no browser was available"
        # and "the page works" are different facts.
        if result["outcome"] in ("static_ok", "not_html", "unbalanced_script", "unreadable"):
            if result["outcome"] == "static_ok":
                return CheckResult(
                    "runs", graded=False, passed=False,
                    detail="no browser available — structure only, not an execution check",
                )
            return CheckResult("runs", True, False, f"{result['outcome']}: {result['detail']}")
        return CheckResult("runs", True, bool(result["ok"]), f"{result['outcome']}: {result['detail']}")
    if not buckets["python"]:
        # Neither a page nor a program. Matching `scoring.execute_artifacts`:
        # this is a failure, not something that could not be checked.
        return CheckResult(
            "runs", True, False,
            f"nothing_executable: produced {len(code_files)} file(s), none runnable",
        )
    outcome = run_python(buckets["python"], inputs, timeout=timeout, cache=cache)
    return CheckResult("runs", True, outcome.ran, f"{outcome.outcome}: {outcome.stderr[-200:]}")


def check_stdout_contains(
    code_files: Sequence[str],
    substrings: Sequence[str],
    inputs: Sequence[str] = (),
    timeout: int = EXEC_TIMEOUT,
    cache: dict | None = None,
) -> CheckResult:
    outcome = run_python(code_files, inputs, timeout, cache)
    if outcome.outcome.startswith("missing_fixture"):
        return CheckResult("stdout_contains", graded=False, passed=False, detail=outcome.outcome)
    if not outcome.ran:
        return CheckResult("stdout_contains", True, False, f"did not run: {outcome.outcome}")
    lowered = outcome.stdout.lower()
    missing = [s for s in substrings if s.lower() not in lowered]
    return CheckResult(
        "stdout_contains",
        True,
        not missing,
        f"missing from stdout: {', '.join(missing)}" if missing else "",
    )


def check_stdout_json_schema(
    code_files: Sequence[str],
    schema_name: str,
    inputs: Sequence[str] = (),
    timeout: int = EXEC_TIMEOUT,
    cache: dict | None = None,
) -> CheckResult:
    """Run it, parse stdout as JSON, validate against a committed schema.

    `jsonschema` is already a runtime dependency of this project (the execution
    protocol validators use it), so this adds nothing to install.
    """
    schema_path = SCHEMAS_DIR / f"{schema_name}.json"
    if not schema_path.is_file():
        return CheckResult(
            "stdout_json_schema", graded=False, passed=False,
            detail=f"no committed schema {schema_name}",
        )
    outcome = run_python(code_files, inputs, timeout, cache)
    if outcome.outcome.startswith("missing_fixture"):
        return CheckResult("stdout_json_schema", graded=False, passed=False, detail=outcome.outcome)
    if not outcome.ran:
        return CheckResult("stdout_json_schema", True, False, f"did not run: {outcome.outcome}")

    text = _last_json_blob(outcome.stdout)
    if text is None:
        return CheckResult("stdout_json_schema", True, False, "stdout held no JSON document")
    try:
        payload = json.loads(text)
    except ValueError as exc:
        return CheckResult("stdout_json_schema", True, False, f"stdout is not valid JSON: {exc}")

    import jsonschema

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    try:
        jsonschema.validate(payload, schema)
    except jsonschema.ValidationError as exc:
        return CheckResult("stdout_json_schema", True, False, str(exc.message)[:200])
    return CheckResult("stdout_json_schema", True, True, f"validated against {schema_name}")


def _last_json_blob(stdout: str) -> str | None:
    """Pull the JSON document out of stdout, ignoring chatter around it.

    Programs print a banner before their output more often than not, so
    requiring stdout to be JSON and nothing else would fail correct programs.

    The delimiter that opens *first* wins. Trying braces before brackets
    unconditionally is wrong and was caught by a test: given
    `[{"name": "a"}, {"name": "b"}]` it returns everything between the first
    `{` and the last `}`, which is `{"name": "a"}, {"name": "b"}` — invalid
    JSON produced from perfectly valid output.
    """
    text = stdout.strip()
    if not text:
        return None
    candidates = []
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            candidates.append((start, text[start : end + 1]))
    if not candidates:
        return None
    return min(candidates)[1]


def check_html_behaviour(
    code_files: Sequence[str],
    spec: dict[str, Any],
    timeout: int = EXEC_TIMEOUT,
) -> CheckResult:
    """Load the page and require it to *do* something, not merely load.

    Generalised from `scripts/showcase_reliability.py`, which is the checker
    that measured the published 2/10 — and which caught a clock that drew a rim
    and no hands, something "no console errors" called a pass.

    Marked `deterministic=False`. Driving a browser over a timed animation is
    not a repeatable measurement of one artifact, and pretending otherwise is
    how a checker with a 1200 ms blind spot called four working games broken.
    """
    buckets = scoring.classify_files(list(code_files))
    pages = buckets["html"]
    if not pages:
        return CheckResult("html_behaviour", True, False, "no HTML artifact", deterministic=False)
    page_path = max(pages, key=lambda p: Path(p).stat().st_size if Path(p).exists() else 0)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return CheckResult(
            "html_behaviour", graded=False, passed=False,
            detail="playwright not installed — behaviour was not checked",
            deterministic=False,
        )

    errors: list[str] = []
    reasons: list[str] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(**scoring._chromium_launch_kwargs())
            page = browser.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)[:200]))
            page.on(
                "console",
                lambda m: errors.append(m.text[:200]) if m.type == "error" else None,
            )
            page.goto(Path(page_path).resolve().as_uri(), timeout=timeout * 1000)
            page.wait_for_timeout(400)

            if spec.get("canvas_drawn") and not page.evaluate(_CANVAS_DRAWN_JS):
                reasons.append("canvas was never drawn on")
            for text in spec.get("text_present", []):
                if not page.evaluate(_TEXT_VISIBLE_JS, text):
                    reasons.append(f"expected text not visible: {text!r}")
            for text in spec.get("forbidden_text", []):
                if page.evaluate(_TEXT_VISIBLE_JS, text):
                    reasons.append(f"forbidden text visible on load: {text!r}")

            keys = list(spec.get("responds_to_keys", []))
            if spec.get("frame_changes") or keys:
                first = page.evaluate(_FRAME_HASH_JS)
                for key in keys:
                    page.keyboard.press(key)
                    page.wait_for_timeout(250)
                if not keys:
                    page.wait_for_timeout(1200)
                second = page.evaluate(_FRAME_HASH_JS)
                if first == second:
                    reasons.append("nothing on the page changed")
            if not page.evaluate("document.body && document.body.innerHTML.length > 0"):
                reasons.append("body rendered empty")
            browser.close()
    except Exception as exc:  # a browser that will not launch has graded nothing
        return CheckResult(
            "html_behaviour", graded=False, passed=False,
            detail=f"browser unavailable ({type(exc).__name__}) — behaviour was not checked",
            deterministic=False,
        )

    if errors:
        reasons.insert(0, f"js error: {errors[0]}")
    return CheckResult(
        "html_behaviour",
        graded=True,
        passed=not reasons,
        detail="; ".join(reasons[:3]),
        deterministic=False,
    )


_CANVAS_DRAWN_JS = """
() => {
  const canvases = Array.from(document.querySelectorAll('canvas'));
  if (!canvases.length) return false;
  return canvases.some(c => {
    try {
      const ctx = c.getContext('2d');
      if (!ctx || !c.width || !c.height) return false;
      const d = ctx.getImageData(0, 0, c.width, c.height).data;
      for (let i = 0; i < d.length; i += 4) if (d[i] || d[i+1] || d[i+2] || d[i+3]) return true;
      return false;
    } catch (e) { return false; }
  });
}
"""

_TEXT_VISIBLE_JS = """
(needle) => {
  const wanted = String(needle).toLowerCase();
  // Reading an element's own computed style is not enough: an <h2>GAME OVER</h2>
  // inside a display:none overlay reports itself visible, which is one of the
  // three defects that made the showcase checker score working games as broken.
  // checkVisibility() answers the question about the whole ancestor chain;
  // getClientRects() is the fallback, and unlike offsetParent it does not treat
  // a position:fixed element as hidden.
  const visible = (el) => {
    if (!el) return false;
    if (el.tagName === 'BODY') return true;
    if (typeof el.checkVisibility === 'function') return el.checkVisibility();
    return el.getClientRects().length > 0;
  };
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (!(node.textContent || '').toLowerCase().includes(wanted)) continue;
    if (visible(node.parentElement)) return true;
  }
  return false;
}
"""

_FRAME_HASH_JS = """
() => {
  let h = 0;
  const text = document.body ? document.body.innerText : '';
  for (let i = 0; i < text.length; i++) h = (h * 31 + text.charCodeAt(i)) | 0;
  for (const c of document.querySelectorAll('canvas')) {
    try {
      const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
      for (let i = 0; i < d.length; i += 997) h = (h * 31 + d[i]) | 0;
    } catch (e) { h = (h * 31 + 7) | 0; }
  }
  return h;
}
"""


# -- the entry point ---------------------------------------------------------

def grade(item, code_files: Sequence[str], timeout: int = EXEC_TIMEOUT) -> GradeResult:
    """Grade one corpus item's artifact.

    `item` is an `evals.corpus.CorpusItem`. Every item gets parse, artifact
    kind, keyword and execution checks; `expect.checks` adds the output-level
    ones. An item with no artifact at all is graded — as a failure — rather
    than skipped, because "produced nothing" is a result.
    """
    files = [str(f) for f in code_files]
    run_cache: dict = {}
    # Every fixture file any check declares, in a stable order, so the
    # execution check sees the same working directory the output checks do.
    declared_inputs: list[str] = []
    for spec in item.checks:
        for name in spec.get("inputs", []):
            if name not in declared_inputs:
                declared_inputs.append(name)

    results = [
        check_parses(files),
        check_artifact_kind(files, item.artifact),
        check_keywords(files, item.expect.get("keywords", [])),
        check_runs(files, item.artifact, timeout, run_cache, declared_inputs),
    ]

    for spec in item.checks:
        kind = spec.get("kind")
        if kind == "stdout_contains":
            results.append(
                check_stdout_contains(
                    files, spec.get("substrings", []), declared_inputs, timeout, run_cache
                )
            )
        elif kind == "stdout_json_schema":
            results.append(
                check_stdout_json_schema(
                    files, spec["schema"], declared_inputs, timeout, run_cache
                )
            )
        elif kind == "html_behaviour":
            results.append(check_html_behaviour(files, spec, timeout))
        else:
            results.append(
                CheckResult(
                    str(kind), graded=False, passed=False,
                    detail=f"unknown check kind {kind!r} — nothing was graded",
                )
            )

    return GradeResult(item_id=item.id, grader_version=GRADER_VERSION, checks=results)
