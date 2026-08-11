"""
Run a showcase artifact N times and check it actually came out right (§1.3).

The sprint's bar is "good in at least 8 of 10". Judging that by eye is slow and
subjective, so each generated file is opened in a real headless browser. What
gets checked is per-artifact and lives in `showcase.py` — see that module for
why a game and a chart are not held to the same criteria.

    python scripts/showcase_reliability.py --runs 10
    python scripts/showcase_reliability.py --candidate clock --runs 10
    python scripts/showcase_reliability.py --candidate clock,chart,particles --runs 3

The default is `snake`, unchanged, so the 2/10 in docs/showcase-ceiling.md stays
reproducible with the original command.

With several candidates the runs are **round-robin**, not grouped: one run of
each, then the next. An interrupted screening pass then still compares like
with like, instead of leaving one candidate fully measured and the rest at zero.

Writes scripts/showcase_results/ with a row per run so the pass rate is
traceable. Each run is a full pipeline execution — budget 20-50 minutes each.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RESULTS = Path(__file__).parent / "showcase_results"

sys.path.insert(0, str(REPO))
from showcase import CANDIDATES, Candidate, get as get_candidate  # noqa: E402

# Canvas ink + any element whose visible text matches a forbidden pattern.
# Deliberately checks *visible* elements rather than source text: `NaN` in a
# comment is harmless, `NaN` painted on the page is a broken artifact.
STATE_JS = """
(forbidden) => {
  const out = {canvas: false, painted: 0, domInk: 0, forbiddenHit: null, text: ''};
  out.text = (document.body && document.body.innerText || '').slice(0, 4000);
  const c = document.querySelector('canvas');
  if (c) {
    out.canvas = true;
    try {
      const ctx = c.getContext('2d');
      const d = ctx.getImageData(0, 0, c.width, c.height).data;
      let painted = 0;
      for (let i = 0; i < d.length; i += 4) {
        if (d[i] + d[i+1] + d[i+2] > 30) painted++;
      }
      out.painted = painted;
    } catch (e) { /* tainted canvas — treat as unpainted */ }
  }
  // DOM ink: how many elements actually occupy visible space. The equivalent
  // of "lit pixels" for an artifact built from elements rather than a canvas.
  out.domInk = [...document.querySelectorAll('body *')].filter(el => {
    const r = el.getBoundingClientRect();
    if (r.width * r.height <= 100) return false;
    const cs = getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden' && cs.opacity !== '0';
  }).length;
  const vis = el => {
    const cs = getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden' && cs.opacity !== '0';
  };
  for (const pat of forbidden) {
    const re = new RegExp(pat, 'i');
    const hit = [...document.querySelectorAll('div,section,p,h1,h2,h3,span,td,li')]
      .some(el => re.test(el.textContent || '') && vis(el));
    if (hit) { out.forbiddenHit = pat; break; }
  }
  return out;
}
"""

FRAME_HASH_JS = """
() => {
  const c = document.querySelector('canvas');
  if (!c) return -1;
  try {
    const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
    let h = 0;
    for (let i = 0; i < d.length; i += 40) h = (h * 31 + d[i] + d[i+1] + d[i+2]) % 1000003;
    return h;
  } catch (e) { return -1; }
}
"""

# Forbidden patterns are regexes so "nan" cannot match inside a longer word.
_WORDY = {"nan", "undefined"}


def _forbidden_patterns(cand: Candidate) -> list[str]:
    out = []
    for term in cand.forbidden_text:
        if term in _WORDY:
            out.append(rf"\b{term}\b")
        else:
            out.append(term.replace(" ", r"\s*"))
    return out


def check_artifact(html_path: Path, cand: Candidate) -> dict:
    """Open the artifact in a headless browser and decide whether it came out right."""
    from playwright.sync_api import sync_playwright

    html_path = Path(html_path).resolve()  # as_uri() rejects relative paths
    verdict = {"ok": False, "reasons": [], "console_errors": []}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)[:200]))
        page.on("console", lambda m: errors.append(m.text[:200]) if m.type == "error" else None)
        try:
            page.goto(html_path.as_uri(), wait_until="load", timeout=20000)
            page.wait_for_timeout(1200)

            state = page.evaluate(STATE_JS, _forbidden_patterns(cand))
            first = page.evaluate(FRAME_HASH_JS)
            page.wait_for_timeout(1500)
            second = page.evaluate(FRAME_HASH_JS)

            # Third sample. For an arrow-key artifact this comes after a key
            # press, matching how snake has always been measured; otherwise it
            # is just more time, so every candidate gets the same three samples.
            if cand.needs_key_response:
                page.keyboard.press("ArrowRight")
                page.wait_for_timeout(800)
            else:
                page.wait_for_timeout(800)
            third = page.evaluate(FRAME_HASH_JS)

            verdict["console_errors"] = errors[:5]
            if errors:
                verdict["reasons"].append(f"js errors: {errors[0]}")
            if cand.needs_canvas:
                if not state["canvas"]:
                    verdict["reasons"].append("no canvas")
                elif state["painted"] < cand.min_ink:
                    verdict["reasons"].append(
                        f"canvas essentially blank ({state['painted']} lit px, need {cand.min_ink})"
                    )
            elif state["domInk"] < cand.min_ink:
                verdict["reasons"].append(
                    f"page essentially empty ({state['domInk']} visible elements, need {cand.min_ink})"
                )
            if state["forbiddenHit"]:
                verdict["reasons"].append(f"forbidden text visible: {state['forbiddenHit']}")

            text = (state.get("text") or "").lower()
            missing = [t for t in cand.required_text if t.lower() not in text]
            if missing:
                verdict["reasons"].append(f"missing required text: {missing}")

            if cand.needs_animation:
                animates = (first != second) or (second != third)
                if not animates:
                    verdict["reasons"].append("no animation (frame never changes)")
            verdict["frames"] = [first, second, third]

            verdict["ok"] = not verdict["reasons"]
            verdict["detail"] = {k: v for k, v in state.items() if k != "text"}
        except Exception as e:
            verdict["reasons"].append(f"page failed: {str(e)[:150]}")
        finally:
            browser.close()
    return verdict


def newest_html(before: set[str]) -> Path | None:
    """The HTML from the run we just did — newest output dir not seen before."""
    out = REPO / "output"
    if not out.exists():
        return None
    fresh = [d for d in out.iterdir() if d.is_dir() and d.name not in before]
    for d in sorted(fresh, reverse=True):
        html = sorted((d / "code").glob("*.html")) if (d / "code").exists() else []
        if html:
            return html[0]
    return None


def one_run(cand: Candidate, prompt_set: str | None) -> dict:
    before = {d.name for d in (REPO / "output").iterdir() if d.is_dir()} if (REPO / "output").exists() else set()
    t0 = time.time()

    cmd = [sys.executable, "cli.py", "--demo-showcase", cand.id, "--no-open"]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    if prompt_set:
        env["PROMPT_SET"] = prompt_set

    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                          errors="replace", env=env)
    elapsed = round(time.time() - t0, 1)

    html = newest_html(before)
    row = {
        "candidate": cand.id,
        "seconds": elapsed,
        "exit_code": proc.returncode,
        "html": str(html) if html else None,
    }
    if html is None:
        row.update({"ok": False, "reasons": ["no html produced"]})
        row["stderr_tail"] = (proc.stderr or "")[-500:]
    else:
        row.update(check_artifact(html, cand))
    return row


def main():
    ap = argparse.ArgumentParser(
        description="Showcase reliability harness (sprint §1.3)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        # Plain hyphen, not an em dash: argparse writes this straight to a
        # Windows console that is often still cp1252, and mojibake in --help
        # is a bad first impression for a stranger.
        epilog="Candidates:\n" + "\n".join(
            f"  {c.id:<10} {c.title} - {c.blurb}" for c in CANDIDATES.values()
        ),
    )
    ap.add_argument("--runs", type=int, default=10, help="runs per candidate")
    ap.add_argument("--candidate", default="snake",
                    help="comma-separated candidate ids (default: snake)")
    ap.add_argument("--prompt-set", default=None)
    args = ap.parse_args()

    cands = [get_candidate(c.strip()) for c in args.candidate.split(",") if c.strip()]

    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log = RESULTS / f"showcase_{stamp}.jsonl"
    rows: list[dict] = []

    total = args.runs * len(cands)
    print(f"Measuring {[c.id for c in cands]} — {args.runs} run(s) each, {total} total.")
    print(f"Log: {log}\n", flush=True)

    n = 0
    # Round-robin so an interrupted pass still compares like with like.
    for cycle in range(1, args.runs + 1):
        for cand in cands:
            n += 1
            print(f"=== [{n}/{total}] {cand.id} run {cycle}/{args.runs} — generating", flush=True)
            row = one_run(cand, args.prompt_set)
            row["run"] = cycle
            rows.append(row)
            with log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")

            mark = "OK    " if row.get("ok") else "FAILED"
            print(f"    {mark} in {row['seconds']/60:.0f} min — {row.get('reasons') or 'clean'}", flush=True)
            for c in cands:
                got = [r for r in rows if r["candidate"] == c.id]
                if got:
                    print(f"      {c.id:<10} {sum(1 for r in got if r.get('ok'))}/{len(got)}", flush=True)

    print(f"\n{'=' * 56}")
    for c in cands:
        got = [r for r in rows if r["candidate"] == c.id]
        ok = sum(1 for r in got if r.get("ok"))
        mins = sum(r["seconds"] for r in got) / max(len(got), 1) / 60
        verdict = "MEETS BAR" if len(got) >= 10 and ok >= 8 else ""
        print(f"{c.id:<10} {ok}/{len(got)}  (avg {mins:.0f} min/run)  {verdict}")
    print(f"Log: {log}")


if __name__ == "__main__":
    main()
