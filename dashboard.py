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
  .node-dot.busy {
    background: #E8FF47;
    box-shadow: 0 0 8px rgba(232,255,71,0.5);
    animation: dotPulse 1s ease-in-out infinite;
  }
  @keyframes dotPulse {
    0%, 100% { opacity: 1; }
    50%       { opacity: 0.4; }
  }
  .node-tasks {
    font-size: 11px;
    color: #00FFAA;
    margin-top: 4px;
  }
  .node-active-task {
    font-size: 11px;
    color: #E8FF47;
    margin-top: 3px;
    font-family: 'Consolas', monospace;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .node-breaker-badge {
    display: inline-block;
    font-family: 'Consolas', monospace;
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 1px;
    padding: 2px 7px;
    border-radius: 3px;
    margin-top: 4px;
    color: #FF5555;
    background: rgba(255,85,85,0.1);
    border: 1px solid rgba(255,85,85,0.25);
  }
  .credit-flash {
    position: absolute;
    right: 14px;
    top: 50%;
    transform: translateY(-50%);
    font-family: 'Consolas', monospace;
    font-size: 12px;
    font-weight: 700;
    color: #E8FF47;
    pointer-events: none;
    animation: creditPop 1.8s ease-out forwards;
  }
  @keyframes creditPop {
    0%   { opacity: 0; transform: translateY(-50%) scale(0.8); }
    20%  { opacity: 1; transform: translateY(-50%) scale(1.1); }
    70%  { opacity: 1; transform: translateY(-60%); }
    100% { opacity: 0; transform: translateY(-80%); }
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
    animation: fadeIn 0.3s ease;
  }
  .log-time { color: #555; margin-right: 8px; }
  .log-agent { color: #00FFAA; font-weight: 600; }
  .log-event { color: #AAAAAA; }

  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(4px); }
    to   { opacity: 1; transform: translateY(0); }
  }
  @keyframes pulseBorder {
    0%, 100% { border-color: rgba(232,255,71,0.2); box-shadow: none; }
    50%       { border-color: rgba(232,255,71,0.5); box-shadow: 0 0 16px rgba(232,255,71,0.08); }
  }
  .pipeline-card { animation: fadeIn 0.25s ease; }
  .pipeline-card.running { animation: fadeIn 0.25s ease, pulseBorder 2s ease-in-out infinite; }

  /* Stage progress shown while pipeline runs */
  .stage-bar {
    display: flex;
    gap: 6px;
    margin: 10px 0 4px;
  }
  .stage {
    flex: 1;
    height: 3px;
    border-radius: 2px;
    background: rgba(255,255,255,0.06);
    position: relative;
    overflow: hidden;
  }
  .stage.done   { background: #00FF88; }
  .stage.active { background: rgba(232,255,71,0.3); }
  .stage.active::after {
    content: '';
    position: absolute;
    left: -60%;
    top: 0;
    width: 60%;
    height: 100%;
    background: linear-gradient(90deg, transparent, #E8FF47, transparent);
    animation: shimmer 1.4s ease-in-out infinite;
  }
  @keyframes shimmer {
    from { left: -60%; }
    to   { left: 160%; }
  }
  .stage-labels {
    display: flex;
    gap: 6px;
    margin-bottom: 10px;
  }
  .stage-label {
    flex: 1;
    font-family: 'Consolas', monospace;
    font-size: 9px;
    letter-spacing: 1px;
    text-transform: uppercase;
    color: #444;
    text-align: center;
    transition: color 0.3s;
  }
  .stage-label.done   { color: #00FF88; }
  .stage-label.active { color: #E8FF47; }

  /* Code blocks in the output modal */
  .code-block {
    margin: 10px 0;
    border-radius: 6px;
    overflow: hidden;
    border: 1px solid rgba(255,255,255,0.08);
  }
  .code-block-header {
    background: rgba(255,255,255,0.05);
    padding: 5px 12px;
    font-family: 'Consolas', monospace;
    font-size: 10px;
    color: #666;
    letter-spacing: 1px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .code-block-copy {
    background: none;
    border: none;
    color: #555;
    cursor: pointer;
    font-size: 10px;
    padding: 0;
    transition: color 0.2s;
  }
  .code-block-copy:hover { color: #00FFAA; }
  .code-block pre {
    margin: 0;
    padding: 14px 16px;
    background: rgba(0,0,0,0.3);
    overflow-x: auto;
    font-family: 'Consolas', monospace;
    font-size: 12.5px;
    color: #C8E6C9;
    line-height: 1.55;
    white-space: pre;
  }
  .prose-text {
    font-size: 13px;
    color: #BBBBBB;
    line-height: 1.7;
    white-space: pre-wrap;
    font-family: 'Segoe UI', system-ui, sans-serif;
  }

  /* Tab navigation */
  .tab-nav {
    display: flex;
    gap: 4px;
    margin-bottom: 20px;
    border-bottom: 1px solid rgba(255,255,255,0.06);
    padding-bottom: 0;
  }
  .tab-btn {
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    padding: 8px 16px;
    font-size: 11px;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: #555;
    cursor: pointer;
    font-weight: 600;
    margin-bottom: -1px;
    transition: color 0.2s, border-color 0.2s;
  }
  .tab-btn:hover { color: #888; }
  .tab-btn.active { color: #00FFAA; border-bottom-color: #00FFAA; }

  /* Gallery grid */
  .gallery-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 14px;
  }
  .gallery-card {
    background: rgba(255,255,255,0.025);
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 10px;
    padding: 16px 18px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    transition: border-color 0.2s, background 0.2s;
  }
  .gallery-card:hover {
    border-color: rgba(0,255,170,0.2);
    background: rgba(255,255,255,0.035);
  }
  .gallery-task {
    font-size: 13px;
    font-weight: 700;
    color: #E0E0E0;
    line-height: 1.4;
  }
  .gallery-meta {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
  }
  .gallery-preview {
    font-family: 'Consolas', monospace;
    font-size: 11px;
    color: #666;
    line-height: 1.5;
    max-height: 80px;
    overflow: hidden;
    position: relative;
  }
  .gallery-preview::after {
    content: '';
    position: absolute;
    bottom: 0; left: 0; right: 0;
    height: 24px;
    background: linear-gradient(transparent, rgba(8,9,12,0.95));
  }
  .gallery-files {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }
  .gallery-file-chip {
    background: rgba(0,255,170,0.08);
    border: 1px solid rgba(0,255,170,0.18);
    border-radius: 4px;
    padding: 2px 8px;
    font-family: 'Consolas', monospace;
    font-size: 10px;
    color: #00FFAA;
  }
  .gallery-actions {
    display: flex;
    gap: 8px;
    margin-top: 2px;
  }
  .gallery-btn {
    flex: 1;
    background: none;
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 11px;
    color: #666;
    cursor: pointer;
    transition: all 0.15s;
    font-weight: 600;
  }
  .gallery-btn:hover { border-color: rgba(0,255,170,0.3); color: #00FFAA; }
  .gallery-btn.fork { border-color: rgba(0,255,170,0.2); color: #00FFAA; }
  .gallery-btn.fork:hover { background: rgba(0,255,170,0.08); }
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
  <div class="stat">
    <span class="stat-value" id="stat-done">-</span>
    <span class="stat-label">Tasks Done</span>
  </div>
  <div class="stat">
    <span class="stat-value" id="stat-latency">-</span>
    <span class="stat-label">Avg Latency</span>
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

    <h2 style="margin-top:24px">Guild Standings</h2>
    <div id="standings-list">
      <div class="empty-state"><p>No contributions yet.</p></div>
    </div>

    <div style="display:flex;justify-content:space-between;align-items:center;margin-top:24px;margin-bottom:14px;">
      <h2 style="margin:0;">Projects</h2>
      <button onclick="promptNewProject()" style="background:rgba(0,255,170,0.08);border:1px solid rgba(0,255,170,0.2);border-radius:5px;padding:3px 10px;font-size:10px;font-weight:700;color:#00FFAA;cursor:pointer;">+ New</button>
    </div>
    <div id="projects-list">
      <div class="empty-state"><p>No projects yet.</p></div>
    </div>
  </div>

  <div class="content">
    <div class="tab-nav">
      <button class="tab-btn active" id="tab-live" onclick="showTab('live')">Live</button>
      <button class="tab-btn" id="tab-gallery" onclick="showTab('gallery')">Gallery</button>
    </div>

    <!-- ── LIVE TAB ── -->
    <div id="view-live">
      <h2>Pitch a Task</h2>
      <!-- Hidden project context — set when "Continue" is clicked -->
      <div id="project-context" style="display:none;margin-bottom:10px;padding:8px 12px;background:rgba(0,255,170,0.06);border:1px solid rgba(0,255,170,0.2);border-radius:6px;justify-content:space-between;align-items:center;">
        <span style="font-size:12px;color:#00FFAA;">Continuing project: <strong id="project-context-name"></strong></span>
        <button onclick="clearProjectContext()" style="background:none;border:none;color:#555;cursor:pointer;font-size:11px;">✕ clear</button>
      </div>
      <input type="hidden" id="active-project-id" value="">
      <div class="pitch-form">
        <input type="text" class="pitch-input" id="pitch-input"
               placeholder="Describe what you want built...">
        <button class="pitch-btn" id="pitch-btn" onclick="pitchTask()">Pitch</button>
      </div>
      <!-- Quick-start templates -->
      <div id="task-templates" style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:20px;"></div>

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

    <!-- ── GALLERY TAB ── -->
    <div id="view-gallery" style="display:none;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
        <h2 style="margin-bottom:0;">Swarm Gallery</h2>
        <span style="font-size:11px;color:#444;">Tasks completed by the network</span>
      </div>
      <div id="gallery-grid" class="gallery-grid">
        <div class="empty-state" style="grid-column:1/-1;"><p>No completed tasks yet.</p></div>
      </div>
    </div>

    <!-- Output viewer modal -->
    <div id="output-modal" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.85);z-index:100;overflow-y:auto;">
      <div style="max-width:800px;margin:40px auto;padding:28px;background:#0D0F14;border:1px solid rgba(255,255,255,0.1);border-radius:12px;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;">
          <h2 id="modal-title" style="font-size:18px;font-weight:700;color:#F0F0F0;margin:0;"></h2>
          <div style="display:flex;gap:8px;align-items:center;">
            <button id="modal-download-btn" onclick="downloadOutput()" style="background:rgba(0,255,170,0.08);border:1px solid rgba(0,255,170,0.2);border-radius:6px;padding:6px 14px;color:#00FFAA;cursor:pointer;font-size:11px;font-weight:600;">Download</button>
            <button onclick="closeModal()" style="background:none;border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:6px 14px;color:#888;cursor:pointer;font-size:12px;">Close</button>
          </div>
        </div>
        <div id="modal-plan" style="margin-bottom:20px;"></div>
        <div id="modal-files" style="margin-bottom:16px;display:none;"></div>
        <div id="modal-review" style="max-height:520px;overflow-y:auto;"></div>
      </div>
    </div>
  </div>
</div>

<script>
// Poll health + nodes + metrics every 3 seconds
async function refresh() {
  try {
    const [health, nodes, met] = await Promise.all([
      fetch('/health').then(r => r.json()),
      fetch('/nodes').then(r => r.json()),
      fetch('/metrics').then(r => r.json()).catch(() => null),
    ]);

    document.getElementById('stat-nodes').textContent = nodes.count;
    document.getElementById('stat-tasks').textContent = health.tasks_pending;
    document.getElementById('stat-models').textContent = health.models.length;
    document.getElementById('stat-status').textContent = health.ollama;
    if (met) {
      const latEl = document.getElementById('stat-latency');
      if (latEl) latEl.textContent = met.avg_task_latency_seconds != null ? met.avg_task_latency_seconds + 's' : '-';
      const doneEl = document.getElementById('stat-done');
      if (doneEl) doneEl.textContent = met.tasks_completed_total;
    }

    const nodesList = document.getElementById('nodes-list');
    if (nodes.count === 0) {
      nodesList.innerHTML = `
        <div class="empty-state">
          <div class="icon">-</div>
          <p>No nodes connected yet.<br>Run <code style="color:#00FFAA">py join.py http://YOUR_IP:8000</code> on another machine to join.</p>
        </div>`;
    } else {
      nodesList.innerHTML = nodes.nodes.map(n => {
        const busy = n.current_task;
        const dotClass = busy ? 'node-dot busy' : 'node-dot';
        const activeHtml = busy
          ? `<div class="node-active-task">&#9654; ${escHtml(n.current_task)}</div>`
          : '';
        const hwParts = [];
        if (n.cpu_count) hwParts.push(`${n.cpu_count} CPU`);
        if (n.ram_gb)    hwParts.push(`${n.ram_gb}GB RAM`);
        if (n.gpu)       hwParts.push(escHtml(n.gpu));
        const hwHtml = hwParts.length
          ? `<div class="node-meta" style="color:#444;">${hwParts.join(' &middot; ')}</div>`
          : '';
        return `
          <div class="node-card active" id="nodecard-${escHtml(n.node_id)}" style="position:relative;">
            <div class="node-name"><span class="${dotClass}"></span>${escHtml(n.node_id)}</div>
            <div class="node-meta">${escHtml(n.platform)} / ${escHtml(n.machine)}</div>
            <div class="node-meta">${escHtml(n.model)}</div>
            ${hwHtml}
            <div class="node-tasks">${n.tasks_completed} tasks &middot; ${n.credits_earned || 0} credits</div>
            ${activeHtml}
          </div>`;
      }).join('');
    }
  } catch(e) {
    document.getElementById('stat-status').textContent = 'offline';
  }
}

// Stage bar helpers
function stageBarHtml(planDone, buildDone, reviewDone, buildLabel) {
  const planClass   = planDone   ? 'done' : (!planDone && !buildDone && !reviewDone ? 'active' : '');
  const buildClass  = planDone && !buildDone ? 'active' : (buildDone ? 'done' : '');
  const reviewClass = buildDone && !reviewDone ? 'active' : (reviewDone ? 'done' : '');
  const planLbl   = planClass   === 'active' ? 'active' : (planDone   ? 'done' : '');
  const buildLbl  = buildClass  === 'active' ? 'active' : (buildDone  ? 'done' : '');
  const reviewLbl = reviewClass === 'active' ? 'active' : (reviewDone ? 'done' : '');
  return `
    <div class="stage-bar">
      <div class="stage ${planClass}"></div>
      <div class="stage ${buildClass}"></div>
      <div class="stage ${reviewClass}"></div>
    </div>
    <div class="stage-labels">
      <div class="stage-label ${planLbl}">Plan</div>
      <div class="stage-label ${buildLbl}">${buildLabel || 'Build'}</div>
      <div class="stage-label ${reviewLbl}">Review</div>
    </div>`;
}

async function pitchTask() {
  const input = document.getElementById('pitch-input');
  const btn = document.getElementById('pitch-btn');
  const task = input.value.trim();
  if (!task) return;
  const activeProjectId = document.getElementById('active-project-id').value || null;

  btn.disabled = true;
  btn.textContent = 'Pitching...';

  const pipelineLog = document.getElementById('pipeline-log');
  const pipelineId = Date.now();
  const startCursor = eventCursor;

  // Show running card with stage bar in PLAN phase
  const runningCard = `
    <div class="pipeline-card running" id="pipeline-${pipelineId}">
      <div class="pipeline-header">
        <div class="pipeline-task">${escHtml(task)}</div>
        <div class="pipeline-status status-running" id="pstatus-${pipelineId}">PLANNING</div>
      </div>
      <div id="pstages-${pipelineId}">${stageBarHtml(false, false, false)}</div>
      <pre id="token-stream-${pipelineId}"
           style="margin-top:8px;padding:8px 10px;background:rgba(0,255,136,0.03);
                  border:1px solid rgba(0,255,136,0.1);border-radius:4px;
                  font-size:11px;color:#888;max-height:120px;overflow-y:auto;
                  white-space:pre-wrap;word-break:break-word;display:none;"></pre>
    </div>`;

  // Remove empty state if present
  const empty = pipelineLog.querySelector('.empty-state');
  if (empty) pipelineLog.innerHTML = '';
  pipelineLog.insertAdjacentHTML('afterbegin', runningCard);

  // Watch events to update the stage bar while the request is in flight.
  // Uses its own cursor so it doesn't conflict with the WebSocket log.
  let planDone = false, buildDone = false, buildCount = 0, totalSubtasks = 0;
  let stageCursor = eventCursor;
  const stageWatcher = setInterval(async () => {
    try {
      const r = await fetch(`/events?since=${stageCursor}`);
      const d = await r.json();
      d.events.forEach(ev => {
        if (ev.type === 'plan') {
          planDone = true;
          totalSubtasks = ev.subtasks ? ev.subtasks.length : 0;
        }
        if (ev.type === 'build') buildCount++;
        if (ev.type === 'review_start') buildDone = true;
        if (ev.id && ev.id > stageCursor) stageCursor = ev.id;
      });

      const statusEl = document.getElementById(`pstatus-${pipelineId}`);
      const stagesEl = document.getElementById(`pstages-${pipelineId}`);
      if (!statusEl) return;

      const buildLabel = totalSubtasks > 0
        ? `Build ${Math.min(buildCount, totalSubtasks)}/${totalSubtasks}`
        : 'Build';

      if (!planDone) {
        statusEl.textContent = 'PLANNING';
        stagesEl.innerHTML = stageBarHtml(false, false, false, buildLabel);
      } else if (!buildDone) {
        statusEl.textContent = buildLabel.toUpperCase();
        stagesEl.innerHTML = stageBarHtml(true, false, false, buildLabel);
      } else {
        statusEl.textContent = 'REVIEWING';
        stagesEl.innerHTML = stageBarHtml(true, true, false, buildLabel);
      }
    } catch(e) {}
  }, 1000);

  try {
    // Use async endpoint — returns job_id immediately, result arrives via WebSocket
    const endpoint = document.getElementById('stat-nodes').textContent !== '0'
      ? '/pitch/distributed' : '/pitch/async';
    const body = {task};
    if (activeProjectId) body.project_id = activeProjectId;
    const resp = await fetch(endpoint, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data = await resp.json();

    // Re-key the token stream element so _appendToken can find it by server job_id
    if (data.job_id) {
      const tsEl = document.getElementById(`token-stream-${pipelineId}`);
      if (tsEl) tsEl.id = `token-stream-${data.job_id}`;
    }

    // Distributed pitch returns full result synchronously (nodes do the work)
    // Async pitch returns {job_id, status:"queued"} and we poll for completion
    if (data.job_id && !data.plan) {
      // Async job — poll until complete
      btn.textContent = 'Running...';
      await _pollJobCompletion(data.job_id, pipelineId, task, stageWatcher);
    } else {
      // Distributed result came back synchronously
      clearInterval(stageWatcher);
      _showCompletedCard(pipelineId, task, data);
    }
  } catch(e) {
    clearInterval(stageWatcher);
    const card = document.getElementById(`pipeline-${pipelineId}`);
    if (card) {
      card.classList.remove('running');
      const s = document.getElementById(`pstatus-${pipelineId}`);
      if (s) { s.className = 'pipeline-status status-pending'; s.textContent = 'FAILED'; }
    }
  }

  btn.disabled = false;
  btn.textContent = 'Pitch';
  input.value = '';
}

async function _pollJobCompletion(jobId, pipelineId, task, stageWatcher) {
  // Poll /jobs/{id} until done; WebSocket keeps the stage bar + event log live
  while (true) {
    await new Promise(r => setTimeout(r, 3000));
    try {
      const r = await fetch(`/jobs/${jobId}`);
      const job = await r.json();
      if (job.status === 'complete' || job.status === 'failed') {
        clearInterval(stageWatcher);
        _showCompletedCard(pipelineId, task, {
          plan: job.plan || [],
          project_dir: job.project_dir || '',
          rating: job.rating,
          status: job.status,
        });
        loadHistory();
        loadProjects();
        return;
      }
    } catch(_) {}
  }
}

function _showCompletedCard(pipelineId, task, result) {
  const card = document.getElementById(`pipeline-${pipelineId}`);
  if (!card) return;
  card.classList.remove('running');

  const failed = result.status === 'failed';
  const statusClass = failed ? 'status-pending' : 'status-complete';
  const statusText = failed ? 'FAILED' : (result.mode === 'distributed' ? 'DISTRIBUTED' : 'COMPLETE');

  let subtasksHtml = '<div class="subtask-list">';
  (result.plan || []).forEach(st => {
    subtasksHtml += `
      <div class="subtask">
        <span class="subtask-id">${escHtml(String(st.id))}</span>
        <span class="subtask-title">${escHtml(st.title)}</span>
        <span class="subtask-check" style="color:#00FF88;">&#10003;</span>
      </div>`;
  });
  subtasksHtml += '</div>';

  card.innerHTML = `
    <div class="pipeline-header">
      <div class="pipeline-task">${escHtml(task)}</div>
      <div class="pipeline-status ${statusClass}">${statusText}</div>
    </div>
    ${stageBarHtml(true, true, true)}
    ${subtasksHtml}
    ${result.project_dir ? `<div class="log-entry" style="margin-top:8px;">
      <span class="log-time">${new Date().toLocaleTimeString()}</span>
      <span class="log-event">&#8594; ${escHtml(result.project_dir)}</span>
    </div>` : ''}
  `;
}

function escHtml(str) {
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function relativeTime(ts) {
  // ts format: 20240101_120000
  try {
    const s = ts.replace('_', 'T') + 'Z';
    const d = new Date(
      s.slice(0,4)+'-'+s.slice(4,6)+'-'+s.slice(6,8)+'T'+
      s.slice(9,11)+':'+s.slice(11,13)+':'+s.slice(13,15)+'Z'
    );
    const delta = Math.floor((Date.now() - d.getTime()) / 1000);
    if (delta < 60)    return 'just now';
    if (delta < 3600)  return `${Math.floor(delta/60)}m ago`;
    if (delta < 86400) return `${Math.floor(delta/3600)}h ago`;
    return `${Math.floor(delta/86400)}d ago`;
  } catch(_) { return ts; }
}

// Enter key to pitch
document.getElementById('pitch-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') pitchTask();
});

// ── Output viewer modal ──
function renderOutput(text) {
  if (!text) return '<div class="prose-text">No review output.</div>';

  const parts = [];
  const codeRe = /```(\\w*)\\n([\\s\\S]*?)```/g;
  let last = 0, m;

  while ((m = codeRe.exec(text)) !== null) {
    if (m.index > last) {
      parts.push(`<div class="prose-text">${escHtml(text.slice(last, m.index))}</div>`);
    }
    const lang = m[1] || 'text';
    const code = escHtml(m[2]);
    parts.push(`
      <div class="code-block">
        <div class="code-block-header">
          <span>${lang}</span>
          <button class="code-block-copy" onclick="copyCode(this)">copy</button>
        </div>
        <pre>${code}</pre>
      </div>`);
    last = m.index + m[0].length;
  }

  if (last < text.length) {
    parts.push(`<div class="prose-text">${escHtml(text.slice(last))}</div>`);
  }

  return parts.join('') || `<div class="prose-text">${escHtml(text)}</div>`;
}

function copyCode(btn) {
  const code = btn.closest('.code-block').querySelector('pre').textContent;
  navigator.clipboard.writeText(code).then(() => {
    btn.textContent = 'copied!';
    setTimeout(() => btn.textContent = 'copy', 1500);
  });
}

async function viewRun(timestamp) {
  try {
    _currentModalTimestamp = timestamp;
    const resp = await fetch(`/history/${timestamp}`);
    const data = await resp.json();

    document.getElementById('modal-title').textContent = data.task;

    let planHtml = '<div style="margin-bottom:16px;">';
    data.plan.forEach(st => {
      planHtml += `<div style="display:flex;gap:8px;padding:5px 0;border-bottom:1px solid rgba(255,255,255,0.04);">
        <span style="font-family:Consolas,monospace;font-size:11px;color:#555;min-width:20px;">${escHtml(String(st.id))}</span>
        <span style="font-size:13px;color:#00FFAA;font-weight:600;">${escHtml(st.title)}</span>
      </div>`;
    });
    planHtml += '</div>';
    document.getElementById('modal-plan').innerHTML = planHtml;

    // Rating badge in title area
    const ratingColors = {PASS: '#00FF88', NEEDS_WORK: '#E8FF47', FAIL: '#FF5555'};
    const ratingEl = document.getElementById('modal-title');
    const ratingBadge = data.rating && data.rating !== '?'
      ? ` <span style="font-size:11px;font-family:Consolas,monospace;color:${ratingColors[data.rating]||'#888'};font-weight:600;">${escHtml(data.rating)}</span>`
      : '';
    ratingEl.innerHTML = escHtml(data.task) + ratingBadge;

    // Code files section
    const filesEl = document.getElementById('modal-files');
    if (data.code_files && data.code_files.length > 0) {
      filesEl.style.display = 'block';
      filesEl.innerHTML = `
        <div style="font-size:10px;color:#666;letter-spacing:2px;text-transform:uppercase;margin-bottom:8px;font-family:Consolas,monospace;">Extracted Files</div>
        <div style="display:flex;flex-wrap:wrap;gap:6px;">
          ${data.code_files.map(f => `
            <span style="background:rgba(0,255,170,0.08);border:1px solid rgba(0,255,170,0.2);border-radius:4px;padding:3px 10px;font-family:Consolas,monospace;font-size:11px;color:#00FFAA;">${escHtml(f)}</span>
          `).join('')}
        </div>`;
    } else {
      filesEl.style.display = 'none';
    }

    // Prefer the clean final output over the full review blob
    const outputContent = (data.final_output && data.final_output.trim())
      ? data.final_output
      : data.review;
    document.getElementById('modal-review').innerHTML = renderOutput(outputContent);
    document.getElementById('output-modal').style.display = 'block';
  } catch(e) {
    console.error('Failed to load run:', e);
  }
}

function closeModal() {
  document.getElementById('output-modal').style.display = 'none';
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeModal();
});

// ── Event log — WebSocket with polling fallback ──
// eventCursor tracks the last SQLite rowid seen, not a count.
// Events from /events now carry an "id" field (rowid). We set cursor = max(id seen).
let eventCursor = 0;
let wsConnected = false;

const EVENT_LABELS = {
  pitch:        {agent: 'PITCH',    color: '#E8FF47'},
  plan:         {agent: 'PLANNER',  color: '#E8FF47'},
  build:        {agent: 'BUILDER',  color: '#00FFAA'},
  review_start: {agent: 'REVIEWER', color: '#AA77FF'},
  complete:     {agent: 'DONE',     color: '#00FF88'},
  error:        {agent: 'ERROR',    color: '#FF5555'},
};

function _appendToken(ev) {
  const el = document.getElementById(`token-stream-${ev.job_id}`);
  if (!el) return;
  if (el.style.display === 'none') el.style.display = 'block';
  el.textContent += ev.token;
  el.scrollTop = el.scrollHeight;
}

function appendEvent(ev) {
  // Token events — route to the live stream display, never to the log
  if (ev.type === 'token') {
    _appendToken(ev);
    return;
  }
  // Handle node activity events — update node cards directly, skip log entry
  if (ev.type === 'node_busy') {
    _setNodeBusy(ev.node_id, ev.task_title);
    return;
  }
  if (ev.type === 'node_idle') {
    _setNodeIdle(ev.node_id, ev.credits_earned);
    if (ev.credits_earned > 0) loadStandings();
    return;
  }
  if (ev.type === 'node_blacklisted') {
    _setNodeBlacklisted(ev.node_id, ev.blacklist_seconds);
    return;
  }

  const label = EVENT_LABELS[ev.type] || {agent: ev.type.toUpperCase(), color: '#888'};
  const t = new Date(ev.time).toLocaleTimeString();
  let msg = '';
  if (ev.type === 'pitch') msg = `Task pitched: "${escHtml(ev.task)}"`;
  else if (ev.type === 'plan') msg = `Decomposed into ${ev.subtasks.length} subtasks: ${ev.subtasks.map(escHtml).join(', ')}`;
  else if (ev.type === 'build') msg = `Subtask ${ev.subtask_id} complete: ${escHtml(ev.subtask)}`;
  else if (ev.type === 'review_start') msg = 'Reviewing combined output...';
  else if (ev.type === 'complete') msg = `Pipeline complete \u2192 ${escHtml(ev.project_dir)}`;
  else if (ev.type === 'error') msg = `Error: ${escHtml(ev.message || '')}`;
  else return; // skip unknown events

  const log = document.getElementById('event-log');
  log.insertAdjacentHTML('beforeend', `<div class="log-entry">
    <span class="log-time">${t}</span>
    <span class="log-agent" style="color:${label.color}">${label.agent}</span>
    <span class="log-event"> ${msg}</span>
  </div>`);
  log.scrollTop = log.scrollHeight;
}

function _setNodeBusy(nodeId, taskTitle) {
  const card = document.getElementById(`nodecard-${nodeId}`);
  if (!card) return;
  const dot = card.querySelector('.node-dot');
  if (dot) { dot.className = 'node-dot busy'; }
  let active = card.querySelector('.node-active-task');
  if (!active) {
    active = document.createElement('div');
    active.className = 'node-active-task';
    card.appendChild(active);
  }
  active.textContent = '\u25b6 ' + taskTitle;
}

function _setNodeIdle(nodeId, creditsEarned) {
  const card = document.getElementById(`nodecard-${nodeId}`);
  if (!card) return;
  const dot = card.querySelector('.node-dot');
  if (dot) { dot.className = 'node-dot'; }
  const active = card.querySelector('.node-active-task');
  if (active) active.remove();

  // Flash credit earned
  if (creditsEarned > 0) {
    const flash = document.createElement('div');
    flash.className = 'credit-flash';
    flash.textContent = `+${creditsEarned}`;
    card.appendChild(flash);
    setTimeout(() => flash.remove(), 2000);
  }
}

function _setNodeBlacklisted(nodeId, seconds) {
  const card = document.getElementById(`nodecard-${nodeId}`);
  if (!card) return;
  const dot = card.querySelector('.node-dot');
  if (dot) { dot.className = 'node-dot'; dot.style.background = '#FF5555'; dot.style.boxShadow = '0 0 8px rgba(255,85,85,0.4)'; }
  // Show or update circuit breaker badge
  let badge = card.querySelector('.node-breaker-badge');
  if (!badge) {
    badge = document.createElement('div');
    badge.className = 'node-breaker-badge';
    card.appendChild(badge);
  }
  badge.textContent = `CIRCUIT OPEN — ${seconds}s`;
  // Auto-remove badge after the blacklist expires
  setTimeout(() => {
    badge.remove();
    if (dot) { dot.style.background = ''; dot.style.boxShadow = ''; }
  }, seconds * 1000);
}

function connectWebSocket() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws/events`);

  ws.onopen = () => {
    wsConnected = true;
  };

  ws.onmessage = (e) => {
    try {
      const ev = JSON.parse(e.data);
      appendEvent(ev);
      // Track highest rowid seen so polling fallback has a correct cursor
      if (ev.id && ev.id > eventCursor) eventCursor = ev.id;
    } catch(_) {}
  };

  ws.onclose = () => {
    wsConnected = false;
    // Reconnect after 3s
    setTimeout(connectWebSocket, 3000);
  };

  ws.onerror = () => {
    ws.close();
  };
}

// Polling fallback — only used if WebSocket never connects
async function pollEvents() {
  if (wsConnected) return;
  try {
    const resp = await fetch(`/events?since=${eventCursor}`);
    const data = await resp.json();
    data.events.forEach(ev => {
      appendEvent(ev);
      if (ev.id && ev.id > eventCursor) eventCursor = ev.id;
    });
  } catch(e) {}
}

connectWebSocket();

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

    const ratingColor = {PASS: '#00FF88', NEEDS_WORK: '#E8FF47', FAIL: '#FF5555'};
    el.innerHTML = data.runs.map(r => {
      const color = ratingColor[r.rating] || '#555';
      return `
        <div class="pipeline-card" style="padding:12px 16px;margin-bottom:6px;cursor:pointer;" onclick="viewRun('${escHtml(r.timestamp)}')">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <div style="font-size:13px;color:#BBBBBB;flex:1;padding-right:12px;">${escHtml(r.task)}</div>
            <div style="display:flex;gap:10px;align-items:center;flex-shrink:0;">
              <span style="font-family:Consolas,monospace;font-size:10px;color:${color};">${escHtml(r.rating || '?')}</span>
              <span style="font-family:Consolas,monospace;font-size:11px;color:#555;">${r.subtask_count} tasks</span>
              <span style="font-family:Consolas,monospace;font-size:11px;color:#444;">${relativeTime(r.timestamp)}</span>
              <span style="font-size:11px;color:#00FFAA;">view</span>
            </div>
          </div>
        </div>`;
    }).join('');
  } catch(e) {}
}

// ── Standings ──
async function loadStandings() {
  try {
    const resp = await fetch('/standings');
    const data = await resp.json();
    const el = document.getElementById('standings-list');

    if (!data.standings.length) {
      el.innerHTML = '<div class="empty-state"><p>No contributions yet.</p></div>';
      return;
    }

    el.innerHTML = data.standings.map((s, i) => `
      <div class="node-card" style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
        <div>
          <div class="node-name" style="font-size:12px;">
            <span style="color:${i===0?'#E8FF47':i===1?'#00FFAA':'#888'};margin-right:6px;">#${i+1}</span>
            ${s.contributor}
          </div>
          <div class="node-meta">${s.compute_tasks} tasks / ${s.pitches} pitches</div>
        </div>
        <div style="font-family:Consolas,monospace;font-size:16px;font-weight:700;color:#E8FF47;">
          ${s.total_credits.toFixed(0)}
        </div>
      </div>
    `).join('');
  } catch(e) {}
}

// ── Projects ──
async function loadProjects() {
  try {
    const resp = await fetch('/projects');
    const data = await resp.json();
    const el = document.getElementById('projects-list');

    if (!data.projects || data.projects.length === 0) {
      el.innerHTML = '<div class="empty-state"><p>No projects yet.</p></div>';
      return;
    }

    el.innerHTML = data.projects.map(p => `
      <div class="node-card" style="margin-bottom:6px;cursor:pointer;padding:10px 12px;">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;">
          <div style="flex:1;min-width:0;">
            <div class="node-name" style="font-size:12px;margin-bottom:2px;">${escHtml(p.name)}</div>
            <div class="node-meta" style="font-size:10px;">${escHtml(p.project_id)}</div>
            <div class="node-meta" style="margin-top:3px;">${p.iteration_count} iteration${p.iteration_count !== 1 ? 's' : ''}</div>
          </div>
          <button onclick="continueProject('${escHtml(p.project_id)}','${escHtml(p.name)}')"
            style="background:rgba(0,255,170,0.1);border:1px solid rgba(0,255,170,0.25);border-radius:5px;padding:4px 10px;color:#00FFAA;font-size:10px;font-weight:700;cursor:pointer;white-space:nowrap;margin-left:8px;flex-shrink:0;">
            Continue
          </button>
        </div>
      </div>
    `).join('');
  } catch(e) {}
}

async function promptNewProject() {
  const name = prompt('Project name:');
  if (!name || !name.trim()) return;
  try {
    const resp = await fetch('/projects', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name.trim(), initial_task: ''}),
    });
    const data = await resp.json();
    if (data.project_id) {
      await loadProjects();
      continueProject(data.project_id, data.name);
    }
  } catch(e) { console.error('Failed to create project', e); }
}

function continueProject(projectId, projectName) {
  document.getElementById('active-project-id').value = projectId;
  document.getElementById('project-context-name').textContent = projectName;
  document.getElementById('project-context').style.display = 'flex';
  document.getElementById('pitch-input').placeholder = `What's next for ${projectName}?`;
  document.getElementById('pitch-input').focus();
}

function clearProjectContext() {
  document.getElementById('active-project-id').value = '';
  document.getElementById('project-context').style.display = 'none';
  document.getElementById('pitch-input').placeholder = 'Describe what you want built...';
}

// ── Tab switching ──
function showTab(name) {
  document.getElementById('view-live').style.display    = name === 'live'    ? '' : 'none';
  document.getElementById('view-gallery').style.display = name === 'gallery' ? '' : 'none';
  document.getElementById('tab-live').classList.toggle('active',    name === 'live');
  document.getElementById('tab-gallery').classList.toggle('active', name === 'gallery');
  if (name === 'gallery') loadGallery();
}

// ── Gallery ──
async function loadGallery() {
  try {
    const resp = await fetch('/gallery');
    const data = await resp.json();
    const el = document.getElementById('gallery-grid');

    if (!data.cards || data.cards.length === 0) {
      el.innerHTML = '<div class="empty-state" style="grid-column:1/-1;"><p>No completed tasks yet.<br>Pitch one from the Live tab to populate the gallery.</p></div>';
      return;
    }

    const ratingColor = {PASS: '#00FF88', NEEDS_WORK: '#E8FF47', FAIL: '#FF5555'};

    el.innerHTML = data.cards.map(c => {
      const color = ratingColor[c.rating] || '#555';
      const ratingHtml = c.rating && c.rating !== '?'
        ? `<span style="font-family:Consolas,monospace;font-size:10px;font-weight:700;color:${color};background:${color}18;border:1px solid ${color}30;border-radius:4px;padding:2px 8px;">${escHtml(c.rating)}</span>`
        : '';
      const subtasksHtml = `<span style="font-family:Consolas,monospace;font-size:10px;color:#555;">${c.subtask_count} tasks</span>`;
      const timeHtml = `<span style="font-family:Consolas,monospace;font-size:10px;color:#444;">${relativeTime(c.timestamp)}</span>`;
      const previewHtml = c.preview
        ? `<div class="gallery-preview">${escHtml(c.preview)}</div>`
        : '';
      const filesHtml = c.code_files && c.code_files.length > 0
        ? `<div class="gallery-files">${c.code_files.map(f => `<span class="gallery-file-chip">${escHtml(f)}</span>`).join('')}</div>`
        : '';
      return `
        <div class="gallery-card">
          <div class="gallery-task">${escHtml(c.task)}</div>
          <div class="gallery-meta">${ratingHtml}${subtasksHtml}${timeHtml}</div>
          ${previewHtml}
          ${filesHtml}
          <div class="gallery-actions">
            <button class="gallery-btn" onclick="viewRun('${escHtml(c.timestamp)}')">View Output</button>
            <button class="gallery-btn fork" onclick="forkTask('${escHtml(c.task.replace(/'/g, "\\'"))}')">Fork this</button>
          </div>
        </div>`;
    }).join('');
  } catch(e) {}
}

function forkTask(task) {
  showTab('live');
  const input = document.getElementById('pitch-input');
  input.value = task;
  input.focus();
  input.select();
}

// ── Task templates ──
const _TEMPLATES = [
  'Build a REST API with FastAPI and SQLite',
  'Write a Python web scraper with BeautifulSoup',
  'Create a CLI tool in Python with argparse',
  'Write a data analysis script for a CSV file',
  'Build a React component library starter',
];
(function renderTemplates() {
  const el = document.getElementById('task-templates');
  if (!el) return;
  const label = document.createElement('span');
  label.textContent = 'Try:';
  label.style.cssText = 'font-size:10px;color:#444;letter-spacing:1px;text-transform:uppercase;align-self:center;margin-right:4px;white-space:nowrap;';
  el.appendChild(label);
  _TEMPLATES.forEach(t => {
    const btn = document.createElement('button');
    btn.textContent = t;
    btn.style.cssText = 'background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:5px;padding:4px 10px;font-size:11px;color:#666;cursor:pointer;transition:all 0.15s;';
    btn.onmouseover = () => { btn.style.borderColor='rgba(0,255,170,0.25)'; btn.style.color='#00FFAA'; };
    btn.onmouseout  = () => { btn.style.borderColor='rgba(255,255,255,0.08)'; btn.style.color='#666'; };
    btn.onclick = () => { document.getElementById('pitch-input').value = t; document.getElementById('pitch-input').focus(); };
    el.appendChild(btn);
  });
})();

// ── Output download ──
let _currentModalTimestamp = null;

function downloadOutput() {
  if (!_currentModalTimestamp) return;
  const content = document.getElementById('modal-review').innerText;
  const title = (document.getElementById('modal-title').textContent || 'output').replace(/[^a-z0-9]/gi, '_').toLowerCase();
  const blob = new Blob([content], {type: 'text/plain'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${title}_${_currentModalTimestamp}.txt`;
  a.click();
  URL.revokeObjectURL(a.href);
}

// Start
refresh();
loadHistory();
loadStandings();
loadProjects();
setInterval(refresh, 3000);
setInterval(pollEvents, 3000);   // fallback only — no-ops when WS is connected
setInterval(loadHistory, 15000);
setInterval(loadStandings, 10000);
setInterval(loadProjects, 20000);
</script>
</body>
</html>"""


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML
