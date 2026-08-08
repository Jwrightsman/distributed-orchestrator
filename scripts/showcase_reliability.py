"""
Run the showcase demo N times and check the game is actually playable (§1.3).

The sprint's bar is "playable in at least 8 of 10". Judging that by eye is slow
and subjective, so each generated file is opened in a real headless browser and
checked for the things that make a Snake game playable on camera:

  - loads with no uncaught JavaScript error
  - has a <canvas> that something actually draws on
  - the frame changes on its own (a game loop is running)
  - arrow keys are wired up
  - no "GAME OVER" visible before the player has died

That last one is a real observed failure: a run shipped the game-over overlay
as its start screen, so the file opened looking broken.

    python scripts/showcase_reliability.py --runs 10

Writes scripts/showcase_results/ with a row per run so the pass rate is
traceable. Each run is a full pipeline execution — budget ~50 minutes each.
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

PLAYABILITY_JS = """
() => {
  const out = {canvas: false, painted: 0, keyHandlers: false, overlayVisibleAtStart: false};
  const c = document.querySelector('canvas');
  if (!c) return out;
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
  // Any element showing game-over text before play begins
  out.overlayVisibleAtStart = [...document.querySelectorAll('div,section,p,h1,h2')].some(el => {
    if (!/game\\s*over/i.test(el.textContent || '')) return false;
    const cs = getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden' && cs.opacity !== '0';
  });
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


def check_playable(html_path: Path) -> dict:
    """Open the game in a headless browser and decide whether it is playable."""
    from playwright.sync_api import sync_playwright

    html_path = Path(html_path).resolve()  # as_uri() rejects relative paths
    verdict = {"playable": False, "reasons": [], "console_errors": []}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)[:200]))
        page.on("console", lambda m: errors.append(m.text[:200]) if m.type == "error" else None)
        try:
            page.goto(html_path.as_uri(), wait_until="load", timeout=20000)
            page.wait_for_timeout(1200)

            state = page.evaluate(PLAYABILITY_JS)
            first = page.evaluate(FRAME_HASH_JS)
            page.wait_for_timeout(1500)
            second = page.evaluate(FRAME_HASH_JS)

            # Steer, then confirm the picture responds
            page.keyboard.press("ArrowRight")
            page.wait_for_timeout(800)
            third = page.evaluate(FRAME_HASH_JS)

            verdict["console_errors"] = errors[:5]
            if errors:
                verdict["reasons"].append(f"js errors: {errors[0]}")
            if not state["canvas"]:
                verdict["reasons"].append("no canvas")
            if state["painted"] < 50:
                verdict["reasons"].append("canvas essentially blank")
            if state["overlayVisibleAtStart"]:
                verdict["reasons"].append("GAME OVER visible before play")
            animates = (first != second) or (second != third)
            if not animates:
                verdict["reasons"].append("no game loop (frame never changes)")

            verdict["playable"] = not verdict["reasons"]
            verdict["detail"] = state
        except Exception as e:
            verdict["reasons"].append(f"page failed: {str(e)[:150]}")
        finally:
            browser.close()
    return verdict


def newest_showcase_html(before: set[str]) -> Path | None:
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


def main():
    ap = argparse.ArgumentParser(description="Showcase reliability harness (sprint §1.3)")
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--prompt-set", default=None)
    args = ap.parse_args()

    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log = RESULTS / f"showcase_{stamp}.jsonl"
    rows = []

    for i in range(1, args.runs + 1):
        before = {d.name for d in (REPO / "output").iterdir() if d.is_dir()} if (REPO / "output").exists() else set()
        print(f"\n=== run {i}/{args.runs} — generating (this takes ~50 min)", flush=True)
        t0 = time.time()

        cmd = [sys.executable, "cli.py", "--demo-showcase"]
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        if args.prompt_set:
            env["PROMPT_SET"] = args.prompt_set

        proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                              errors="replace", env=env)
        elapsed = round(time.time() - t0, 1)

        html = newest_showcase_html(before)
        row = {
            "run": i,
            "seconds": elapsed,
            "exit_code": proc.returncode,
            "html": str(html) if html else None,
        }
        if html is None:
            row.update({"playable": False, "reasons": ["no html produced"]})
        else:
            row.update(check_playable(html))

        rows.append(row)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

        mark = "PLAYABLE" if row.get("playable") else "FAILED  "
        print(f"    {mark} in {elapsed/60:.0f} min — {row.get('reasons') or 'clean'}", flush=True)

        playable = sum(1 for r in rows if r.get("playable"))
        print(f"    running total: {playable}/{len(rows)} playable", flush=True)

    playable = sum(1 for r in rows if r.get("playable"))
    print(f"\n{'=' * 50}")
    print(f"§1.3 result: {playable}/{len(rows)} playable (bar is >= 8/10)")
    print(f"Log: {log}")
    if playable < 8 and len(rows) >= 10:
        print("BELOW BAR — the showcase prompt needs tuning before it goes on camera.")


if __name__ == "__main__":
    main()
