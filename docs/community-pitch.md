# Community Pitch Draft

Post this (adapted for each community) with your demo video.

---

## For ExoLabs Discord / LocalLLaMA / AI Communities

**Title:** My laptop just built an app using AI agents running on consumer hardware — no cloud, no API keys

**Body:**

I built a distributed AI orchestrator that runs entirely on local hardware. You pitch a task in plain English, and a pipeline of AI agents decomposes it, builds each piece, and assembles the final output. No OpenAI, no cloud compute, no API keys.

Here's what it does:

- **Planner agent** breaks your task into 3-5 subtasks with a dependency graph
- **Builder agents** execute each subtask in order, passing context forward
- **Reviewer agent** checks the combined output and assembles the final deliverable
- Everything runs through Ollama on local models (gemma3:4b on my 8GB laptop)

The interesting part: it supports **distributed execution**. Other machines can join as worker nodes with one command. The orchestrator keeps the planner and reviewer, but distributes the builder tasks across connected nodes. Falls back to local if nobody's connected.

Running on gemma3:4b because I'm on 8GB with no GPU. Imagine this with Gemma 4 26B MoE split across 4 machines via Exo.

**Demo:** [link to video]

**Repo:** https://github.com/Jwrightsman/distributed-orchestrator

**Want to connect a node?**
```
git clone https://github.com/Jwrightsman/distributed-orchestrator
pip install httpx
ollama pull gemma3:4b
python join.py http://[MY_IP]:8000
```

Looking for people who want to connect machines and stress-test this. The goal is a collectively-owned AI system where anyone can pitch an idea and a swarm of consumer devices builds it.

---

## Shorter version (for Discord channels with character limits)

Built a distributed AI orchestrator — pitch a task, AI agents decompose it, build it, and review it. Runs on local hardware via Ollama, no cloud. Supports worker nodes: other machines can join with one command and take builder tasks.

8GB laptop, gemma3:4b, no GPU. Still works.

Repo: https://github.com/Jwrightsman/distributed-orchestrator
Demo: [video link]

Looking for people to connect nodes and test distributed execution.
