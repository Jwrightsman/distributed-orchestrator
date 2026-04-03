# Demo Recording Script

Record this with OBS or Windows Game Bar (Win+G). Show your terminal full-screen, dark background.

## Part 1: Local Pipeline (solo machine)

```bash
# Show the system is real
ollama list

# Pitch a task
py cli.py "Build a Python script that takes a CSV of student grades and generates a report card with GPA calculation"
```

Let it run. The CLI shows each step live:
- PLANNER decomposes into subtasks
- Each BUILDER prints output as it completes
- REVIEWER checks and assembles

Total time: ~5-8 min on 8GB CPU. Speed it up 4x in editing.

## Part 2: Start the Server + Dashboard

```bash
# Start the orchestrator
py -m uvicorn server:app --host 0.0.0.0 --port 8000
```

Open browser to http://localhost:8000/dashboard

Show:
- The dark UI with stats bar
- 0 nodes connected
- Pitch a task from the browser
- Watch the live activity feed update

## Part 3: Distributed (if you have a second machine)

On machine 2:
```bash
git clone https://github.com/Jwrightsman/distributed-orchestrator
pip install httpx
ollama pull gemma3:4b
py join.py http://[YOUR_IP]:8000
```

Show the dashboard update: "1 Node Online"
Pitch a task. Watch the builder tasks route to the second machine.

## Recording Tips

- Use a dark terminal theme
- Font size 14-16 so it's readable
- Show the dashboard side-by-side with the terminal if possible
- Keep it under 3 minutes (speed up the inference waiting)
- Add captions explaining what's happening at each step

## The Hook (first 5 seconds)

Start the video with the completed output — show the finished result first.
Then say/caption: "This was built by AI agents running on my laptop. No cloud. No API keys. Here's how."
Then show the process.
