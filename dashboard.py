"""
Live dashboard — watch the orchestrator work in your browser.

Shows connected nodes, active tasks, and pipeline progress in real-time.
Serves a web UI at http://localhost:8000/dashboard when the server runs.

This file adds dashboard routes to the FastAPI server.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Distributed AI Orchestrator</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    background: #08090C;
    color: #D8D8D8;
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    min-height: 100vh;
  }

  .header {
    padding: 28px 32px 20px;
    border-bottom: 1px solid rgba(255,255,255,0.06);
  }
  .header-tag {
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: 10px;
    color: #00FF88;
    letter-spacing: 3px;
    font-weight: 600;
    margin-bottom: 6px;
  }
  .header h1 {
    font-size: 26px;
    font-weight: 800;
    color: #F0F0F0;
    letter-spacing: -0.5px;
  }
  .header h1 span { color: #00FFAA; }
  .header-sub {
    color: #666;
    font-size: 13px;
    margin-top: 6px;
  }

  .stats-bar {
    display: flex;
    gap: 24px;
    padding: 16px 32px;
    border-bottom: 1px solid rgba(255,255,255,0.06);
    background: rgba(255,255,255,0.015);
  }
  .stat {
    display: flex;
    align-items: baseline;
    gap: 8px;
  }
  .stat-value {
    font-size: 22px;
    font-weight: 700;
    color: #F0F0F0;
    font-family: 'Consolas', monospace;
  }
  .stat-label {
    font-size: 11px;
    color: #666;
    text-transform: uppercase;
    letter-spacing: 1px;
  }

  .main {
    display: grid;
    grid-template-columns: 300px 1fr;
    min-height: calc(100vh - 140px);
  }

  .sidebar {
    border-right: 1px solid rgba(255,255,255,0.06);
    padding: 20px;
  }
  .sidebar h2 {
    font-size: 11px;
    color: #888;
    letter-spacing: 2px;
    text-transform: uppercase;
    margin-bottom: 14px;
    font-weight: 600;
  }

  .node-card {
    background: rgba(255,255,255,0.025);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 8px;
    padding: 12px 14px;
    margin-bottom: 8px;
    transition: border-color 0.2s;
  }
  .node-card.active { border-color: rgba(0,255,136,0.3); }
  .node-name {
    font-weight: 700;
    color: #E0E0E0;
    font-size: 13px;
    margin-bottom: 4px;
  }
  .node-meta {
    font-size: 11px;
    color: #666;
    font-family: 'Consolas', monospace;
  }
  .node-dot {
    display: inline-block;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: #00FF88;
    margin-right: 6px;
    box-shadow: 0 0 8px rgba(0,255,136,0.4);
  }
  .node-tasks {
    font-size: 11px;
    color: #00FFAA;
    margin-top: 4px;
  }

  .content {
    padding: 20px 28px;
    overflow-y: auto;
  }
  .content h2 {
    font-size: 11px;
    color: #888;
    letter-spacing: 2px;
    text-transform: uppercase;
    margin-bottom: 14px;
    font-weight: 600;
  }

  .pipeline-card {
    background: rgba(255,255,255,0.02);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 10px;
    padding: 18px 20px;
    margin-bottom: 12px;
  }
  .pipeline-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;
  }
  .pipeline-task {
    font-weight: 700;
    color: #E0E0E0;
    font-size: 14px;
  }
  .pipeline-status {
    font-family: 'Consolas', monospace;
    font-size: 10px;
    letter-spacing: 1px;
    padding: 3px 10px;
    border-radius: 4px;
    font-weight: 600;
  }
  .status-running {
    color: #E8FF47;
    background: rgba(232,255,71,0.1);
    border: 1px solid rgba(232,255,71,0.2);
  }
  .status-complete {
    color: #00FF88;
    background: rgba(0,255,136,0.1);
    border: 1px solid rgba(0,255,136,0.2);
  }
  .status-pending {
    color: #666;
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.08);
  }

  .subtask-list { margin-top: 10px; }
  .subtask {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 0;
    border-top: 1px solid rgba(255,255,255,0.04);
  }
  .subtask:first-child { border-top: none; }
  .subtask-id {
    font-family: 'Consolas', monospace;
    font-size: 11px;
    color: #555;
    min-width: 24px;
  }
  .subtask-title {
    font-size: 13px;
    color: #BBBBBB;
    flex: 1;
  }
  .subtask-node {
    font-family: 'Consolas', monospace;
    font-size: 10px;
    color: #AA77FF;
  }
  .subtask-check {
    color: #00FF88;
    font-size: 14px;
  }
  .subtask-spinner {
    color: #E8FF47;
    font-size: 12px;
    animation: spin 1s linear infinite;
  }
  @keyframes spin {
    from { transform: rotate(0deg); }
    to { transform: rotate(360deg); }
  }

  .empty-state {
    text-align: center;
    padding: 60px 20px;
    color: #444;
  }
  .empty-state .icon { font-size: 40px; margin-bottom: 16px; opacity: 0.3; }
  .empty-state p { font-size: 14px; line-height: 1.6; }

  .pitch-form {
    display: flex;
    gap: 8px;
    margin-bottom: 24px;
  }
  .pitch-input {
    flex: 1;
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 8px;
    padding: 10px 14px;
    color: #E0E0E0;
    font-size: 14px;
    outline: none;
    transition: border-color 0.2s;
  }
  .pitch-input:focus { border-color: rgba(0,255,170,0.4); }
  .pitch-input::placeholder { color: #444; }
  .pitch-btn {
    background: rgba(0,255,170,0.12);
    border: 1px solid rgba(0,255,170,0.3);
    border-radius: 8px;
    padding: 10px 20px;
    color: #00FFAA;
    font-weight: 700;
    font-size: 13px;
    cursor: pointer;
    transition: all 0.15s;
    white-space: nowrap;
  }
  .pitch-btn:hover { background: rgba(0,255,170,0.2); }
  .pitch-btn:disabled { opacity: 0.4; cursor: not-allowed; }

  .log-entry {
    font-family: 'Consolas', monospace;
    font-size: 12px;
    color: #888;
    padding: 4px 0;
    border-bottom: 1px solid rgba(255,255,255,0.03);
  }
  .log-time { color: #555; margin-right: 8px; }
  .log-agent { color: #00FFAA; font-weight: 600; }
  .log-event { color: #AAAAAA; }
</style>
</head>
<body>

<div class="header">
  <div class="header-tag">DISTRIBUTED AI ORCHESTRATOR</div>
  <h1>The <span>Superconscious</span></h1>
  <div class="header-sub">Collectively-owned AI powered by consumer hardware</div>
</div>

<div class="stats-bar">
  <div class="stat">
    <span class="stat-value" id="stat-nodes">0</span>
    <span class="stat-label">Nodes Online</span>
  </div>
  <div class="stat">
    <span class="stat-value" id="stat-tasks">0</span>
    <span class="stat-label">Tasks Pending</span>
  </div>
  <div class="stat">
    <span class="stat-value" id="stat-models">-</span>
    <span class="stat-label">Models Available</span>
  </div>
  <div class="stat">
    <span class="stat-value" id="stat-status">-</span>
    <span class="stat-label">Ollama</span>
  </div>
</div>

<div class="main">
  <div class="sidebar">
    <h2>Network Nodes</h2>
    <div id="nodes-list">
      <div class="empty-state">
        <div class="icon">-</div>
        <p>No nodes connected yet.<br>Run <code style="color:#00FFAA">py node.py --server http://YOUR_IP:8000</code> on another machine to join.</p>
      </div>
    </div>
  </div>

  <div class="content">
    <h2>Pitch a Task</h2>
    <div class="pitch-form">
      <input type="text" class="pitch-input" id="pitch-input"
             placeholder="Describe what you want built...">
      <button class="pitch-btn" id="pitch-btn" onclick="pitchTask()">Pitch</button>
    </div>

    <h2>Live Activity</h2>
    <div id="event-log" style="margin-bottom:24px;max-height:200px;overflow-y:auto;"></div>

    <h2>Pipeline Runs</h2>
    <div id="pipeline-log">
      <div class="empty-state">
        <div class="icon">-</div>
        <p>No active pipelines.<br>Pitch a task above to see agents work.</p>
      </div>
    </div>

    <h2 style="margin-top:24px">History</h2>
    <div id="history-list">
      <div class="empty-state"><p>No past runs yet.</p></div>
    </div>
  </div>
</div>

<script>
// Poll health + nodes every 3 seconds
async function refresh() {
  try {
    const [health, nodes] = await Promise.all([
      fetch('/health').then(r => r.json()),
      fetch('/nodes').then(r => r.json()),
    ]);

    document.getElementById('stat-nodes').textContent = nodes.count;
    document.getElementById('stat-tasks').textContent = health.tasks_pending;
    document.getElementById('stat-models').textContent = health.models.length;
    document.getElementById('stat-status').textContent = health.ollama;

    const nodesList = document.getElementById('nodes-list');
    if (nodes.count === 0) {
      nodesList.innerHTML = `
        <div class="empty-state">
          <div class="icon">-</div>
          <p>No nodes connected yet.<br>Run <code style="color:#00FFAA">py node.py --server http://YOUR_IP:8000</code> on another machine to join.</p>
        </div>`;
    } else {
      nodesList.innerHTML = nodes.nodes.map(n => `
        <div class="node-card active">
          <div class="node-name"><span class="node-dot"></span>${n.node_id}</div>
          <div class="node-meta">${n.platform} / ${n.machine}</div>
          <div class="node-meta">${n.model}</div>
          <div class="node-tasks">${n.tasks_completed} tasks completed</div>
        </div>
      `).join('');
    }
  } catch(e) {
    document.getElementById('stat-status').textContent = 'offline';
  }
}

async function pitchTask() {
  const input = document.getElementById('pitch-input');
  const btn = document.getElementById('pitch-btn');
  const task = input.value.trim();
  if (!task) return;

  btn.disabled = true;
  btn.textContent = 'Working...';

  const pipelineLog = document.getElementById('pipeline-log');
  const pipelineId = Date.now();

  // Show running state
  pipelineLog.innerHTML = `
    <div class="pipeline-card" id="pipeline-${pipelineId}">
      <div class="pipeline-header">
        <div class="pipeline-task">${task}</div>
        <div class="pipeline-status status-running">RUNNING</div>
      </div>
      <div class="log-entry">
        <span class="log-time">${new Date().toLocaleTimeString()}</span>
        <span class="log-agent">PLANNER</span>
        <span class="log-event"> Decomposing task...</span>
      </div>
    </div>
  ` + pipelineLog.innerHTML;

  try {
    const endpoint = document.getElementById('stat-nodes').textContent !== '0'
      ? '/pitch/distributed' : '/pitch';
    const resp = await fetch(endpoint, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({task}),
    });
    const result = await resp.json();

    // Show completed state
    const card = document.getElementById(`pipeline-${pipelineId}`);
    if (card) {
      let subtasksHtml = '<div class="subtask-list">';
      result.plan.forEach(st => {
        subtasksHtml += `
          <div class="subtask">
            <span class="subtask-id">${st.id}</span>
            <span class="subtask-title">${st.title}</span>
            <span class="subtask-check">done</span>
          </div>`;
      });
      subtasksHtml += '</div>';

      card.innerHTML = `
        <div class="pipeline-header">
          <div class="pipeline-task">${task}</div>
          <div class="pipeline-status status-complete">${result.mode === 'distributed' ? 'DISTRIBUTED' : 'COMPLETE'}</div>
        </div>
        ${subtasksHtml}
        <div class="log-entry">
          <span class="log-time">${new Date().toLocaleTimeString()}</span>
          <span class="log-event">Output saved to ${result.project_dir}</span>
        </div>
      `;
    }
  } catch(e) {
    const card = document.getElementById(`pipeline-${pipelineId}`);
    if (card) {
      card.querySelector('.pipeline-status').className = 'pipeline-status status-pending';
      card.querySelector('.pipeline-status').textContent = 'FAILED';
    }
  }

  btn.disabled = false;
  btn.textContent = 'Pitch';
  input.value = '';
}

// Enter key to pitch
document.getElementById('pitch-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') pitchTask();
});

// ── Event log (live updates during pipeline runs) ──
let eventCursor = 0;
const EVENT_LABELS = {
  pitch: {agent: 'PITCH', color: '#E8FF47'},
  plan: {agent: 'PLANNER', color: '#E8FF47'},
  build: {agent: 'BUILDER', color: '#00FFAA'},
  review_start: {agent: 'REVIEWER', color: '#AA77FF'},
  complete: {agent: 'DONE', color: '#00FF88'},
};

async function pollEvents() {
  try {
    const resp = await fetch(`/events?since=${eventCursor}`);
    const data = await resp.json();
    const log = document.getElementById('event-log');

    data.events.forEach(ev => {
      const label = EVENT_LABELS[ev.type] || {agent: ev.type.toUpperCase(), color: '#888'};
      const t = new Date(ev.time).toLocaleTimeString();
      let msg = '';
      if (ev.type === 'pitch') msg = `Task pitched: "${ev.task}"`;
      else if (ev.type === 'plan') msg = `Decomposed into ${ev.subtasks.length} subtasks: ${ev.subtasks.join(', ')}`;
      else if (ev.type === 'build') msg = `Subtask ${ev.subtask_id} complete: ${ev.subtask}`;
      else if (ev.type === 'review_start') msg = 'Reviewing combined output...';
      else if (ev.type === 'complete') msg = `Pipeline complete. Saved to ${ev.project_dir}`;

      log.innerHTML += `<div class="log-entry">
        <span class="log-time">${t}</span>
        <span class="log-agent" style="color:${label.color}">${label.agent}</span>
        <span class="log-event"> ${msg}</span>
      </div>`;
    });

    eventCursor += data.events.length;
    if (data.events.length > 0) log.scrollTop = log.scrollHeight;
  } catch(e) {}
}

// ── History ──
async function loadHistory() {
  try {
    const resp = await fetch('/history');
    const data = await resp.json();
    const el = document.getElementById('history-list');

    if (data.count === 0) {
      el.innerHTML = '<div class="empty-state"><p>No past runs yet.</p></div>';
      return;
    }

    el.innerHTML = data.runs.map(r => `
      <div class="pipeline-card" style="padding:12px 16px;margin-bottom:6px;">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <div style="font-size:13px;color:#BBBBBB;flex:1;">${r.task}</div>
          <div style="display:flex;gap:12px;align-items:center;">
            <span style="font-family:Consolas,monospace;font-size:11px;color:#555;">${r.subtask_count} subtasks</span>
            <span style="font-family:Consolas,monospace;font-size:11px;color:#444;">${r.timestamp}</span>
          </div>
        </div>
      </div>
    `).join('');
  } catch(e) {}
}

// Start polling
refresh();
loadHistory();
setInterval(refresh, 3000);
setInterval(pollEvents, 2000);
setInterval(loadHistory, 15000);
</script>
</body>
</html>"""


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML
