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

// ── Routing ──────────────────────────────────────────────────────
// The whole dashboard used to be one URL: showTab() swapped a div and left
// the address bar alone, so no view could be linked, bookmarked or refreshed
// into, and the back button did nothing at all.
//
// Each view has a real path now — /dashboard/gallery — and an open run is
// /dashboard/runs/<id>, so back closes the run rather than leaving the page.
// Real paths rather than hash routes because these get pasted into posts
// alongside /run/{id}, and "#gallery" reads like an anchor, not a page.
// The old #hash links still work; they are rewritten on arrival.

const ROOT = '/dashboard';

function pathFor(view, runId) {
  let path = view === 'overview' ? ROOT : `${ROOT}/${view}`;
  if (runId) path += `/${encodeURIComponent(runId)}`;
  return path;
}

/** Parse a URL into {view, run}. Unknown views fall back to overview rather
 *  than showing an empty shell. */
function parseRoute(url) {
  const u = new URL(url, location.origin);

  // Back-compat: /dashboard#gallery and /dashboard#run=<id> predate this.
  const hash = (u.hash || '').replace(/^#/, '');
  if (hash) {
    const runMatch = hash.match(/^run=(.+)$/);
    if (runMatch) return {view: 'runs', run: decodeURIComponent(runMatch[1]), legacy: true};
    if (TABS.indexOf(hash) !== -1) return {view: hash, run: null, legacy: true};
  }

  const parts = u.pathname.replace(/\/+$/, '').split('/').filter(Boolean);
  // ['dashboard'] | ['dashboard', view] | ['dashboard', 'runs', id]
  const view = TABS.indexOf(parts[1]) !== -1 ? parts[1] : 'overview';
  const run = parts[2] ? decodeURIComponent(parts[2]) : null;
  return {view, run};
}

let _applyingRoute = false;

/** The single place a view becomes visible. `push` controls history. */
function applyRoute({view, run}, push) {
  _applyingRoute = true;
  try {
    if (TABS.indexOf(view) === -1) view = 'overview';
    TABS.forEach(t => {
      const el = $('view-' + t);
      const btn = $('tab-' + t);
      if (el) el.hidden = (t !== view);
      if (btn) {
        // aria-current is how a screen reader announces "you are here".
        if (t === view) btn.setAttribute('aria-current', 'page');
        else btn.removeAttribute('aria-current');
      }
    });
    const title = $('view-title');
    if (title) title.textContent = TAB_TITLES[view] || view;
    document.title = `${TAB_TITLES[view] || view} — Mycelium`;

    if (isDrawer()) closeDrawer();

    if (view === 'gallery') loadGallery();
    if (view === 'guild') loadStandings();
    if (view === 'runs') loadHistory();

    const target = pathFor(view, run);
    if (push === 'push' && location.pathname + location.hash !== target) {
      history.pushState({view, run}, '', target);
    } else if (push === 'replace') {
      history.replaceState({view, run}, '', target);
    }

    // A run in the URL means the panel is open; no run means it is not.
    if (run) {
      if (_currentModalTimestamp !== run || $('output-modal').hidden) viewRun(run);
    } else if (!$('output-modal').hidden) {
      closeModal('output-modal', {silent: true});
    }
  } finally {
    _applyingRoute = false;
  }
}

/** Navigate — the only caller that adds a history entry. */
function go(view, run) {
  applyRoute({view, run}, 'push');
}

/** Kept as the name every caller already uses. */
function showTab(name) { go(name, null); }

// Back and forward now do what they look like they do.
window.addEventListener('popstate', (e) => {
  applyRoute(e.state && e.state.view ? e.state : parseRoute(location.href), null);
});

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

function closeModal(id, opts = {}) {
  const m = $(id);
  if (!m || m.hidden) return;
  m.hidden = true;
  if (_lastFocused && document.contains(_lastFocused)) _lastFocused.focus();
  _lastFocused = null;
  // Closing the run panel is a navigation: the URL said a run was open, so
  // it has to stop saying that, or Back lands on a page that reopens it.
  if (id === 'output-modal' && !opts.silent && !_applyingRoute) {
    _currentModalTimestamp = null;
    const {view} = parseRoute(location.href);
    applyRoute({view, run: null}, 'push');
  }
}

function anyModalOpen() {
  return ['output-modal', 'node-modal'].some(id => !$(id).hidden);
}

// ── Feedback ─────────────────────────────────────────────────────
// One helper, so every action confirms itself the same way. Before this each
// caller invented its own: some flashed a button label, some did nothing, and
// the clipboard fallbacks called prompt() — a blocking, unstyleable system
// dialog sitting directly on the share path, which is the single thing a
// stranger is most likely to do after watching a run.

let _toastTimer = null;

/**
 * @param {string} message  what happened
 * @param {object} [opts]   {kind: 'ok'|'error', field: string, timeout: ms}
 *   `field` shows the text in a pre-selected read-only input — the fallback
 *   for when the clipboard is unavailable, which is every page served over
 *   plain HTTP, i.e. this one.
 */
function notify(message, opts = {}) {
  const {kind = 'ok', field = null, timeout = field ? 0 : 2400} = opts;
  clearTimeout(_toastTimer);
  document.querySelectorAll('.toast').forEach(t => t.remove());

  const toast = document.createElement('div');
  toast.className = `toast is-${kind}` + (field ? ' has-field' : '');
  // An error has to interrupt; a confirmation must not.
  toast.setAttribute('role', kind === 'error' ? 'alert' : 'status');

  const line = document.createElement('span');
  line.className = 'toast-msg';
  line.textContent = message;
  toast.appendChild(line);

  if (field) {
    const input = document.createElement('input');
    input.className = 'toast-field';
    input.readOnly = true;
    input.value = field;
    input.setAttribute('aria-label', 'Copy this text');
    toast.appendChild(input);

    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'toast-close';
    close.textContent = 'Done';
    close.addEventListener('click', () => dismissToast(toast));
    toast.appendChild(close);

    document.body.appendChild(toast);
    input.focus();
    input.select();
    return toast;
  }

  document.body.appendChild(toast);
  if (timeout) _toastTimer = setTimeout(() => dismissToast(toast), timeout);
  return toast;
}

function dismissToast(toast) {
  if (!toast || !toast.isConnected) return;
  toast.classList.add('is-leaving');
  setTimeout(() => toast.remove(), 200);
}

/** Copy, confirm visibly, and degrade to something selectable — never to a
 *  system dialog. Optionally flashes the button that triggered it, matching
 *  what copyCode() already did well. */
function copyToClipboard(text, confirmation, button) {
  const flash = () => {
    if (!button) return;
    const was = button.textContent;
    button.textContent = 'Copied!';
    setTimeout(() => { button.textContent = was; }, 1500);
  };
  const fallback = () => notify('Copy this yourself — the browser blocked the clipboard',
                                {kind: 'error', field: text});

  if (!navigator.clipboard || !navigator.clipboard.writeText) { fallback(); return; }
  navigator.clipboard.writeText(text)
    .then(() => { flash(); notify(confirmation); })
    .catch(fallback);
}

// ── Empty, loading, error ────────────────────────────────────────
// These were one state before: a panel that had not loaded yet and a network
// with nothing in it rendered identically, so a slow fetch looked like a
// broken page — and a failed fetch looked like an empty one, silently.
//
// Now: a skeleton while data is in flight, an empty state that offers the
// action which resolves it, and an error that says what failed and offers
// a retry. Every list goes through these three.

/** Grey bars in the shape of the content that is coming. */
function skeleton(rows = 3, kind = 'row') {
  const one = kind === 'card'
    ? '<div class="skel-card"><div class="skel-line is-title"></div>' +
      '<div class="skel-line"></div><div class="skel-line is-short"></div></div>'
    : '<div class="skel-row"><div class="skel-line is-title"></div>' +
      '<div class="skel-line is-short"></div></div>';
  return `<div class="skeleton" aria-hidden="true">${one.repeat(rows)}</div>` +
         '<span class="sr-only" role="status">Loading…</span>';
}

/**
 * An empty state that teaches rather than apologises: it says what would be
 * here, and gives the one control that makes it appear.
 * @param {object} o {title, body, action: {label, id, href}}
 */
function emptyState(o) {
  const action = o.action
    ? (o.action.href
        ? `<a class="btn-accent" href="${escHtml(o.action.href)}">${escHtml(o.action.label)}</a>`
        : `<button type="button" class="btn-accent" data-empty-action="${escHtml(o.action.id)}">${escHtml(o.action.label)}</button>`)
    : '';
  return `<div class="empty-state${o.grid ? ' is-grid' : ''}">
    ${o.icon || ''}
    <p><b>${escHtml(o.title)}</b><br>${o.body}</p>
    ${action ? `<div class="empty-action">${action}</div>` : ''}
  </div>`;
}

/** A failed fetch says so, and offers the retry, instead of showing nothing. */
function errorState(what, retryId) {
  return `<div class="empty-state is-error">
    <p><b>Could not load ${escHtml(what)}.</b><br>
       The orchestrator did not answer. It may be restarting.</p>
    <div class="empty-action">
      <button type="button" class="btn-quiet" data-retry="${escHtml(retryId)}">Try again</button>
    </div>
  </div>`;
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
    nodesList.innerHTML = emptyState({
      icon: '<div class="icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="5" r="2.2"/><circle cx="5" cy="18" r="2.2"/><circle cx="19" cy="18" r="2.2"/><path d="M10.5 6.8 6.5 15.8M13.5 6.8l4 9M7.4 18h9.2"/></svg></div>',
      title: 'No machines connected.',
      body: 'The orchestrator still runs work on itself. One command joins another machine:' +
            `<br><code class="code-inline">python join.py ${escHtml(location.origin)}</code>`,
      action: {label: 'Copy the join command', id: 'copy-join'},
    });
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
    // Verification record. Hidden entirely until this node has actually been
    // spot-checked, so a server with verify_rate=0 looks exactly as before.
    const samples = n.verified_samples || 0;
    const weight = (n.routing_weight != null) ? n.routing_weight : 1;
    const repHtml = samples > 0
      ? `<div class="node-meta node-rep" title="Sampled tasks are run on two nodes and the answers compared. Routing weight decides who is offered work first; it never excludes a node.">
           <span class="${weight >= 0.9 ? 'is-trusted' : 'is-sampling'}">&#9670; ${weight.toFixed(2)} routing weight</span>
           <span> &middot; ${Math.round((n.agreement_score != null ? n.agreement_score : 1) * 100)}% agreement over ${samples} check${samples === 1 ? '' : 's'}${n.trusted_for_routing ? '' : ' (still sampling)'}</span>
         </div>`
      : '';
    return `
      <button type="button" class="node-card active" id="nodecard-${escHtml(n.node_id)}"
              data-node="${escHtml(JSON.stringify(n))}">
        <span class="node-name"><span class="${dotClass}" aria-hidden="true"></span>${escHtml(n.node_id)}</span>
        <div class="node-meta">${escHtml(n.platform)} / ${escHtml(n.machine)}</div>
        <div class="node-meta">${escHtml(n.model)}</div>
        ${hwHtml}${capsRow}
        <div class="node-tasks">${n.tasks_completed} tasks &middot; ${n.credits_earned || 0} credits</div>
        ${repHtml}
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
          <button type="button" class="btn-quiet is-sm" id="pcancel-${pipelineId}" hidden>Stop</button>
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
          totalSubtasks = ev.subtasks ? ev.subtasks.length : 0;
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

    // Only offered once there is a job to stop. A pitch is minutes on a 4B
    // CPU model and there was no way to take one back.
    const cancelBtn = $(`pcancel-${pipelineId}`);
    if (cancelBtn) {
      cancelBtn.hidden = false;
      cancelBtn.addEventListener('click', () => cancelJob(data.job_id, pipelineId, cancelBtn));
    }
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

/** Ask the server to stop. Honest about what that does and does not do. */
async function cancelJob(jobId, pipelineId, button) {
  if (button) { button.disabled = true; button.textContent = 'Stopping…'; }
  const statusEl = $(`pstatus-${pipelineId}`);
  try {
    const resp = await fetch(`/jobs/${encodeURIComponent(jobId)}/cancel`, {method: 'POST'});
    const body = await resp.json();
    if (!resp.ok) {
      notify(body.detail || 'Could not stop that run.', {kind: 'error'});
      if (button) { button.disabled = false; button.textContent = 'Stop'; }
      return;
    }
    if (statusEl) {
      statusEl.className = 'pipeline-status status-pending';
      statusEl.textContent = body.still_running ? 'STOPPING' : 'STOPPED';
    }
    // Say plainly that a machine mid-subtask is allowed to finish — otherwise
    // "stopped" followed by thirty more seconds of activity looks like a bug.
    notify(body.detail);
    if (button) button.hidden = true;
  } catch (e) {
    notify('Could not reach the server to stop that run.', {kind: 'error'});
    if (button) { button.disabled = false; button.textContent = 'Stop'; }
  }
}

async function _pollJobCompletion(jobId, pipelineId, task, stageWatcher, elapsedTicker) {
  while (true) {
    await new Promise(r => setTimeout(r, 3000));
    try {
      const job = await (await fetch(`/jobs/${jobId}`)).json();
      if (job.status === 'complete' || job.status === 'failed' || job.status === 'cancelled') {
        clearInterval(stageWatcher);
        clearInterval(elapsedTicker);
        _showCompletedCard(pipelineId, task, {
          plan: job.plan || [],
          project_dir: job.project_dir || '',
          rating: job.rating,
          status: job.status,
          mode: job.mode,
          error: job.error || null,
          cancelled_during: job.cancelled_during,
          completed_subtasks: job.completed_subtasks || [],
          credits_settled: job.credits_settled,
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
  const cancelled = result.status === 'cancelled';
  const statusClass = (failed || cancelled) ? 'status-pending' : 'status-complete';
  const statusText = failed ? 'FAILED'
    : cancelled ? 'STOPPED'
    : (result.mode === 'distributed' ? 'DISTRIBUTED' : 'COMPLETE');
  const badge = (failed || cancelled) ? '' : ratingBadge(result.rating);

  // A stopped run lists what it did get through, and what that was paid.
  // "Cancelled" with nothing beside it reads as work thrown away.
  const done = result.completed_subtasks || [];
  let subtasksHtml = '<div class="subtask-list">';
  if (cancelled) {
    done.forEach(st => {
      subtasksHtml += `
        <div class="subtask">
          <span class="subtask-id">${escHtml(String(st.id))}</span>
          <span class="subtask-title">${escHtml(st.title)}</span>
          ${st.executor ? `<span class="mono-dim">${escHtml(st.executor)}</span>` : ''}
          <span class="subtask-check" aria-hidden="true">&#10003;</span>
        </div>`;
    });
    if (!done.length) {
      subtasksHtml += '<div class="subtask"><span class="subtask-title">' +
        'Stopped before any subtask finished.</span></div>';
    }
  } else {
    (result.plan || []).forEach(st => {
      subtasksHtml += `
        <div class="subtask">
          <span class="subtask-id">${escHtml(String(st.id))}</span>
          <span class="subtask-title">${escHtml(st.title)}</span>
          <span class="subtask-check" aria-hidden="true">&#10003;</span>
        </div>`;
    });
  }
  subtasksHtml += '</div>';

  const cancelNote = cancelled
    ? `<p class="cancel-note">Stopped during ${escHtml(result.cancelled_during || 'the run')}.
       ${done.length} subtask${done.length === 1 ? '' : 's'} finished and
       ${result.credits_settled ? `<b>${escHtml(result.credits_settled)} credits</b> were settled`
                                : 'nothing was settled'}.
       Work already on a machine was allowed to finish rather than being thrown away.</p>`
    : '';

  const ts = result.project_dir ? result.project_dir.split(/[\\/]/).pop() : '';

  card.innerHTML = `
    <div class="pipeline-header">
      <div class="pipeline-task">${escHtml(task)}</div>
      <div class="pipeline-head-right">${badge}<div class="pipeline-status ${statusClass}">${statusText}</div></div>
    </div>
    ${cancelled ? '' : stageBarHtml(true, true, true)}
    ${subtasksHtml}
    ${cancelNote}
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
    const resp = await fetch(`/history/${encodeURIComponent(timestamp)}`);
    if (!resp.ok) throw new Error(`the server returned ${resp.status}`);
    const data = await resp.json();

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
    // Silently doing nothing on a dead link is what made this look broken.
    _currentModalTimestamp = null;
    notify(`Could not open that run — ${e.message}`, {kind: 'error'});
  }
}

/** Open a run as a navigation, so the URL carries it and Back closes it. */
function openRun(timestamp) {
  const {view} = parseRoute(location.href);
  applyRoute({view: view === 'overview' ? 'runs' : view, run: timestamp}, 'push');
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
  cancelling:   {agent: 'STOPPING', cls: 'is-error'},
  cancelled:    {agent: 'STOPPED',  cls: 'is-error'},
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
  if (ev.type === 'node_busy') { _setNodeBusy(ev.node_id, ev.task_title); return; }
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
  if (ev.type === 'pitch') msg = `Task pitched: "${escHtml(ev.task)}"`;
  else if (ev.type === 'plan') msg = `Decomposed into ${ev.subtasks.length} subtasks: ${ev.subtasks.map(escHtml).join(', ')}`;
  else if (ev.type === 'build') msg = `Subtask ${ev.subtask_id} complete: ${escHtml(ev.subtask)}`;
  else if (ev.type === 'review_start') msg = 'Reviewing combined output...';
  else if (ev.type === 'complete') msg = `Pipeline complete → ${escHtml(ev.project_dir)}`;
  else if (ev.type === 'cancelling') {
    msg = `Stop requested — dropped ${ev.dropped} queued subtask(s), ` +
          `${ev.still_running} already running will finish`;
  }
  else if (ev.type === 'cancelled') {
    msg = `Stopped during ${escHtml(ev.stage)} — ${ev.completed} subtask(s) finished, ` +
          `${ev.credits} credits settled`;
  }
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

// A dropped socket used to be entirely silent: the activity log simply
// stopped moving, which is indistinguishable from a quiet network. Now it
// says so, and says when it is back.
let _wsAttempts = 0;

function setConnectionState(state) {
  const host = $('conn-banner');
  if (!host) return;
  if (state === 'live') { host.hidden = true; host.innerHTML = ''; return; }

  const down = state === 'down';
  host.hidden = false;
  host.className = 'conn-banner' + (down ? ' is-down' : '');
  host.innerHTML =
    '<span class="dot" aria-hidden="true"></span>' +
    `<span>${down
      ? 'Lost the live connection to the orchestrator. Runs already started keep going.'
      : 'Reconnecting to the live feed…'}</span>` +
    '<span class="spacer"></span>' +
    (down ? '<button type="button" class="btn-quiet is-sm" data-retry="socket">Reconnect now</button>' : '');
}

function connectWebSocket() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  let ws;
  try {
    ws = new WebSocket(`${proto}://${location.host}/ws/events`);
  } catch (e) {
    setConnectionState('down');
    return;
  }
  ws.onopen = () => {
    wsConnected = true;
    if (_wsAttempts > 0) notify('Live feed reconnected');
    _wsAttempts = 0;
    setConnectionState('live');
  };
  ws.onmessage = (e) => {
    try {
      const ev = JSON.parse(e.data);
      appendEvent(ev);
      if (ev.id && ev.id > eventCursor) eventCursor = ev.id;
    } catch (_) {}
  };
  ws.onclose = () => {
    wsConnected = false;
    _wsAttempts++;
    // One drop is a blip and reconnects in three seconds; a run of them is
    // worth telling someone about, and polling still covers the gap.
    setConnectionState(_wsAttempts >= 2 ? 'down' : 'reconnecting');
    setTimeout(connectWebSocket, Math.min(3000 * _wsAttempts, 15000));
  };
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

let _historyLoaded = false;

async function loadHistory(opts = {}) {
  const el = $('history-list');
  if (!el) return;
  const q = ($('history-search')?.value || '').trim();
  const url = q ? `/history?search=${encodeURIComponent(q)}` : '/history';

  // Only skeleton the first paint and explicit reloads — the 15s poll must
  // not make a populated list flicker back to grey bars.
  if (!_historyLoaded || opts.showLoading) el.innerHTML = skeleton(4);

  try {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(resp.status);
    const data = await resp.json();
    _historyLoaded = true;

    if (data.count === 0) {
      el.innerHTML = q
        ? emptyState({
            title: `Nothing matches “${escHtml(q)}”.`,
            body: 'Try a shorter search, or clear it to see everything.',
            action: {label: 'Clear search', id: 'clear-search'},
          })
        : emptyState({
            title: 'No runs yet.',
            body: 'Every completed pitch lands here with a page of its own that you can share.',
            action: {label: 'Pitch the first one', id: 'focus-pitch'},
          });
      return;
    }

    // The row is not itself a link: the task opens the shareable page, and
    // Preview opens the run inside the dashboard without losing your place.
    // A link nested inside a link would be neither.
    el.innerHTML = data.runs.map(r => `
      <div class="history-card">
        <a class="history-task" href="/run/${encodeURIComponent(r.timestamp)}">${escHtml(r.task)}</a>
        <span class="history-meta">
          ${distBadge(r.mode)}
          ${ratingBadge(r.rating)}
          <span class="mono-dim">${r.subtask_count} tasks</span>
          <span class="mono-dim">${relativeTime(r.timestamp)}</span>
          <button type="button" class="btn-quiet is-sm" data-preview="${escHtml(r.timestamp)}">Preview</button>
        </span>
      </div>`).join('');
  } catch (e) {
    if (!_historyLoaded) el.innerHTML = errorState('past runs', 'history');
  }
}

// ── Standings ────────────────────────────────────────────────────
let _standingsLoaded = false;

async function loadStandings(opts = {}) {
  const el = $('standings-list');
  if (!el) return;
  if (!_standingsLoaded || opts.showLoading) el.innerHTML = skeleton(3);
  try {
    const resp = await fetch('/standings');
    if (!resp.ok) throw new Error(resp.status);
    const data = await resp.json();
    _standingsLoaded = true;

    if (!data.standings.length) {
      el.innerHTML = emptyState({
        title: 'Nobody has earned anything yet.',
        body: 'Credits are recorded when a machine builds a subtask, reviews a result, or pitches a task.',
        action: {label: 'Pitch a task', id: 'focus-pitch'},
      });
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
  } catch (e) {
    if (!_standingsLoaded) el.innerHTML = errorState('the standings', 'standings');
  }
}

// ── Projects ─────────────────────────────────────────────────────
let _projectsLoaded = false;

async function loadProjects(opts = {}) {
  const el = $('projects-list');
  if (!el) return;
  if (!_projectsLoaded || opts.showLoading) el.innerHTML = skeleton(2);
  try {
    const resp = await fetch('/projects');
    if (!resp.ok) throw new Error(resp.status);
    const data = await resp.json();
    _projectsLoaded = true;

    if (!data.projects || data.projects.length === 0) {
      el.innerHTML = emptyState({
        title: 'No projects yet.',
        body: 'A project carries memory from one pitch to the next, so the second ' +
              'task knows what the first one built.',
        action: {label: 'Start a project', id: 'new-project'},
      });
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
  } catch (e) {
    if (!_projectsLoaded) el.innerHTML = errorState('projects', 'projects');
  }
}

// Naming a project used to be a native prompt() — a blocking grey box with
// the origin printed above it, which looks like the page has been hijacked
// rather than like a feature. It is an inline form now: it can be styled,
// it can be cancelled, it can show an error, and it does not freeze the page
// while a pipeline is running behind it.
function openNewProjectForm() {
  const form = $('new-project-form');
  form.hidden = false;
  $('new-project').setAttribute('aria-expanded', 'true');
  $('new-project-name').value = '';
  $('new-project-error').hidden = true;
  $('new-project-name').focus();
}

function closeNewProjectForm() {
  $('new-project-form').hidden = true;
  $('new-project').setAttribute('aria-expanded', 'false');
  $('new-project').focus();
}

async function createProject() {
  const input = $('new-project-name');
  const submit = $('new-project-submit');
  const name = input.value.trim();
  const fail = (msg) => {
    const err = $('new-project-error');
    err.textContent = msg;
    err.hidden = false;
    input.focus();
  };
  if (!name) { fail('Give the project a name first.'); return; }

  submit.disabled = true;
  submit.textContent = 'Creating…';
  try {
    const resp = await fetch('/projects', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, initial_task: ''}),
    });
    const data = await resp.json();
    if (!resp.ok || !data.project_id) {
      fail(data.detail || 'The server would not create that project.');
      return;
    }
    closeNewProjectForm();
    await loadProjects();
    notify(`Project “${data.name}” created`);
    continueProject(data.project_id, data.name);
  } catch (e) {
    fail('Could not reach the server.');
  } finally {
    submit.disabled = false;
    submit.textContent = 'Create';
  }
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
let _galleryLoaded = false;

async function loadGallery(opts = {}) {
  const el = $('gallery-grid');
  if (!el) return;
  if (!_galleryLoaded || opts.showLoading) el.innerHTML = skeleton(6, 'card');
  try {
    const resp = await fetch('/gallery');
    if (!resp.ok) throw new Error(resp.status);
    const data = await resp.json();
    _galleryLoaded = true;

    if (!data.cards || data.cards.length === 0) {
      el.innerHTML = emptyState({
        grid: true,
        title: 'Nothing built yet.',
        body: 'Finished work shows up here, each card with a page of its own you can paste anywhere.',
        action: {label: 'Pitch the first task', id: 'focus-pitch'},
      });
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
            <button type="button" class="gallery-btn open" data-preview="${escHtml(c.timestamp)}">Preview</button>
            <a class="gallery-btn" href="/run/${ts}">Open page &#8599;</a>
            <button type="button" class="gallery-btn" data-fork="${escHtml(c.task)}"
                    data-project="${escHtml(c.project_id || '')}">Fork &amp; continue</button>
            <button type="button" class="gallery-btn share" data-share="${escHtml(c.timestamp)}"
                    title="Copy a link to this run">Share &#x2197;</button>
          </div>
        </div>`;
    }).join('');
  } catch (e) {
    if (!_galleryLoaded) el.innerHTML = errorState('the gallery', 'gallery');
  }
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
  copyToClipboard(`${location.origin}/run/${encodeURIComponent(timestamp)}`, 'Link copied');
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
  copyToClipboard(`${location.origin}/run/${encodeURIComponent(_currentModalTimestamp)}`,
                  'Link copied', $('modal-share-btn'));
}

// ── What empty and error states offer ────────────────────────────
// Every empty state names the one action that resolves it, and every error
// offers the retry, rather than being a dead end.
const EMPTY_ACTIONS = {
  'focus-pitch': () => focusPitch(),
  'new-project': () => { go('projects'); openNewProjectForm(); },
  'clear-search': () => {
    const box = $('history-search');
    if (box) { box.value = ''; loadHistory({showLoading: true}); box.focus(); }
  },
  'copy-join': () => copyToClipboard(`python join.py ${location.origin}`, 'Join command copied'),
};

const RETRIES = {
  history:   () => loadHistory({showLoading: true}),
  standings: () => loadStandings({showLoading: true}),
  projects:  () => loadProjects({showLoading: true}),
  gallery:   () => loadGallery({showLoading: true}),
  socket:    () => { _wsAttempts = 0; setConnectionState('reconnecting'); connectWebSocket(); },
};

// ── Wiring ───────────────────────────────────────────────────────
// One delegated listener rather than an onclick attribute per control. The
// markup stays declarative and every one of these is keyboard-operable,
// because they are all real <button> and <a> elements now.
document.addEventListener('click', (e) => {
  const t = e.target.closest('[data-tab], [data-close-modal], [data-node], [data-template], ' +
                             '[data-fork], [data-share], [data-continue-project], [data-preview], ' +
                             '[data-empty-action], [data-retry], .code-block-copy');
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
  if (t.dataset.preview) { openRun(t.dataset.preview); return; }
  if (t.dataset.emptyAction) { EMPTY_ACTIONS[t.dataset.emptyAction]?.(); return; }
  if (t.dataset.retry) { RETRIES[t.dataset.retry]?.(); return; }
  if (t.dataset.continueProject) {
    continueProject(t.dataset.continueProject, t.dataset.projectName);
    return;
  }
  if (t.classList.contains('code-block-copy')) {
    // Through the same helper as every other copy, so a blocked clipboard
    // degrades to a selectable field here too rather than failing silently.
    copyToClipboard(t.closest('.code-block').querySelector('pre').textContent,
                    'Code copied', t);
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
$('new-project').addEventListener('click', () =>
  $('new-project-form').hidden ? openNewProjectForm() : closeNewProjectForm());
$('new-project-submit').addEventListener('click', createProject);
$('new-project-cancel').addEventListener('click', closeNewProjectForm);
$('new-project-name').addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); createProject(); }
  if (e.key === 'Escape') { e.preventDefault(); closeNewProjectForm(); }
});
$('modal-download-btn').addEventListener('click', downloadOutput);
$('modal-share-btn').addEventListener('click', copyShareLink);

$('pitch-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) pitchTask();
});

$('history-search').addEventListener('input', () => {
  clearTimeout(_historySearchTimer);
  _historySearchTimer = setTimeout(() => loadHistory({showLoading: true}), 250);
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
// Restore whatever the URL asks for, without adding a history entry for
// simply arriving. A legacy #hash link is rewritten to its path form here, so
// old bookmarks keep working and stop propagating the old shape.
(function restoreRoute() {
  const route = parseRoute(location.href);
  applyRoute(route, 'replace');
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
