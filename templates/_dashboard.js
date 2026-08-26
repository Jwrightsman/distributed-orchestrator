/* Dashboard behaviour — injected into templates/dashboard.html by dashboard.py
   at that page's DASHBOARD_JS marker.

   Split out of dashboard.html for the same reason as the CSS: one 80 KB file
   holding markup, styles and a thousand lines of script is not navigable.
   No build step — the server pastes this in.

   Two rules this file keeps:
   1. No inline onclick in the markup. Everything is bound here, so every
      control is reachable by keyboard and nothing depends on a global name
      surviving a rename.
   2. No colours or layout in generated HTML. Generated nodes get classes;
      the classes live in _dashboard.css. The old code built badge colours by
      concatenating a token with an alpha suffix — `var(--accent)18` — which
      is not a colour, so those backgrounds silently did nothing. */

'use strict';

const $ = (id) => document.getElementById(id);

function escHtml(str) {
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/* Rating → class. One place, so a new rating never leaks a raw colour. */
const RATING_CLASS = {PASS: 'is-pass', NEEDS_WORK: 'is-needs-work', FAIL: 'is-fail'};
function ratingClass(r) { return RATING_CLASS[r] || 'is-unknown'; }
function ratingBadge(r) {
  if (!r || r === '?') return '';
  return `<span class="badge ${ratingClass(r)}">${escHtml(r)}</span>`;
}
function distBadge(mode) {
  return mode === 'distributed' ? '<span class="badge is-dist">DIST</span>' : '';
}

function relativeTime(ts) {
  // ts format: 20240101_120000, UTC
  try {
    const s = String(ts);
    const d = new Date(
      s.slice(0, 4) + '-' + s.slice(4, 6) + '-' + s.slice(6, 8) + 'T' +
      s.slice(9, 11) + ':' + s.slice(11, 13) + ':' + s.slice(13, 15) + 'Z'
    );
    const delta = Math.floor((Date.now() - d.getTime()) / 1000);
    if (delta < 60) return 'just now';
    if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
    if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
    return `${Math.floor(delta / 86400)}d ago`;
  } catch (_) { return ts; }
}

// ── Theme ────────────────────────────────────────────────────────
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  const icon = $('theme-toggle-icon');
  const label = $('theme-toggle-label');
  // Label the destination, not the current state — "Light" on a dark page
  // reads as "click for light", which is what people expect from a toggle.
  if (icon) icon.textContent = theme === 'dark' ? '☀' : '☾';
  if (label) label.textContent = theme === 'dark' ? 'Light' : 'Dark';
}
function toggleTheme() {
  const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  try { localStorage.setItem('mycelium-theme', next); } catch (e) { /* private mode */ }
  applyTheme(next);
}
applyTheme(document.documentElement.getAttribute('data-theme') || 'dark');

// Follow the OS only while the user has not chosen for themselves.
try {
  window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', (e) => {
    try { if (localStorage.getItem('mycelium-theme')) return; } catch (_) {}
    applyTheme(e.matches ? 'light' : 'dark');
  });
} catch (e) { /* older browsers */ }

// ── Navigation shell ─────────────────────────────────────────────
const DRAWER_QUERY = '(max-width: 900px)';
const isDrawer = () => window.matchMedia(DRAWER_QUERY).matches;

let _navScrim = null;

function openDrawer() {
  const app = $('app');
  app.classList.add('nav-open');
  $('nav-toggle').setAttribute('aria-expanded', 'true');
  if (!_navScrim) {
    _navScrim = document.createElement('button');
    _navScrim.type = 'button';
    _navScrim.className = 'nav-scrim';
    _navScrim.setAttribute('aria-label', 'Close navigation');
    _navScrim.addEventListener('click', closeDrawer);
    document.body.appendChild(_navScrim);
  }
  // Focus moves into the drawer, so Tab does not walk the page behind it.
  const first = document.querySelector('#nav-items .nav-item');
  if (first) first.focus();
}

function closeDrawer() {
  const app = $('app');
  app.classList.remove('nav-open');
  $('nav-toggle').setAttribute('aria-expanded', 'false');
  if (_navScrim) { _navScrim.remove(); _navScrim = null; }
}

function toggleNav() {
  const app = $('app');
  // Below 900px the sidebar is an overlay, so the same control has to mean
  // "open" there and "collapse to icons" on a wide screen.
  if (isDrawer()) {
    if (app.classList.contains('nav-open')) closeDrawer(); else openDrawer();
    return;
  }
  app.classList.toggle('nav-collapsed');
  const collapsed = app.classList.contains('nav-collapsed');
  $('nav-toggle').setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  try { localStorage.setItem('mycelium-nav', collapsed ? '1' : '0'); } catch (e) {}
}

try {
  if (localStorage.getItem('mycelium-nav') === '1') {
    $('app').classList.add('nav-collapsed');
  }
} catch (e) {}

// ── Views ────────────────────────────────────────────────────────
const TABS = ['overview', 'runs', 'gallery', 'nodes', 'projects', 'guild'];
const TAB_TITLES = {
  overview: 'Overview', runs: 'Runs', gallery: 'Gallery',
  nodes: 'Network nodes', projects: 'Projects', guild: 'Guild standings',
};

function showTab(name, opts) {
  if (TABS.indexOf(name) === -1) name = 'overview';
  TABS.forEach(t => {
    const view = $('view-' + t);
    const btn = $('tab-' + t);
    if (view) view.hidden = (t !== name);
    if (btn) {
      // aria-current is how a screen reader announces "you are here".
      if (t === name) btn.setAttribute('aria-current', 'page');
      else btn.removeAttribute('aria-current');
    }
  });
  const title = $('view-title');
  if (title) title.textContent = TAB_TITLES[name] || name;
  // A deep link should survive a refresh; without this, "Runs" is unshareable.
  if (!opts || opts.pushHash !== false) {
    try { history.replaceState(null, '', '#' + name); } catch (e) {}
  }
  if (isDrawer()) closeDrawer();

  if (name === 'gallery') loadGallery();
  if (name === 'guild') loadStandings();
  if (name === 'runs') loadHistory();
}

function focusPitch() {
  showTab('overview');
  const el = $('pitch-input');
  if (el) { el.focus(); el.scrollIntoView({behavior: 'smooth', block: 'center'}); }
}

// ── Modals ───────────────────────────────────────────────────────
// Focus goes into the dialog and comes back to whatever opened it. Without
// that, closing a modal drops keyboard focus on <body> and the next Tab
// starts from the top of the page.
let _lastFocused = null;

function openModal(id) {
  _lastFocused = document.activeElement;
  const m = $(id);
  m.hidden = false;
  const focusable = m.querySelector('button, a[href], input, [tabindex]:not([tabindex="-1"])');
  if (focusable) focusable.focus();
}

function closeModal(id) {
  const m = $(id);
  if (!m || m.hidden) return;
  m.hidden = true;
  if (_lastFocused && document.contains(_lastFocused)) _lastFocused.focus();
  _lastFocused = null;
}

function anyModalOpen() {
  return ['output-modal', 'node-modal'].some(id => !$(id).hidden);
}

// ── Health / nodes / metrics polling ─────────────────────────────
async function refresh() {
  try {
    const [health, nodes, met] = await Promise.all([
      fetch('/health').then(r => r.json()),
      fetch('/nodes').then(r => r.json()),
      fetch('/metrics').then(r => r.json()).catch(() => null),
    ]);

    $('stat-nodes').textContent = nodes.count;
    $('stat-tasks').textContent = health.tasks_pending;
    $('stat-models').textContent = health.models.length;
    const ok = health.ollama === 'connected';
    // Status lives in the shell as well as on Overview: whichever view you are
    // on, "is the swarm alive" has to be answerable without navigating.
    [['stat-status', health.ollama], ['nav-status', ok ? 'connected' : health.ollama]]
      .forEach(([id, text]) => {
        const el = $(id);
        if (!el) return;
        el.className = 'stat-status ' + (ok ? 'is-ok' : 'is-down');
        el.innerHTML = '<i aria-hidden="true"></i>' + escHtml(text);
      });
    const navModel = $('nav-model');
    if (navModel) navModel.textContent = (health.models && health.models[0]) || 'no model';
    const navCount = $('nav-node-count');
    if (navCount) {
      // An empty pill reads as a rendering bug. Show the badge only once
      // there is a count to put in it.
      const n = (nodes.nodes || []).length;
      navCount.textContent = n;
      navCount.hidden = n === 0;
    }
    if (met) {
      const latEl = $('stat-latency');
      if (latEl) latEl.textContent = met.avg_task_latency_seconds != null ? met.avg_task_latency_seconds + 's' : '-';
      const doneEl = $('stat-done');
      if (doneEl) doneEl.textContent = met.tasks_completed_total;
      // Name whose balance this is — anyone can open this dashboard
      const hostEl = $('credit-host');
      if (hostEl && met.orchestrator_id) hostEl.textContent = met.orchestrator_id;
      const credEl = $('credit-value');
      if (credEl) credEl.textContent = met.orchestrator_credits ?? 0;
    }

    renderNodes(nodes);
  } catch (e) {
    ['stat-status', 'nav-status'].forEach(id => {
      const el = $(id);
      if (el) { el.className = 'stat-status is-down'; el.innerHTML = '<i aria-hidden="true"></i>offline'; }
    });
  }
}

function renderNodes(nodes) {
  const nodesList = $('nodes-list');
  if (!nodesList) return;
  if (nodes.count === 0) {
    nodesList.innerHTML = `
      <div class="empty-state">
        <div class="icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="5" r="2.2"/><circle cx="5" cy="18" r="2.2"/><circle cx="19" cy="18" r="2.2"/><path d="M10.5 6.8 6.5 15.8M13.5 6.8l4 9M7.4 18h9.2"/></svg></div>
        <p>No nodes connected yet.<br>Run <code class="code-inline">python join.py ${escHtml(location.origin)}</code> on another machine to join.</p>
      </div>`;
    return;
  }
  nodesList.innerHTML = nodes.nodes.map(n => {
    const busy = n.current_task;
    const dotClass = busy ? 'node-dot busy' : 'node-dot';
    const activeHtml = busy
      ? `<div class="node-active-task">&#9654; ${escHtml(n.current_task)}</div>`
      : '';
    const hwParts = [];
    if (n.cpu_count) hwParts.push(`${n.cpu_count} CPU`);
    if (n.ram_gb) hwParts.push(`${n.ram_gb}GB RAM`);
    if (n.gpu) hwParts.push(escHtml(n.gpu));
    const hwHtml = hwParts.length ? `<div class="node-meta">${hwParts.join(' &middot; ')}</div>` : '';
    // Filter the auto-added model: tag out of the visible chips — model is shown explicitly
    const visibleCaps = (n.capabilities || []).filter(c => !c.startsWith('model:'));
    const capsRow = visibleCaps.length
      ? `<div class="node-meta">${visibleCaps.map(c => `<span class="chip">${escHtml(c)}</span>`).join('')}</div>`
      : '';
    return `
      <button type="button" class="node-card active" id="nodecard-${escHtml(n.node_id)}"
              data-node="${escHtml(JSON.stringify(n))}">
        <span class="node-name"><span class="${dotClass}" aria-hidden="true"></span>${escHtml(n.node_id)}</span>
        <div class="node-meta">${escHtml(n.platform)} / ${escHtml(n.machine)}</div>
        <div class="node-meta">${escHtml(n.model)}</div>
        ${hwHtml}${capsRow}
        <div class="node-tasks">${n.tasks_completed} tasks &middot; ${n.credits_earned || 0} credits</div>
        ${activeHtml}
      </button>`;
  }).join('');
}

// ── Stage bar ────────────────────────────────────────────────────
function stageBarHtml(planDone, buildDone, reviewDone, buildLabel) {
  const planClass = planDone ? 'done' : (!planDone && !buildDone && !reviewDone ? 'active' : '');
  const buildClass = planDone && !buildDone ? 'active' : (buildDone ? 'done' : '');
  const reviewClass = buildDone && !reviewDone ? 'active' : (reviewDone ? 'done' : '');
  const planLbl = planClass === 'active' ? 'active' : (planDone ? 'done' : '');
  const buildLbl = buildClass === 'active' ? 'active' : (buildDone ? 'done' : '');
  const reviewLbl = reviewClass === 'active' ? 'active' : (reviewDone ? 'done' : '');
  return `
    <div class="stage-bar" aria-hidden="true">
      <div class="stage ${planClass}"></div>
      <div class="stage ${buildClass}"></div>
      <div class="stage ${reviewClass}"></div>
    </div>
    <div class="stage-labels">
      <div class="stage-label ${planLbl}">Plan</div>
      <div class="stage-label ${buildLbl}">${escHtml(buildLabel || 'Build')}</div>
      <div class="stage-label ${reviewLbl}">Review</div>
    </div>`;
}

// ── Pitching ─────────────────────────────────────────────────────
async function pitchTask() {
  const input = $('pitch-input');
  const btn = $('pitch-btn');
  const task = input.value.trim();
  if (!task) return;
  const activeProjectId = $('active-project-id').value || null;

  btn.disabled = true;
  btn.textContent = 'Pitching...';

  const pipelineLog = $('pipeline-log');
  const pipelineId = Date.now();
  const _cardStart = Date.now();

  const runningCard = `
    <div class="pipeline-card running" id="pipeline-${pipelineId}">
      <div class="pipeline-header">
        <div class="pipeline-task">${escHtml(task)}</div>
        <div class="pipeline-head-right">
          <span class="pipeline-elapsed" id="pelapsed-${pipelineId}">0s</span>
          <div class="pipeline-status status-running" id="pstatus-${pipelineId}">PLANNING</div>
        </div>
      </div>
      <div id="pstages-${pipelineId}">${stageBarHtml(false, false, false)}</div>
      <pre class="token-stream" id="token-stream-${pipelineId}" hidden></pre>
    </div>`;

  const elapsedTicker = setInterval(() => {
    const el = $(`pelapsed-${pipelineId}`);
    if (!el) { clearInterval(elapsedTicker); return; }
    const secs = Math.round((Date.now() - _cardStart) / 1000);
    el.textContent = secs >= 60 ? `${Math.floor(secs / 60)}m ${secs % 60}s` : `${secs}s`;
  }, 1000);

  const empty = pipelineLog.querySelector('.empty-state');
  if (empty) pipelineLog.innerHTML = '';
  pipelineLog.insertAdjacentHTML('afterbegin', runningCard);

  // Watch events to update the stage bar while the request is in flight.
  // Uses its own cursor so it doesn't conflict with the WebSocket log.
  let planDone = false, buildDone = false, buildCount = 0, totalSubtasks = 0;
  let stageCursor = eventCursor;
  const stageWatcher = setInterval(async () => {
    try {
      const d = await (await fetch(`/events?since=${stageCursor}`)).json();
      d.events.forEach(ev => {
        if (ev.type === 'plan') {
          planDone = true;
          totalSubtasks = Number.isInteger(ev.subtask_count)
            ? ev.subtask_count
            : (Array.isArray(ev.subtasks) ? ev.subtasks.length : 0);
        }
        if (ev.type === 'build') buildCount++;
        if (ev.type === 'review_start') buildDone = true;
        if (ev.id && ev.id > stageCursor) stageCursor = ev.id;
      });

      const statusEl = $(`pstatus-${pipelineId}`);
      const stagesEl = $(`pstages-${pipelineId}`);
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
    } catch (e) {}
  }, 1000);

  try {
    // Always use the async endpoint — returns job_id immediately, live updates
    // via WebSocket. /pitch/async falls back to /pitch/distributed internally
    // when nodes are connected.
    const body = {task};
    if (activeProjectId) body.project_id = activeProjectId;
    const resp = await fetch('/pitch/async', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data = await resp.json();

    // Re-key the token stream element so _appendToken can find it by job_id
    if (data.job_id) {
      const tsEl = $(`token-stream-${pipelineId}`);
      if (tsEl) tsEl.id = `token-stream-${data.job_id}`;
    }

    if (!data.job_id) throw new Error('Unexpected response: ' + JSON.stringify(data));
    btn.textContent = 'Running...';
    await _pollJobCompletion(data.job_id, pipelineId, task, stageWatcher, elapsedTicker);
  } catch (e) {
    clearInterval(stageWatcher);
    clearInterval(elapsedTicker);
    const card = $(`pipeline-${pipelineId}`);
    if (card) {
      card.classList.remove('running');
      const s = $(`pstatus-${pipelineId}`);
      if (s) { s.className = 'pipeline-status status-pending'; s.textContent = 'FAILED'; }
    }
  }

  btn.disabled = false;
  btn.textContent = 'Pitch';
  input.value = '';
}

async function _pollJobCompletion(jobId, pipelineId, task, stageWatcher, elapsedTicker) {
  while (true) {
    await new Promise(r => setTimeout(r, 3000));
    try {
      const job = await (await fetch(`/jobs/${jobId}`)).json();
      if (job.status === 'complete' || job.status === 'failed') {
        clearInterval(stageWatcher);
        clearInterval(elapsedTicker);
        _showCompletedCard(pipelineId, task, {
          plan: job.plan || [],
          project_dir: job.project_dir || '',
          rating: job.rating,
          status: job.status,
          mode: job.mode,
          error: job.error || null,
        });
        loadHistory();
        loadProjects();
        return;
      }
    } catch (_) {}
  }
}

function _showCompletedCard(pipelineId, task, result) {
  const card = $(`pipeline-${pipelineId}`);
  if (!card) return;
  card.classList.remove('running');

  const failed = result.status === 'failed';
  const statusClass = failed ? 'status-pending' : 'status-complete';
  const statusText = failed ? 'FAILED' : (result.mode === 'distributed' ? 'DISTRIBUTED' : 'COMPLETE');
  const badge = failed ? '' : ratingBadge(result.rating);

  let subtasksHtml = '<div class="subtask-list">';
  (result.plan || []).forEach(st => {
    subtasksHtml += `
      <div class="subtask">
        <span class="subtask-id">${escHtml(String(st.id))}</span>
        <span class="subtask-title">${escHtml(st.title)}</span>
        <span class="subtask-check" aria-hidden="true">&#10003;</span>
      </div>`;
  });
  subtasksHtml += '</div>';

  const ts = result.project_dir ? result.project_dir.split(/[\\/]/).pop() : '';

  card.innerHTML = `
    <div class="pipeline-header">
      <div class="pipeline-task">${escHtml(task)}</div>
      <div class="pipeline-head-right">${badge}<div class="pipeline-status ${statusClass}">${statusText}</div></div>
    </div>
    ${stageBarHtml(true, true, true)}
    ${subtasksHtml}
    ${ts ? `<a class="run-link" href="/run/${encodeURIComponent(ts)}">Open the run page &#8594;</a>` : ''}
    ${result.error ? `<div class="error-box">${escHtml(result.error)}</div>` : ''}
  `;
}

// ── Output viewer ────────────────────────────────────────────────
function renderOutput(text) {
  if (!text) return '<div class="prose-text">No review output.</div>';

  const parts = [];
  // This pattern used to be written with doubled backslashes, which made it
  // match a literal "\w" — so no fenced block ever matched and every review
  // rendered as one undifferentiated wall of prose, fences and all.
  const codeRe = /```(\w*)\n([\s\S]*?)```/g;
  let last = 0, m;

  while ((m = codeRe.exec(text)) !== null) {
    if (m.index > last) {
      parts.push(`<div class="prose-text">${escHtml(text.slice(last, m.index))}</div>`);
    }
    parts.push(`
      <div class="code-block">
        <div class="code-block-header">
          <span>${escHtml(m[1] || 'text')}</span>
          <button type="button" class="code-block-copy">copy</button>
        </div>
        <pre>${escHtml(m[2])}</pre>
      </div>`);
    last = m.index + m[0].length;
  }

  if (last < text.length) {
    parts.push(`<div class="prose-text">${escHtml(text.slice(last))}</div>`);
  }

  return parts.join('') || `<div class="prose-text">${escHtml(text)}</div>`;
}

let _currentModalTimestamp = null;

async function viewRun(timestamp) {
  try {
    _currentModalTimestamp = timestamp;
    const data = await (await fetch(`/history/${encodeURIComponent(timestamp)}`)).json();

    $('modal-title').innerHTML =
      escHtml(data.task) + ' ' + ratingBadge(data.rating) + distBadge(data.mode);

    $('modal-permalink').href = `/run/${encodeURIComponent(timestamp)}`;

    $('modal-plan').innerHTML = (data.plan || []).map(st => `
      <div class="plan-row">
        <span class="plan-id">${escHtml(String(st.id))}</span>
        <span class="plan-title">${escHtml(st.title)}</span>
      </div>`).join('');

    const filesEl = $('modal-files');
    if (data.code_files && data.code_files.length) {
      filesEl.hidden = false;
      filesEl.innerHTML =
        `<div class="files-label">Extracted files</div>
         <div class="file-chips">${data.code_files.map(f => `<span class="file-chip">${escHtml(f)}</span>`).join('')}</div>`;
    } else {
      filesEl.hidden = true;
    }

    // Prefer the clean final output over the full review blob
    const outputContent = (data.final_output && data.final_output.trim())
      ? data.final_output
      : data.review;
    $('modal-review').innerHTML = renderOutput(outputContent);
    openModal('output-modal');
  } catch (e) {
    console.error('Failed to load run:', e);
  }
}

// ── Node detail ──────────────────────────────────────────────────
function openNodeModal(n) {
  $('node-modal-title').textContent = n.node_id;
  $('node-modal-permalink').href = `/node/${encodeURIComponent(n.node_id)}`;

  const row = (label, val) => (val || val === 0)
    ? `<span class="kv-key">${escHtml(label)}</span><span class="kv-val">${escHtml(String(val))}</span>`
    : '';

  const rows = [
    row('Model', n.model),
    row('Platform', `${n.platform} / ${n.machine}`),
    row('Hostname', n.hostname),
    row('CPU', n.cpu_count ? `${n.cpu_count} cores` : null),
    row('RAM', n.ram_gb ? `${n.ram_gb} GB` : null),
    row('GPU', n.gpu),
    row('Joined', n.registered_at ? n.registered_at.slice(0, 19).replace('T', ' ') + ' UTC' : null),
    row('Tasks done', n.tasks_completed),
    row('Credits', n.credits_earned || 0),
  ];

  const visibleCaps = (n.capabilities || []).filter(c => !c.startsWith('model:'));
  if (visibleCaps.length) {
    rows.push(`<span class="kv-key">Capabilities</span><span class="kv-val">${
      visibleCaps.map(c => `<span class="chip">${escHtml(c)}</span>`).join('')}</span>`);
  }

  let html = `<div class="kv">${rows.filter(Boolean).join('')}</div>`;
  if (n.current_task) {
    html += `<div class="node-current">&#9654; ${escHtml(n.current_task)}</div>`;
  }
  $('node-modal-body').innerHTML = html;
  openModal('node-modal');
}

// ── Event log ────────────────────────────────────────────────────
// eventCursor tracks the last SQLite rowid seen, not a count.
let eventCursor = 0;
let wsConnected = false;

// One class per stage so the log is scannable at a glance. Muted on purpose:
// these repeat on every line, and saturated colour at that density is noise.
const EVENT_LABELS = {
  pitch:        {agent: 'PITCH',    cls: 'is-pitch'},
  plan:         {agent: 'PLANNER',  cls: 'is-plan'},
  build:        {agent: 'BUILDER',  cls: 'is-build'},
  review_start: {agent: 'REVIEWER', cls: 'is-review'},
  complete:     {agent: 'DONE',     cls: 'is-done'},
  error:        {agent: 'ERROR',    cls: 'is-error'},
};

function _appendToken(ev) {
  const el = $(`token-stream-${ev.job_id}`);
  if (!el) return;
  if (el.hidden) el.hidden = false;
  el.textContent += ev.token;
  el.scrollTop = el.scrollHeight;
}

function appendEvent(ev) {
  // Token events — route to the live stream display, never to the log
  if (ev.type === 'token') { _appendToken(ev); return; }
  if (ev.type === 'node_busy') {
    _setNodeBusy(ev.node_id, ev.unit_id || ev.task_id || ev.task_title || 'working');
    return;
  }
  if (ev.type === 'node_idle') {
    _setNodeIdle(ev.node_id, ev.credits_earned);
    if (ev.credits_earned > 0) loadStandings();
    return;
  }
  if (ev.type === 'node_blacklisted') { _setNodeBlacklisted(ev.node_id, ev.blacklist_seconds); return; }

  const label = EVENT_LABELS[ev.type];
  if (!label) return;  // skip unknown events
  const t = new Date(ev.time).toLocaleTimeString();
  let msg = '';
  if (ev.type === 'pitch') {
    msg = ev.task ? `Task pitched: "${escHtml(ev.task)}"` : 'Task accepted';
  } else if (ev.type === 'plan') {
    const legacySubtasks = Array.isArray(ev.subtasks) ? ev.subtasks : [];
    const count = Number.isInteger(ev.subtask_count) ? ev.subtask_count : legacySubtasks.length;
    msg = `Decomposed into ${count} subtasks`;
    if (legacySubtasks.length) msg += `: ${legacySubtasks.map(escHtml).join(', ')}`;
  } else if (ev.type === 'build') {
    msg = `Subtask ${ev.subtask_id} complete`;
    if (ev.subtask) msg += `: ${escHtml(ev.subtask)}`;
  } else if (ev.type === 'review_start') msg = 'Reviewing combined output...';
  else if (ev.type === 'complete') msg = `Pipeline complete → ${escHtml(ev.project_dir)}`;
  else if (ev.type === 'error') msg = `Error: ${escHtml(ev.message || '')}`;

  const log = $('event-log');
  log.insertAdjacentHTML('beforeend', `<div class="log-entry">
    <span class="log-time">${t}</span>
    <span class="log-agent ${label.cls}">${label.agent}</span>
    <span class="log-event"> ${msg}</span>
  </div>`);
  log.scrollTop = log.scrollHeight;
}

function _setNodeBusy(nodeId, taskTitle) {
  const card = $(`nodecard-${nodeId}`);
  if (!card) return;
  card.classList.remove('finished');
  card.classList.add('working');
  const dot = card.querySelector('.node-dot');
  if (dot) dot.className = 'node-dot busy';
  let active = card.querySelector('.node-active-task');
  if (!active) {
    active = document.createElement('div');
    active.className = 'node-active-task';
    card.appendChild(active);
  }
  active.textContent = '▶ ' + taskTitle;
}

function _setNodeIdle(nodeId, creditsEarned) {
  const card = $(`nodecard-${nodeId}`);
  if (!card) return;
  card.classList.remove('working');
  card.classList.add('finished');
  card.addEventListener('animationend', () => card.classList.remove('finished'), {once: true});
  const dot = card.querySelector('.node-dot');
  if (dot) dot.className = 'node-dot';
  const active = card.querySelector('.node-active-task');
  if (active) active.remove();

  if (creditsEarned > 0) {
    const flash = document.createElement('div');
    flash.className = 'credit-flash';
    flash.textContent = `+${creditsEarned}`;
    card.appendChild(flash);
    setTimeout(() => flash.remove(), 2000);
  }
}

function _setNodeBlacklisted(nodeId, seconds) {
  const card = $(`nodecard-${nodeId}`);
  if (!card) return;
  const dot = card.querySelector('.node-dot');
  if (dot) dot.className = 'node-dot down';
  let badge = card.querySelector('.node-breaker-badge');
  if (!badge) {
    badge = document.createElement('div');
    badge.className = 'node-breaker-badge';
    card.appendChild(badge);
  }
  badge.textContent = `CIRCUIT OPEN — ${seconds}s`;
  setTimeout(() => {
    badge.remove();
    if (dot) dot.className = 'node-dot';
  }, seconds * 1000);
}

function connectWebSocket() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws/events`);
  ws.onopen = () => { wsConnected = true; };
  ws.onmessage = (e) => {
    try {
      const ev = JSON.parse(e.data);
      appendEvent(ev);
      if (ev.id && ev.id > eventCursor) eventCursor = ev.id;
    } catch (_) {}
  };
  ws.onclose = () => { wsConnected = false; setTimeout(connectWebSocket, 3000); };
  ws.onerror = () => ws.close();
}

// Polling fallback — only used if WebSocket never connects
async function pollEvents() {
  if (wsConnected) return;
  try {
    const data = await (await fetch(`/events?since=${eventCursor}`)).json();
    data.events.forEach(ev => {
      appendEvent(ev);
      if (ev.id && ev.id > eventCursor) eventCursor = ev.id;
    });
  } catch (e) {}
}

// ── History ──────────────────────────────────────────────────────
let _historySearchTimer = null;

async function loadHistory() {
  const q = ($('history-search')?.value || '').trim();
  const url = q ? `/history?search=${encodeURIComponent(q)}` : '/history';
  try {
    const data = await (await fetch(url)).json();
    const el = $('history-list');

    if (data.count === 0) {
      el.innerHTML = q
        ? `<div class="empty-state"><p>Nothing matches “${escHtml(q)}”.</p></div>`
        : '<div class="empty-state"><p>No past runs yet.<br>A completed pitch lands here with a permanent link you can share.</p></div>';
      return;
    }

    el.innerHTML = data.runs.map(r => `
      <a class="history-card" href="/run/${encodeURIComponent(r.timestamp)}">
        <span class="history-task">${escHtml(r.task)}</span>
        <span class="history-meta">
          ${distBadge(r.mode)}
          ${ratingBadge(r.rating)}
          <span class="mono-dim">${r.subtask_count} tasks</span>
          <span class="mono-dim">${relativeTime(r.timestamp)}</span>
          <span class="history-view">view &#8594;</span>
        </span>
      </a>`).join('');
  } catch (e) {}
}

// ── Standings ────────────────────────────────────────────────────
async function loadStandings() {
  try {
    const data = await (await fetch('/standings')).json();
    const el = $('standings-list');

    if (!data.standings.length) {
      el.innerHTML = '<div class="empty-state"><p>No contributions yet.<br>Credits are recorded when a machine builds, reviews or pitches.</p></div>';
      return;
    }

    el.innerHTML = data.standings.map((s, i) => `
      <div class="node-card">
        <div class="standing">
          <div>
            <div class="node-name">
              <span class="standing-rank ${i === 0 ? 'is-first' : i === 1 ? 'is-second' : ''}">#${i + 1}</span>
              ${escHtml(s.contributor)}
            </div>
            <div class="node-meta">${s.compute_tasks} tasks / ${s.pitches} pitches</div>
          </div>
          <div class="standing-credits">${s.total_credits.toFixed(0)}</div>
        </div>
      </div>`).join('');
  } catch (e) {}
}

// ── Projects ─────────────────────────────────────────────────────
async function loadProjects() {
  try {
    const data = await (await fetch('/projects')).json();
    const el = $('projects-list');

    if (!data.projects || data.projects.length === 0) {
      el.innerHTML = '<div class="empty-state"><p>No projects yet.<br>A project carries memory from one pitch to the next.</p></div>';
      return;
    }

    el.innerHTML = data.projects.map(p => `
      <div class="node-card">
        <div class="project-row">
          <div class="project-main">
            <div class="node-name">${escHtml(p.name)}</div>
            <div class="node-meta">${escHtml(p.project_id)}</div>
            <div class="node-meta">${p.iteration_count} iteration${p.iteration_count !== 1 ? 's' : ''}</div>
          </div>
          <button type="button" class="btn-accent is-sm" data-continue-project="${escHtml(p.project_id)}"
                  data-project-name="${escHtml(p.name)}">Continue</button>
        </div>
      </div>`).join('');
  } catch (e) {}
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
  } catch (e) { console.error('Failed to create project', e); }
}

function continueProject(projectId, projectName) {
  $('active-project-id').value = projectId;
  $('project-context-name').textContent = projectName;
  $('project-context').hidden = false;
  showTab('overview');
  $('pitch-input').placeholder = `What's next for ${projectName}?`;
  $('pitch-input').focus();
}

function clearProjectContext() {
  $('active-project-id').value = '';
  $('project-context').hidden = true;
  $('pitch-input').placeholder = 'Describe what you want built...';
}

// ── Gallery ──────────────────────────────────────────────────────
async function loadGallery() {
  try {
    const data = await (await fetch('/gallery')).json();
    const el = $('gallery-grid');

    if (!data.cards || data.cards.length === 0) {
      el.innerHTML = '<div class="empty-state is-grid"><p>No completed tasks yet.<br>Pitch one from Overview and it will appear here with its own page.</p></div>';
      return;
    }

    el.innerHTML = data.cards.map(c => {
      const ts = encodeURIComponent(c.timestamp);
      const nodesHtml = c.nodes_used > 0
        ? `<span class="mono-info">${c.nodes_used} node${c.nodes_used > 1 ? 's' : ''}</span>`
        : '';
      return `
        <div class="gallery-card">
          <a class="gallery-task" href="/run/${ts}">${escHtml(c.task)}</a>
          <div class="gallery-meta">
            ${distBadge(c.mode)}${ratingBadge(c.rating)}
            <span class="mono-dim">${c.subtask_count} tasks</span>
            ${nodesHtml}
            <span class="mono-dim">${relativeTime(c.timestamp)}</span>
          </div>
          ${c.preview ? `<div class="gallery-preview">${escHtml(c.preview)}</div>` : ''}
          ${c.code_files && c.code_files.length
            ? `<div class="gallery-files">${c.code_files.map(f => `<span class="gallery-file-chip">${escHtml(f)}</span>`).join('')}</div>`
            : ''}
          <div class="gallery-actions">
            <a class="gallery-btn open" href="/run/${ts}">Open run</a>
            <button type="button" class="gallery-btn" data-fork="${escHtml(c.task)}"
                    data-project="${escHtml(c.project_id || '')}">Fork &amp; continue</button>
            <button type="button" class="gallery-btn share" data-share="${escHtml(c.timestamp)}"
                    title="Copy a link to this run">Share &#x2197;</button>
          </div>
        </div>`;
    }).join('');
  } catch (e) {}
}

function forkTask(task, projectId) {
  // If this run belongs to a project, continue it so the model loads the
  // memory context. Otherwise just load the task text for a fresh pitch.
  if (projectId) {
    continueProject(projectId, task);
  } else {
    showTab('overview');
    const input = $('pitch-input');
    input.value = task;
    input.focus();
    input.select();
  }
}

function shareRun(timestamp) {
  const url = `${location.origin}/run/${encodeURIComponent(timestamp)}`;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(url).then(() => showToast('Link copied'))
      .catch(() => prompt('Copy this link:', url));
  } else {
    prompt('Copy this link:', url);
  }
}

function showToast(msg) {
  const t = document.createElement('div');
  t.className = 'toast';
  t.setAttribute('role', 'status');
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 400); }, 2000);
}

// ── Task templates ───────────────────────────────────────────────
const _TEMPLATES = [
  'Build a REST API with FastAPI and SQLite',
  'Write a Python web scraper with BeautifulSoup',
  'Create a CLI tool in Python with argparse',
  'Write a data analysis script for a CSV file',
  'Build a React component library starter',
];

function renderTemplates() {
  const el = $('task-templates');
  if (!el) return;
  el.innerHTML = '<span class="template-label">Try:</span>' + _TEMPLATES.map(t =>
    `<button type="button" class="template-chip" data-template="${escHtml(t)}">${escHtml(t)}</button>`
  ).join('');
}

// ── Download / share ─────────────────────────────────────────────
function downloadOutput() {
  if (!_currentModalTimestamp) return;
  const a = document.createElement('a');
  a.href = `/history/${encodeURIComponent(_currentModalTimestamp)}/download`;
  a.download = `output_${_currentModalTimestamp}.zip`;
  a.click();
}

function copyShareLink() {
  if (!_currentModalTimestamp) return;
  const url = `${location.origin}/run/${encodeURIComponent(_currentModalTimestamp)}`;
  const done = () => {
    const btn = $('modal-share-btn');
    if (btn) { btn.textContent = 'Copied!'; setTimeout(() => btn.textContent = 'Copy link', 1500); }
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(url).then(done).catch(() => prompt('Copy this link:', url));
  } else {
    prompt('Copy this link:', url);   // non-HTTPS contexts have no clipboard API
  }
}

// ── Wiring ───────────────────────────────────────────────────────
// One delegated listener rather than an onclick attribute per control. The
// markup stays declarative and every one of these is keyboard-operable,
// because they are all real <button> and <a> elements now.
document.addEventListener('click', (e) => {
  const t = e.target.closest('[data-tab], [data-close-modal], [data-node], [data-template], ' +
                             '[data-fork], [data-share], [data-continue-project], .code-block-copy');
  if (!t) return;

  if (t.dataset.tab) { showTab(t.dataset.tab); return; }
  if (t.dataset.closeModal) { closeModal(t.dataset.closeModal); return; }
  if (t.dataset.node) { openNodeModal(JSON.parse(t.dataset.node)); return; }
  if (t.dataset.template) {
    const input = $('pitch-input');
    input.value = t.dataset.template;
    input.focus();
    return;
  }
  if (t.dataset.fork !== undefined && t.hasAttribute('data-fork')) {
    forkTask(t.dataset.fork, t.dataset.project || '');
    return;
  }
  if (t.dataset.share) { shareRun(t.dataset.share); return; }
  if (t.dataset.continueProject) {
    continueProject(t.dataset.continueProject, t.dataset.projectName);
    return;
  }
  if (t.classList.contains('code-block-copy')) {
    const code = t.closest('.code-block').querySelector('pre').textContent;
    navigator.clipboard?.writeText(code).then(() => {
      t.textContent = 'copied!';
      setTimeout(() => t.textContent = 'copy', 1500);
    });
  }
});

// Clicking the backdrop closes a dialog — but only the backdrop itself.
['output-modal', 'node-modal'].forEach(id => {
  $(id).addEventListener('click', (e) => { if (e.target === $(id)) closeModal(id); });
});

$('nav-toggle').addEventListener('click', toggleNav);
$('theme-toggle').addEventListener('click', toggleTheme);
$('focus-pitch').addEventListener('click', focusPitch);
$('pitch-btn').addEventListener('click', pitchTask);
$('clear-project').addEventListener('click', clearProjectContext);
$('new-project').addEventListener('click', promptNewProject);
$('modal-download-btn').addEventListener('click', downloadOutput);
$('modal-share-btn').addEventListener('click', copyShareLink);

$('pitch-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) pitchTask();
});

$('history-search').addEventListener('input', () => {
  clearTimeout(_historySearchTimer);
  _historySearchTimer = setTimeout(loadHistory, 250);
});

document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  if (anyModalOpen()) { closeModal('output-modal'); closeModal('node-modal'); return; }
  if ($('app').classList.contains('nav-open')) closeDrawer();
});

// A drawer left open while the window grows back to desktop leaves a stale
// scrim over a perfectly normal layout.
window.matchMedia(DRAWER_QUERY).addEventListener?.('change', (e) => {
  if (!e.matches) closeDrawer();
});

// ── Start ────────────────────────────────────────────────────────
renderTemplates();

// Restore the view from the URL. #run=<ts> opens a run; #gallery selects a
// view. Before this, reloading on #gallery silently landed on Overview.
(function restoreFromHash() {
  const runMatch = (location.hash || '').match(/#run=([^&]+)/);
  if (runMatch) { viewRun(decodeURIComponent(runMatch[1])); return; }
  const name = (location.hash || '').slice(1);
  if (TABS.indexOf(name) !== -1) showTab(name, {pushHash: false});
})();

connectWebSocket();
refresh();
loadHistory();
loadStandings();
loadProjects();

setInterval(refresh, 3000);
setInterval(pollEvents, 3000);   // fallback only — no-ops when WS is connected
setInterval(loadHistory, 15000);
setInterval(loadStandings, 10000);
setInterval(loadProjects, 20000);
