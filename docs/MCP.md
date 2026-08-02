# MCP Interface — delegate tasks to the swarm from any AI app

The orchestrator ships an [MCP](https://modelcontextprotocol.io) server, so any
MCP client — Claude Desktop, Claude Code, and a growing list of agent apps —
can hand work to the swarm and pick up the result.

Five tools are exposed:

| Tool | What it does |
|---|---|
| `pitch_task(task, project_id?)` | Submit a task; returns a `job_id` immediately |
| `get_job_status(job_id)` | queued / running / complete / failed, with the subtask plan |
| `get_result(job_id)` | The final assembled output + extracted code file list |
| `list_projects()` | Persistent projects the swarm remembers |
| `continue_project(project_id, task)` | Iterate on a project with full memory of previous runs |

## 60-second setup (Claude Desktop)

1. **Requirements:** the repo cloned, `pip install -r requirements.txt` done,
   and the orchestrator running:

   ```bash
   py -m uvicorn server:app --host 0.0.0.0 --port 8000
   ```

2. **Open the Claude Desktop config file:**
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

3. **Add the server** (adjust the path to where you cloned the repo):

   ```json
   {
     "mcpServers": {
       "swarm": {
         "command": "py",
         "args": ["C:\\Users\\YOU\\distributed-orchestrator\\mcp_server.py"],
         "env": { "ORCHESTRATOR_URL": "http://localhost:8000" }
       }
     }
   }
   ```

   (On Mac/Linux use `"command": "python3"` and a normal path.)

4. **Restart Claude Desktop.** Ask Claude: *"Pitch this to the swarm: build a
   CSV deduplication script."* It will call `pitch_task`, poll
   `get_job_status`, and fetch the result with `get_result`.

## Claude Code

```bash
claude mcp add swarm -- py /path/to/distributed-orchestrator/mcp_server.py
```

## Remote orchestrator

Point at any orchestrator you can reach (LAN, Tailscale, or a public VM from
[DEPLOY.md](DEPLOY.md)):

```json
"env": {
  "ORCHESTRATOR_URL": "http://100.64.0.7:8000",
  "PITCH_KEY": "the-pitch-key-if-one-is-set"
}
```

The MCP server itself can also run as a network service (streamable HTTP on
`127.0.0.1:8765/mcp`) instead of stdio:

```bash
py mcp_server.py --http
```

## Notes

- A pipeline run takes **minutes** on CPU hardware. `pitch_task` returns
  immediately by design — the client polls `get_job_status` rather than
  holding a connection open.
- The MCP server never touches the pipeline: it is a thin adapter over the
  same async job API the dashboard uses (`/pitch/async`, `/jobs/{id}`,
  `/history/{ts}`).

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Could not reach the orchestrator" | Start it: `py -m uvicorn server:app --host 0.0.0.0 --port 8000` |
| "requires a pitch key" | Set `PITCH_KEY` in the `env` block to the orchestrator's `pitch_key` |
| Tools don't appear in Claude Desktop | Check the config file path and JSON syntax; fully quit and reopen the app |
| Rate limited | 5 pitches/minute per IP — wait a minute |
