# Demo Recording Script

**Target:** 60–90 seconds final cut. Record ~30 minutes raw across the takes below,
then cut hard.
**Setup:** dark terminal theme, font size 16, 1920×1080. OBS or Win+G.
**Dashboard theme:** it has a light/dark toggle (bottom of the sidebar). Pick
**dark** to match the terminal, set it before the first take, and don't touch it
again — a theme that changes between shots reads as two different products.
**Rule:** record every take as its own file. Do not try to do this in one pass.

---

## Before you press record — budget half a day, not 15 minutes

Do this in order. It removes every failure that has bitten a take so far.

**This section used to claim "15-minute prep" and that was badly wrong** — it
counted the setup steps and not the generation, which is most of the clock:

| step | time | can you skip it? |
| --- | --- | --- |
| 1, 2, 5, 6, 7 — machine, Ollama, event feed, server, node | **~10 min** | no |
| 3 — generate the chart showcase | **~22 min** | no, this is Shot 1 |
| 3 — pre-generate the Snake game | **~50 min** | yes — a verified-playable copy is committed at `docs/demo-assets/snake-game/` |
| 4 — pre-run `--demo` for the memory beat | **~35 min** | yes, if you cut Shot 4 |

**Doing all of it is about 2 hours before you press record**, and every minute
of it is inference you cannot speed up on a CPU. Start it in the morning. The
short path — chart only, committed Snake, no memory shot — is about 35 minutes.

1. **Free the machine.** Close Chrome, Slack, anything heavy. The pipeline needs
   the whole CPU; competition makes runs stall.
2. **RESTART Ollama, then warm it up.** Not optional — this is measured. Ollama
   gets progressively slower over a long session, and a call that takes 30
   minutes hits the timeout and kills the take.
   The reliable way is the system tray: **quit Ollama, then reopen it.** If you
   would rather type it, this is PowerShell — the shell that opens by default.
   `%LOCALAPPDATA%` does *not* expand in PowerShell, which is why the older
   version of this step silently did nothing:
   ```powershell
   taskkill /IM ollama.exe /F
   Start-Process "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" serve
   py status.py
   ```
   Confirm it prints `Active:  qwen3.5:4b` under `Ollama`.

   > **The evidence:** across seven back-to-back `--demo` runs, duration climbed
   > 26 → 33 → 40 → 36 → 47 → 67 → 83 minutes (rank correlation with run order
   > **0.96**), and the last two failed on 30-minute model-call timeouts.
   > Restarting Ollama and immediately re-running the *same task* took it from
   > **83 minutes back to 34, clean** — no cooldown, so this is Ollama's session
   > state, not an overheating laptop. If you have been generating all day,
   > restart it before you roll.
3. **Generate both showcases.** You are filming both — the chart live in Shot 1,
   the game as the honest-limits beat in Shot 6. No decision to make here; the
   numbers below are why (real runs, checked in a real browser,
   `docs/showcase-ceiling.md`):

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
5. **Clear the old activity feed.** Live Activity is read from `events.db` and
   is *historical* — it opens showing whatever ran last, which in a rehearsal
   was five entries from twelve days earlier, timestamped `12:42 AM`, sitting
   directly above the fresh ones. On camera that reads as a bug. Rename the file
   before you start; it is recreated empty:
   ```powershell
   Rename-Item events.db events.db.bak
   ```
   This only clears the dashboard's event feed. Past runs in `output/`, the
   ledger and the Gallery are separate files and are untouched.

6. **Start the server:**
   ```bash
   py -m uvicorn server:app --host 127.0.0.1 --port 8000
   ```
   > Use `127.0.0.1`, not `0.0.0.0`. That keeps the port off your network while
   > you record. Nothing is exposed to the internet.
   >
   > **The trade-off, and it shows on camera:** the landing page prints a join
   > command built from the address you are browsing, so on `127.0.0.1` Shot 7
   > will show `python join.py http://127.0.0.1:8000` — an address that means
   > "your own machine" to every viewer. Either film Shot 7 against the public
   > orchestrator, or overlay the real address in the edit. Do not "fix" it by
   > binding `0.0.0.0`; that puts the port on whatever Wi-Fi you are on.

7. **Start a worker node — Shots 2 and 3 are blank without one.** This is the
   step the script used to be missing entirely. The dashboard's node cards, the
   credits and the whole parallel-execution beat exist only when a node is
   connected. In a second terminal:
   ```bash
   py node.py --server http://127.0.0.1:8000 --node-id Laptop-1
   ```
   Wait for `Connected. Welcome, Laptop-1.` and check the dashboard's **Nodes**
   view shows the card before you record anything.

---

## Shot list

### Shot 1 — The hook (0:00–0:10)

**Film this one. Do not deliberate on the day — the reasoning is below and the
decision is already made.**

**On screen:** you typing the pitch, then the finished bar chart appearing in
the browser. Timelapse the wait; land on the chart full-frame.

> **Terminal and browser only — keep the dashboard out of this shot.**
> `cli.py` runs the pipeline in-process and never talks to the server, so the
> dashboard sits completely still through the whole of Shot 1. (Checked at
> runtime, not by reading: importing `cli` never loads the server's event
> module.) The dashboard's turn is Shot 3, which is driven by a pitch made
> *through* the server.

**Say / caption:** *"I typed that a few minutes ago. A swarm of AI agents on my
own machines built it. No cloud, no API keys."*

Hold on the finished chart for three full seconds — long enough to read the
labels. Labels are the proof here: they show it read real data rather than drew
a pretty picture.

**Why this and not the Snake game**, settled by measurement so you don't have to
re-litigate it at 1am:

| | measured | what the shot claims |
| --- | --- | --- |
| chart, generated live | **10/10** | "watch it happen" — audience sees it |
| Snake game, pre-made | 2/10 | "here's what it made" — take my word |

The game is the prettier image, and it is the weaker claim: you would be showing
a finished artifact and asserting the swarm made it. The chart is watched being
made. It is also the only one of the two you can safely generate on camera.

**Keep the game for Shot 6 (the honest-limits beat, below).** It is worth more
there — "here is the thing it only gets right 2 times in 10" buys more
credibility with this audience than a prettier hook does.

---

### Shot 2 — The MCP moment (0:10–0:28) ⭐ the differentiator

This is the shot nobody else has. Two windows side by side: **Claude Desktop on
the left, the dashboard on the right.**

1. Type into Claude: *"Pitch this to the swarm: build a CSV deduplication
   script."*
2. Claude calls `pitch_task`. **The dashboard reacts immediately** — the pitch
   appears in Live Activity on **Overview**. Verified: MCP posts to the same
   `/pitch/async` route the dashboard's own Pitch button uses, and a rehearsal
   pitch showed `PITCH` → `PLANNER Decomposed into 5 subtasks: …` in the feed.
3. Node cards light up as builders take subtasks — **that is the Nodes view**,
   not Overview. Decide before you roll whether the right-hand window sits on
   Overview (the feed narrates itself, better for this shot) or Nodes (the
   machine, better for Shot 3). Trying to show both means switching views on
   camera while Claude is talking. Overview is the better choice here.
4. Claude polls `get_job_status` and narrates progress in chat.

> **Expect a wait between step 2 and step 3.** The planner runs first, on the
> orchestrator, and it took **82, 97 and 181 seconds** across the three rehearsal runs before the
> first subtask was even queued. Nothing appears on the node in that window.
> Cut it in the edit; do not re-take thinking it hung.

**Say / caption:** *"It's an MCP server. Any AI app can hand work to the swarm."*

Cut away while it works; come back for the result landing in Claude's chat.
Setup instructions are in [MCP.md](MCP.md) — do this take with the config
already working.

---

### Shot 3 — Parallel execution + credits (0:28–0:45)

**On screen:** the dashboard — but **three views, not one frame.** The dashboard
was rebuilt with a sidebar, and what this shot needs now lives in three places.
Switching between them on camera is fine and reads as a tour; expecting it all
in one frame is what will waste a take.

| beat | where it is now | what you see |
| --- | --- | --- |
| 1. the plan | **Overview** → Live Activity | `PLANNER Decomposed into 5 subtasks: …` with the titles |
| 2. the machine working | **Nodes** | the node card, with `▶ CLI Wrapper and Main Script` under it, changing as it moves through subtasks |
| 3. credits | **Nodes** (on the card) or **Guild** (the leaderboard) | `3 tasks · 15 credits`, climbing |

The node card carries the credits itself, so beats 2 and 3 are one shot in the
**Nodes** view. Use **Guild** only if you want the leaderboard framing.

**Say / caption:** *"Subtasks run in parallel across every machine that joined.
Everyone who contributes compute earns credits."*

> **Read this before filming: with one machine, "parallel" is not on screen.**
> The planner does produce independent subtasks and they are all queued at
> once — but a single node takes them **one at a time**, so you get one card
> going busy → idle → busy, not several pulsing together. Rehearsed on a real
> run: five subtasks, one node, strictly sequential.
>
> So either **get a second machine before this shot** (an IU lab machine, a
> friend's laptop for an evening, or the Hetzner VM joined as a node), or
> change the caption to something the footage actually supports — *"subtasks
> are handed out to whatever machines are online"* — and let the parallel claim
> rest on the README. Do not narrate parallel execution over one node. This
> audience will notice, and Shot 6 spends real credibility to buy exactly the
> trust that would cost.

---

### Shot 4 — Memory (0:45–0:57)

**On screen:** terminal replay of `--demo`'s second pitch.

Show the moment the second pitch loads context from the first, then builds only
the new feature.

**Say / caption:** *"Pitch again and it remembers what it already built."*

---

### Shot 5 — It checks its own work (0:57–1:08)

**On screen:** terminal, the reviewer stage.

Show the rating appearing, and — if a run produced one — the reviser firing to
fix flagged issues. If you have a run where the code validator caught broken
code and repaired it, use that instead; it is the stronger story.

**Say / caption:** *"A reviewer grades it, and the code gets checked for real —
if it doesn't parse, it goes back for a fix."*

---

### Shot 6 — What it can't do (1:08–1:18)

**This shot buys more credibility than any other one here.** r/LocalLLaMA has
seen a hundred demos that only show the good take. Almost none show the number.

**On screen:** the Snake game — a working one from `docs/demo-assets/snake-game/`
— then cut to the measured line.

**Say / caption:** *"It made this game too. But only 2 times out of 10 — I ran
it ten times and counted. The chart was 10 out of 10. Tightly-coupled code is
where a 4B swarm falls over, and I'd rather show you that than hide it."*

Put the two numbers on screen as text. If you want one more beat: *"28 test
pitches, about 57% come back actually runnable."*

Do not apologise over this shot. Stated plainly, it is the most trustworthy
fifteen seconds in the video.

---

### Shot 7 — The CTA (1:18–1:30)

**On screen:** the landing page at `http://localhost:8000/` — live node count
visible, join command on screen.

**Say / caption:** *"It needs machines. If you've got a spare laptop, one command
joins the swarm."*

> **Two things to sort out before this take, both found in rehearsal.**
>
> **1. The address on the page is the address you browsed.** The landing page
> builds its join command from the URL bar, so on the local server it renders
> `python join.py http://127.0.0.1:8000` — which tells every viewer to join
> their own laptop. Film this shot against **the public orchestrator** if it is
> live, or overlay the real address in the edit. It is the single most
> load-bearing frame in the video: it is the one people act on.
>
> **2. The page and the caption show different commands.** The page shows
> `python join.py <address>`; the one-liner below is the `curl … install.sh`
> form. Both work — `join.py` assumes the repo is already cloned, `install.sh`
> does the cloning for you — but showing one and reading the other is how a
> viewer ends up running neither. **Pick the `install.sh` one-liner for the
> voiceover and the end card**, since a stranger has not cloned anything yet.

Show the one-liner large enough to read:

```bash
curl -fsSL https://raw.githubusercontent.com/Jwrightsman/distributed-orchestrator/master/install.sh | bash -s -- http://YOUR_ORCHESTRATOR:8000
```

Windows viewers need the PowerShell form instead — put both on the end card:

```powershell
$env:SWARM_SERVER="http://YOUR_ORCHESTRATOR:8000"; irm https://raw.githubusercontent.com/Jwrightsman/distributed-orchestrator/master/install.ps1 | iex
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
- **Length discipline:** the shot list above lands at exactly 1:30. If a take
  runs long, cut in this order — **Shot 4 (memory), then Shot 5 (self-check)**.
  Never cut Shots 2, 3 or 6: MCP and parallel execution are what make this
  different from every other agent demo, and Shot 6 (the honest 2/10) is what
  makes the rest of the numbers believable. Cutting the limitation to fit a
  runtime is how a credible video becomes a marketing one.

## If something breaks mid-take

- Pipeline fails with an error panel → that's the honest-failure path working;
  just re-run. Don't film the failure unless you want it as a "here's what real
  local models are like" beat.
- Dashboard shows 0 nodes → check the node's own terminal **before** you
  restart anything. If it is printing `TASK …` it is alive and working, and
  restarting it throws away the build in progress. If it really has dropped:
  `py node.py --server http://127.0.0.1:8000 --node-id Laptop-1`, then re-take
  Shot 3.
  > Until Aug 14 this happened *by itself* on any subtask longer than 90
  > seconds: streamed tokens didn't count as a heartbeat, so the server evicted
  > a working node, reclaimed its subtask, and paid it nothing for the build it
  > went on to finish. Fixed. If you see it again on a machine running older
  > code, that is the cause — update the node.
- Node card shows `▶ <subtask>` and nothing changes for several minutes → that
  is normal. A builder call is minutes of CPU inference. Check the node's
  terminal for the elapsed counter rather than guessing from the dashboard.
- Game opens on "GAME OVER" → click restart, then start the shot (see prep #3).

---

## Rehearsal notes — what has actually been executed

This script was run start to finish for the first time on **Aug 14, 2026**, on
Jett's machine with real inference, and the fixes above came out of it. What was
executed, so the next person knows what is verified and what is still on trust:

**Executed and verified:** the Ollama check (prep 2), the server start (prep 6),
the node join (prep 7), a real pitch through the dashboard driving Live Activity,
node cards, and credits, the Nodes and Guild views, and the landing page in
Shot 7. Three end-to-end pipeline runs.

**Found by running it, and fixed in the code rather than the script:**

- A node was **evicted mid-build** and its subtask reclaimed, because streamed
  tokens did not count as a heartbeat. On a 329-second build the node was paid
  **+0 credits** for work it completed, and the dashboard showed 0 nodes while
  the node's terminal showed it building. After the fix, a 138-second build kept
  the node online and paid it. This would have happened on camera.
- The **one-line join could never finish** — `curl … | bash` left join.py with a
  pipe for stdin, so its consent prompt refused. That is Shot 7's payoff.

**Not executed, and still on trust:** Shot 1's live chart generation (~22 min),
Shot 2's Claude Desktop MCP take (needs Claude Desktop configured), Shot 4's
`--demo` memory run (~35 min), and Shot 6's Snake generation. Their *mechanics*
are verified — the MCP route is the same one the rehearsal drove — but the
takes themselves have not been filmed.

**The planner is not deterministic.** The same pitch, "Build a CSV deduplication
script", decomposed into **5 subtasks on one run and 1 on the next** (a third pitch
gave 4). Planner latency varied just as much: 82 s, 97 s, 181 s. Shot 3
wants a run with several subtasks; if you get a one-subtask plan, re-pitch
rather than trying to film it.
