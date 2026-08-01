# Demo Recording Script

**Target length:** 60–90 seconds final cut (record ~8–10 min raw, cut in editing)
**Setup:** Terminal full-screen, dark theme, font size 16. OBS or Windows Game Bar (Win+G).

---

## The MCP Moment ("I ask my AI; it delegates to the swarm")

Candidate opening hook — an AI app handing work to the swarm hits harder than a
typed command. Requires docs/MCP.md setup done beforehand and the orchestrator
running with the dashboard visible on a second screen/window.

1. **On screen:** Claude Desktop (or Claude Code) chat + the dashboard side by side.
2. **Type to Claude:** *"Pitch this to the swarm: build a retro Snake game as a
   single HTML file with neon styling."*
3. **What the camera catches:** Claude calls `pitch_task` → the pitch appears on
   the dashboard instantly → node cards light up as builders take subtasks →
   Claude polls `get_job_status` and narrates progress → `get_result` brings the
   finished code back into the chat.
4. **Caption:** *"Any AI app can delegate to the swarm — it's just an MCP server."*

Record this segment separately; if the timing drags, cut to the dashboard while
the swarm works and come back for the result landing in chat.

---

## The Hook (first 3 seconds of final video)

**Start with the finished output already on screen** — the completed expense tracker code, scrolled to show the main logic. Let it sit for 3 seconds.

Caption/voiceover: *"This was built by AI agents running on my laptop. No cloud. No API keys. Here's how."*

Then cut to the beginning.

---

## Part 1: CLI Demo — Pipeline + Project Memory

This is the core story. Run this one command and let it play:

```bash
py cli.py --demo
```

This runs the full automated demo sequence:
1. Creates project `demo-expense-tracker`
2. Runs **Pitch 1**: "Build a Python expense tracker with categories, date tracking, and a spending summary report"
3. Shows the planner decomposing the task into subtasks
4. Shows each builder agent streaming output live
5. Shows the reviewer assembling and rating
6. Pauses 3 seconds so the viewer can read the output
7. Runs **Pitch 2**: "Add a monthly budget feature that warns when spending exceeds the budget"
8. The planner loads memory from Pitch 1 — knows what's already built
9. Prints a summary: both iterations + memory file size

**What to highlight in editing:**

- When the PLAN prints: zoom in briefly to show the parallel structure (independent subtasks vs. dependent ones)
- When BUILDER output streams: let a few seconds of live tokens play — this is the visual proof it's actually running
- When REVIEWER prints: pause on the rating badge
- When Pitch 2 starts: caption *"Same project — it remembers everything"*
- The final summary panel: hold 3 seconds

**Total raw runtime:** ~10–15 minutes on 8GB CPU. Speed up 6–8x in editing except the hook moments above.

---

## Part 2: Dashboard Demo — Web UI

Open a second terminal, start the server, then switch to the browser:

```bash
py -m uvicorn server:app --host 127.0.0.1 --port 8000
```

> ⚠️ Use `127.0.0.1` not `0.0.0.0` for screen recording — no ports exposed to your network.

Open **http://localhost:8000/dashboard**

**What to show (30 seconds of screen time):**

1. **Stats bar** — Nodes Online, Tasks Pending, Ollama status
2. **Live tab** — type a short pitch in the input box, hit Enter
   - Watch the stage bar progress: `Plan → Build → Review`
   - Watch the elapsed timer tick up
   - Watch the token stream box appear with live output
3. **Gallery tab** — show the completed run from Part 1
   - Click "Share ↗" — copy the share link
   - Open `/share/{timestamp}` in browser — show the beautiful shareable card
   - Click "Fork & continue" to show the fork mechanic
4. **History tab** — show the PASS badge and DIST mode badge if applicable

**Caption for the share card moment:** *"Every project gets a shareable link. Anyone can fork it and continue it on their own machine."*

---

## Part 3: Node Join (if you have a second machine)

Optional — only add this if you have another machine available.

On the second machine:
```bash
py join.py   # auto-discovers orchestrator — no IP needed
```

Show the dashboard update: node count goes from 0 → 1, the node card appears with hardware info.

Pitch a task from the dashboard. Watch builder tasks route to the second machine — the node card shows "BUSY" and tokens stream back live.

**Caption:** *"Any machine with Ollama can join. Builder tasks route to all connected nodes in parallel."*

---

## Recording Tips

- **Dark terminal** — Windows Terminal with One Dark or Catppuccin Mocha theme
- **Font:** Consolas or Cascadia Code, size 16
- **Resolution:** 1920×1080 minimum
- **Don't talk through the AI output** — let the tokens stream in silence, it's more dramatic
- **Cut the waiting aggressively** — 6–8x speed for inference, real-time for the moments that matter (plan reveal, rating reveal, memory summary)
- **Captions over every key moment** — most people watch without sound
- **End card:** repo URL + `py cli.py --demo` command visible for 3 seconds

## Ideal post format

Upload the full 60–90 second cut as a native video to Twitter/X (not a link — native gets 10x more reach).

Post simultaneously to:
- Twitter/X with the thread from `community-pitch.md`
- r/LocalLLaMA (link post with the video, title matches Tweet 1)
- ExoLabs Discord #projects channel
- Hacker News (Show HN post — use the long pitch from `community-pitch.md`)
- Relevant Discord servers (AI builders, open source AI, etc.)

## Quick test before recording

Make sure the demo runs clean end-to-end:

```bash
py status.py          # confirm Ollama is running and model is pulled
py cli.py --demo-fast  # dry run without the 3-second pause — should complete without errors
```

If `--demo-fast` finishes with a PASS rating and the summary panel shows memory growth, you're ready to record with `--demo`.
