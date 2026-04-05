# Two-Laptop Video Setup Guide

Everything you need to record the distributed demo video.

**What the video shows:**
- Two laptops working as nodes in real time
- Planner decomposes a task → builder tasks route to both machines
- Guild leaderboard ticking up credits as each machine completes work
- Second pitch loads memory from the first run
- Auto-reviser fires and fixes its own issues

---

## Before you start

Both machines need:
1. Python 3.12+ installed
2. [Ollama](https://ollama.com) installed and running
3. The repo cloned: `git clone https://github.com/Jwrightsman/distributed-orchestrator`
4. Dependencies: `pip install fastapi uvicorn httpx rich`
5. Model pulled: `ollama pull gemma3:4b`

Both machines must be on the **same Wi-Fi network**.

---

## Step-by-step setup

### Your machine (Machine 1 — the host)

Open **3 separate terminal windows**.

**Terminal 1 — Start the server:**
```bash
py -m uvicorn server:app --host 0.0.0.0 --port 8000
```

> ⚠️ **Security note:** `--host 0.0.0.0` makes the server visible to every device on your Wi-Fi.
> This is required for Machine 2 to connect. Only run this on a trusted home or private network.
> Do NOT run this on public Wi-Fi (coffee shops, airports, hotels).

Wait until you see: `Uvicorn running on http://0.0.0.0:8000`

**Terminal 2 — Join as a node (your machine earns credits too):**
```bash
py node.py --server http://localhost:8000 --node-id Laptop-1
```

Wait until you see: `Registered as Laptop-1`

**Terminal 3 — This is where you'll run the demo:**
Leave this open. You'll use it in the recording.

---

### Friend's machine (Machine 2 — the worker node)

Find your IP address first. On your machine, run:
```bash
# Windows:
ipconfig
# Look for "IPv4 Address" — something like 192.168.1.42
```

Tell your friend your IP. On their machine, one terminal:
```bash
py join.py http://192.168.1.42:8000
# Or if auto-discovery works (same network):
py join.py
```

Wait until they see: `Registered as` and a node ID.

---

## Verify both nodes are connected

On your machine, open http://localhost:8000/dashboard

You should see **2 nodes** in the Nodes section — both showing green ●

If you only see 1, Machine 2 hasn't connected yet. Wait a moment and refresh.

---

## Recording the demo

### Pre-recording checks

Run these on your machine before you start OBS:

```bash
py status.py --server   # should show 2 nodes, Ollama running
```

Expected output:
```
Ollama       running · gemma3:4b
Server       running · 2 nodes connected
```

If that looks good, do a fast dry run (skips pauses, no recording):
```bash
py cli.py --demo-fast
```

This should finish with a PASS rating and no errors. If it fails, fix it before recording.

---

### What to run for the recording

In Terminal 3 (the one you keep on screen):

```bash
py cli.py --demo-live
```

This is the live distributed version. It:
1. Shows both nodes connected
2. Submits Pitch 1 to the server → builder tasks route to both machines
3. Prints events live: PLAN, BUILDER 1 → Laptop-2, BUILDER 2 → Laptop-1, REVIEWER...
4. Pauses 3 seconds, then loads memory from Pitch 1
5. Submits Pitch 2 — same project, AI knows what was already built
6. Shows final Guild Standings with credits earned by each node

---

### Recording setup (OBS or Windows Game Bar)

- **OBS:** Scene → Display Capture → your terminal window
- **Windows Game Bar:** Win+G → Start recording
- Resolution: 1920×1080
- Terminal theme: dark (One Dark, Catppuccin Mocha, or Dracula)
- Font: Consolas or Cascadia Code, size 16
- Make the terminal full screen before you hit record

---

## Troubleshooting

**Friend can't connect:**
- Check you're on the same Wi-Fi
- Try `py join.py http://YOUR_IP:8000` with your actual IP instead of auto-discovery
- Make sure Windows Firewall isn't blocking port 8000:
  `netsh advfirewall firewall add rule name="Orchestrator" dir=in action=allow protocol=TCP localport=8000`

**Tasks all run locally (not on friend's machine):**
- Their node might not have registered. Check the dashboard Nodes panel.
- If their node shows up but tasks still run locally, restart their `join.py`

**Demo fails with timeout:**
- gemma3:4b on CPU takes 30–90s per agent call. The demo has a 600s timeout.
- If it still times out, the model may have gotten stuck. Restart Ollama: `ollama serve`

**Only 1 node showing in dashboard:**
- Refresh the dashboard (F5)
- Check Terminal 2 on your machine — the node.py process must still be running
- Re-run: `py node.py --server http://localhost:8000 --node-id Laptop-1`

---

## After recording

Edit the raw video:
- Speed up inference sections 6–8x
- Keep real-time: the PLAN reveal, the REVIEWER rating badge, and the Guild Standings
- Add captions over key moments (most viewers watch without sound):
  - When PLAN prints: *"Decomposed into parallel subtasks"*
  - When 2 builders run simultaneously: *"Both machines working in parallel"*
  - When Pitch 2 starts: *"Same project — it remembers everything"*
  - When standings show: *"Every machine earns credits for its compute"*

Target length: 60–90 seconds final cut.

Post with the thread from `community-pitch.md`.
