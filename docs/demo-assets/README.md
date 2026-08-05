# Demo fallback assets

**Currently empty — and it has to stay that way until a real run fills it.**

Live inference on CPU is variable. If the swarm produces something weak while
the camera is rolling, the recovery is to cut to a real run captured earlier
rather than re-rolling on camera and hoping.

`output/` is gitignored and gets pruned by the disk cap, so good runs vanish.
This directory is committed, so what lands here survives.

## Capturing one

After a run you are happy with:

```bash
python scripts/capture_demo_asset.py --run latest --name snake-game
python scripts/capture_demo_asset.py --list
```

It copies the deliverable, the extracted code, the plan, the review and the
builder transcripts, and writes a manifest recording the model, the prompt set,
the rating and the mechanical checks. A run whose code fails `check_code_files`
is refused unless you pass `--force`, so "known good" means something.

Then commit it.

## Why this is empty

Everything here must be a **real pipeline run, captured verbatim**. The sessions
that built this tooling had no access to a model — no local Ollama, and network
policy blocked the live orchestrator — so there was nothing genuine to capture.
Writing plausible-looking "example output" by hand would have produced material
that looks like swarm output but isn't, sitting in the folder you reach for when
a take goes wrong. That is worse than an empty folder.

The two runs worth capturing first, both of which appear in the demo script:

- **`--demo-showcase`** — the Snake game. The visual money shot.
- **`--demo`** — the expense tracker, which carries memory across two iterations.
