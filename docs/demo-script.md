# Demo Recording Script

**Target:** 60–90 seconds final cut. Record ~30 minutes raw across the takes below,
then cut hard.
**Setup:** dark terminal theme, font size 16, 1920×1080. OBS or Win+G.
**Rule:** record every take as its own file. Do not try to do this in one pass.

---

## Before you press record — 15-minute prep

Do this in order. It removes every failure that has bitten a take so far.

1. **Free the machine.** Close Chrome, Slack, anything heavy. The pipeline needs
   the whole CPU; competition makes runs stall.
2. **RESTART Ollama, then warm it up.** Not optional — this is measured. Ollama
   gets progressively slower over a long session, and a call that takes 30
   minutes hits the timeout and kills the take.
   ```bash
   # Windows: quit Ollama from the system tray, then reopen it. Or:
   taskkill /IM ollama.exe /F
   start "" "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" serve
   py status.py
   ```
   Confirm it prints `Active: qwen3.5:4b`.

   > **The evidence:** across seven back-to-back `--demo` runs, duration climbed
   > 26 → 33 → 40 → 36 → 47 → 67 → 83 minutes (rank correlation with run order
   > **0.96**), and the last two failed on 30-minute model-call timeouts.
   > Restarting Ollama and immediately re-running the *same task* took it from
   > **83 minutes back to 34, clean** — no cooldown, so this is Ollama's session
   > state, not an overheating laptop. If you have been generating all day,
   > restart it before you roll.
3. **Decide which showcase you are filming.** There are now two, and they are
   for different shots. Both numbers are real runs checked in a real browser
   (`docs/showcase-ceiling.md`):

   | | measured | on camera |
   | --- | --- | --- |
   | `--demo-showcase chart` | **10/10**, ~22 min | **safe to generate live** |
   | `--demo-showcase` (Snake) | **2/10**, ~50 min | pre-generate only |

   **The chart is the one you can film being built.** It renders a small
   dataset as a labelled bar chart, and every run so far got all seven labels
   and all seven values right. That is what makes a "watch it build this right
   now" shot possible:
   ```bash
   py cli.py --demo-showcase chart
   ```

   **Pre-generate the Snake game** if you want it — do NOT generate it live, it
   is 2/10 and takes ~50 minutes:
   ```bash
   py cli.py --demo-showcase
   ```
   When it finishes, open the generated HTML and **click restart once so the
   snake is already moving**. Some runs open on a "GAME OVER" start screen;
   starting it first means the camera never sees that. A verified-playable copy
   is already committed at `docs/demo-assets/snake-game/` if a take goes wrong.
4. **Pre-run the memory demo** the same way (`py cli.py --demo`) if you want the
   iteration story — it is ~35 minutes on a freshly-restarted Ollama, so you are
   filming the *replay* of its output, not the wait.

   Measured over 8 runs: **6/8 clean overall — but 6/6 on a fresh Ollama and
   0/2 once it had been running 5+ hours.** Both failures were 30-minute
   model-call timeouts, not bad output. Follow prep step 2 and this shot is
   reliable. A failure is loud anyway: red panel, non-zero exit, obvious while
   filming rather than discovered in the edit.
5. **Start the server and check the dashboard looks right:**
   ```bash
   py -m uvicorn server:app --host 127.0.0.1 --port 8000
   ```
   > Use `127.0.0.1`, not `0.0.0.0`. That keeps the port off your network while
   > you record. Nothing is exposed to the internet.

---

## Shot list

### Shot 1 — The hook (0:00–0:08)

**On screen:** the finished Snake game, already playing, filling the frame.

**Say / caption:** *"A swarm of AI agents built this game. On my laptop. No
cloud, no API keys."*

Let the snake move for a full three seconds before cutting. Movement is the
proof — a static screenshot reads as fake.

> **Alternative hook, now that live generation is safe — your call, Jett.**
> Open on the *pitch being typed*, then cut to the finished bar chart appearing
> in the browser, and say *"I typed that thirty seconds ago"* (timelapse the
> ~22 minutes). The chart is measured 10/10, so a take is very unlikely to be
> wasted. It is a stronger claim than the game shot — the audience watches it
> happen instead of taking your word for it — but the game is the prettier
> image. Film both if you have the time; decide in the edit.

---

### Shot 2 — The MCP moment (0:08–0:25) ⭐ the differentiator

This is the shot nobody else has. Two windows side by side: **Claude Desktop on
the left, the dashboard on the right.**

1. Type into Claude: *"Pitch this to the swarm: build a CSV deduplication
   script."*
2. Claude calls `pitch_task`. **The dashboard reacts immediately** — the pitch
   appears in Live Activity.
3. Node cards light up and pulse as builders take subtasks.
4. Claude polls `get_job_status` and narrates progress in chat.

**Say / caption:** *"It's an MCP server. Any AI app can hand work to the swarm."*

Cut away while it works; come back for the result landing in Claude's chat.
Setup instructions are in [MCP.md](MCP.md) — do this take with the config
already working.

---

### Shot 3 — Parallel execution + credits (0:25–0:45)

**On screen:** dashboard, full frame.

Point the camera at these in order:
1. **PLAN** appears — several subtasks, some independent
2. Multiple **node cards pulse simultaneously** — that's parallel execution
   across machines, the whole thesis of the project
3. **Credits tick up** on the leaderboard as each node finishes

**Say / caption:** *"Subtasks run in parallel across every machine that joined.
Everyone who contributes compute earns credits."*

If a second machine is available, this is where it matters most — two node cards
lighting up beats one every time.

---

### Shot 4 — Memory (0:45–1:00)

**On screen:** terminal replay of `--demo`'s second pitch.

Show the moment the second pitch loads context from the first, then builds only
the new feature.

**Say / caption:** *"Pitch again and it remembers what it already built."*

---

### Shot 5 — It checks its own work (1:00–1:10)

**On screen:** terminal, the reviewer stage.

Show the rating appearing, and — if a run produced one — the reviser firing to
fix flagged issues. If you have a run where the code validator caught broken
code and repaired it, use that instead; it is the stronger story.

**Say / caption:** *"A reviewer grades it, and the code gets checked for real —
if it doesn't parse, it goes back for a fix."*

---

### Shot 6 — The CTA (1:10–1:25)

**On screen:** the landing page at `http://localhost:8000/` — live node count
visible, join command on screen.

**Say / caption:** *"It needs machines. If you've got a spare laptop, one command
joins the swarm."*

Show the one-liner large enough to read:

```bash
curl -fsSL https://raw.githubusercontent.com/Jwrightsman/distributed-orchestrator/master/install.sh | bash -s -- http://YOUR_ORCHESTRATOR:8000
```

If the public orchestrator is live by recording day, **put its real address on
screen here** — that single detail is the difference between "neat project" and
people actually joining.

End card: repo URL.

---

## Editing notes

- **Speed:** 6–8× everything except the four beats that need real time — the
  snake moving, node cards pulsing, credits ticking, the result landing in
  Claude's chat.
- **Captions over voiceover** if you don't like your recorded voice. Most of
  this audience watches muted.
- **Cut every wait.** No viewer needs to see a 20-minute reviewer call. The
  honest framing is "this takes minutes on volunteer hardware" in a caption,
  not real-time footage.
- **Length discipline:** if it's over 90 seconds, cut Shot 4 (memory) before
  cutting Shots 2 or 3. MCP and parallel execution are what make this different.

## If something breaks mid-take

- Pipeline fails with an error panel → that's the honest-failure path working;
  just re-run. Don't film the failure unless you want it as a "here's what real
  local models are like" beat.
- Dashboard shows 0 nodes → the node process dropped. Restart it with
  `py node.py --server http://127.0.0.1:8000` and re-take Shot 3.
- Game opens on "GAME OVER" → click restart, then start the shot (see prep #3).
