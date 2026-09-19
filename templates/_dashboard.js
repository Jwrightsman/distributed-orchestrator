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

/* ── Viewer auth and degraded state ───────────────────────────────
   Every private route answers 401 when viewer_key is set and this browser has
   no session, and /ws/events closes 4401 in the same situation. Before this,
   both produced a shell full of empty tables — a locked server that read as a
   broken one.

   The static key is never in this file. The operator types it into the locked
   screen, it is exchanged once at POST /v1/viewer/session, and what comes back
   is an HttpOnly cookie this script cannot read. So every request here is a
   plain same-origin fetch that carries the cookie, and no code path holds a
   credential. */

let viewerLocked = false;
let currentTab = 'overview';

/* Thrown instead of returning, so a caller's `await apiFetch(...)` unwinds
   rather than continuing on to parse a 401 body as data. Callers already wrap
   their loads in try/catch; this rides that. */
class ViewerLocked extends Error {
  constructor() { super('viewer authentication required'); this.name = 'ViewerLocked'; }
}

function showLockedScreen() {
  if (viewerLocked) return;          // idempotent: 13 fetches can all 401 at once
  viewerLocked = true;
  const app = $('app'), locked = $('locked-screen');
  if (!locked || !app) return;
  app.hidden = true;
  locked.hidden = false;
  // The bar belongs to the console, and the console is gone. The panel's own
  // line carries the same derived state so there is one lamp on screen, not
  // two — and it is the same lamp, so it cannot disagree with itself.
  const bar = $('statusbar');
  if (bar) bar.hidden = true;
  renderStatus();
  const field = $('locked-key');
  if (field) field.focus();
}

/* Every private request goes through here. Public ones (/status.json) do not
   need to, and deliberately do not, so a locked console can still prove the
   server is alive. */
async function apiFetch(url, opts) {
  const resp = await fetch(url, Object.assign({credentials: 'same-origin'}, opts || {}));
  if (resp.status === 401) {
    showLockedScreen();
    throw new ViewerLocked();
  }
  if (resp.status === 503) {
    // Only the persistence refusal speaks for `commit`. A generic 503 is not
    // the same claim and must not borrow its wording.
    let code = '';
    try { code = ((await resp.clone().json()).detail || {}).code || ''; } catch (e) {}
    // Stated by the coordinator, so it is believed on this observation rather
    // than waiting for a second poll to agree.
    if (code === 'execution_persistence_unavailable') {
      STATUS_MODEL.writeRefused(statusState, Date.now());
      renderStatus();
    }
  } else if (resp.ok && (opts || {}).method && String(opts.method).toUpperCase() !== 'GET') {
    // A write that landed is the only proof the coordinator is writing again.
    // Clearing the latch does not turn the lamp green: the next two polls do
    // that, and the first of them reads `recovering`.
    STATUS_MODEL.writeAccepted(statusState);
  }
  return resp;
}

/* Convenience for the common `await (await fetch(x)).json()` shape. */
async function apiJson(url, opts) {
  return (await apiFetch(url, opts)).json();
}

function wireLockedScreen() {
  const form = $('locked-form');
  if (!form) return;
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const field = $('locked-key'), btn = $('locked-submit'), err = $('locked-error');
    const key = (field?.value || '').trim();
    if (!key) return;
    if (err) err.hidden = true;
    if (btn) { btn.disabled = true; btn.textContent = 'Unlocking…'; }
    try {
      const resp = await fetch('/v1/viewer/session', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        credentials: 'same-origin',
        body: JSON.stringify({viewer_key: key}),
      });
      if (resp.ok) {
        // The cookie is set. Reload rather than resuming in place: several
        // loads failed on the way here and their views are half-filled.
        if (field) field.value = '';
        location.reload();
        return;
      }
      if (err) {
        err.textContent = resp.status === 401
          ? 'That key was not accepted.'
          : resp.status === 409
            ? 'This server has no viewer key configured, so there is nothing to unlock.'
            : `The server answered ${resp.status}.`;
        err.hidden = false;
      }
    } catch (e2) {
      if (err) { err.textContent = 'Could not reach the server to exchange the key.'; err.hidden = false; }
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = 'Unlock this browser'; }
      if (field) field.focus();
    }
  });
}

/* ── A run's verdict, in five values ──────────────────────────────
   Ported from docs/design/status-model-2026-09-07 §06. Five, no sixth, and
   never a percentage: the published quality figures rest on a check that
   counted a blank page as a working app (HANDOFF-DELTA §4.1).

   PASS means the reviewer passed it *and* the mechanical check found no
   defects. So a run whose check never reached a verdict cannot wear it — an
   empty problem list beside a precheck error means "not checked", not
   "checked clean". The badge this replaces had two failure modes: it rendered
   nothing at all for rating "?", and it had no way to say "unchecked".

   execution/validators.py refuses to construct a ParsePrecheckResult carrying
   both a runner failure and code problems — a starved validator runner and a
   defect in the code are separate channels, deliberately. This function is
   where the UI could quietly re-merge them, so it reads the two fields
   separately and never lets one stand in for the other.

   Precedence is worst-wins, the same rule the status bar's severity uses: a
   named negative verdict outranks "we do not know", and "we do not know"
   outranks PASS. There is no path through this function that renders an
   unchecked run as PASS. */
const VERDICTS = {
  PASS:       {label: 'PASS',       cls: 'is-pass'},
  NEEDS_WORK: {label: 'NEEDS WORK', cls: 'is-needs-work'},
  FAIL:       {label: 'FAIL',       cls: 'is-fail'},
  UNCHECKED:  {label: 'UNCHECKED',  cls: 'is-unchecked'},
  NO_VERDICT: {label: 'NO VERDICT', cls: 'is-no-verdict'},
};
const RECORDED_RATINGS = ['PASS', 'NEEDS_WORK', 'FAIL'];

function runVerdict(rating, precheckError) {
  if (rating === 'FAIL') return 'FAIL';
  if (rating === 'NEEDS_WORK') return 'NEEDS_WORK';
  // No rating was recorded at all. That says nothing about the work, and it
  // is the more fundamental absence, so it is reported ahead of UNCHECKED.
  if (RECORDED_RATINGS.indexOf(rating) === -1) return 'NO_VERDICT';
  // The rating is PASS. The mechanical check is the other half of that claim.
  if (precheckError) return 'UNCHECKED';
  return 'PASS';
}

function verdictChip(rating, precheckError) {
  const v = VERDICTS[runVerdict(rating, precheckError)];
  return `<span class="verdict ${v.cls}"><i class="lamp" aria-hidden="true"></i>${v.label}</span>`;
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

function setRail(open) {
  const app = $('app');
  app.classList.toggle('rail-open', open);
  const btn = $('rail-toggle');
  if (btn) {
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    const word = open ? 'Collapse the rail' : 'Expand the rail';
    btn.title = word;
    btn.setAttribute('aria-label', word);
    btn.textContent = open ? '\u00ab' : '\u00bb';
  }
}

function toggleNav() {
  const app = $('app');
  // Below 900px the rail is an overlay drawer, so the top bar's control opens
  // it there. On a wide screen the rail's own control widens it.
  if (isDrawer()) {
    if (app.classList.contains('nav-open')) closeDrawer(); else openDrawer();
    return;
  }
  const open = !app.classList.contains('rail-open');
  setRail(open);
  try { localStorage.setItem('mycelium-rail', open ? '1' : '0'); } catch (e) {}
}

/* A fresh session lands on the collapsed rail. Only a remembered choice
   opens it: the rail is navigation, and the view is the page. */
try {
  setRail(localStorage.getItem('mycelium-rail') === '1');
} catch (e) { setRail(false); }

// ── The machine identity block ──────────────────────────────────
function setIdentMenu(open) {
  $('ident-menu').hidden = !open;
  $('ident-scrim').hidden = !open;
  const t = $('ident-toggle');
  t.setAttribute('aria-expanded', open ? 'true' : 'false');
  t.title = open ? 'Hide this menu' : 'This coordinator, settings and version';
  setText('ident-chev', open ? '\u2304' : '\u2303');
}
function identName(name) {
  setText('ident-menu-name', name);
  const parts = String(name).split(/[^A-Za-z0-9]+/).filter(Boolean);
  const initials = parts.length > 1
    ? parts[0][0] + parts[parts.length - 1][parts[parts.length - 1].length - 1]
    : String(name).slice(0, 2);
  setText('ident-initials', initials.toLowerCase());
}
setText('ident-origin', location.origin);

// ── Command palette ─────────────────────────────────────────────
/* Every command is a control that already exists somewhere on the page, so
   the palette adds no capability of its own: it is a faster way to reach
   them. Filtered as you type; Enter runs the highlighted one. */
const PALETTE_COMMANDS = () => [
  ...TABS.map(t => ({kind: 'VIEW', label: TAB_TITLES[t], hint: '#' + t, run: () => showTab(t)})),
  {kind: 'RUN', label: 'Pitch a task', hint: 'overview', run: () => focusPitch()},
  {kind: 'THEME', label: document.documentElement.getAttribute('data-theme') === 'dark'
      ? 'Switch to the light theme' : 'Switch to the dark theme', hint: '', run: () => toggleTheme()},
  {kind: 'PAGE', label: 'Open the public status page', hint: '/status', run: () => { location.href = '/status'; }},
];
let _paletteActive = 0;
let _paletteReturn = null;

function paletteMatches() {
  const q = ($('palette-input').value || '').trim().toLowerCase();
  return PALETTE_COMMANDS().filter(c => !q || (c.label + ' ' + c.kind + ' ' + c.hint).toLowerCase().includes(q));
}
function renderPalette() {
  const list = $('palette-list');
  const cmds = paletteMatches();
  if (_paletteActive >= cmds.length) _paletteActive = Math.max(0, cmds.length - 1);
  list.innerHTML = cmds.length ? cmds.map((c, i) => `
    <button class="palette-cmd${i === _paletteActive ? ' is-active' : ''}" type="button"
            role="option" aria-selected="${i === _paletteActive}" data-palette="${i}"
            title="${escHtml(c.label)}">
      <span class="palette-kind">${escHtml(c.kind)}</span>
      <span class="palette-label">${escHtml(c.label)}</span>
      <span class="palette-hint">${escHtml(c.hint)}</span>
    </button>`).join('') : '<div class="palette-empty">Nothing matches.</div>';
}
function openPalette() {
  _paletteReturn = document.activeElement;
  _paletteActive = 0;
  $('palette-input').value = '';
  $('palette').hidden = false;
  renderPalette();
  $('palette-input').focus();
}
function closePalette() {
  if ($('palette').hidden) return;
  $('palette').hidden = true;
  if (_paletteReturn && _paletteReturn.focus) _paletteReturn.focus();
}
function runPalette(i) {
  const cmd = paletteMatches()[i];
  closePalette();
  if (cmd) cmd.run();
}

// ── Views ────────────────────────────────────────────────────────
const TABS = ['overview', 'runs', 'gallery', 'nodes', 'projects', 'guild', 'evals', 'invite', 'config'];
const TAB_TITLES = {
  overview: 'Overview', runs: 'Runs', gallery: 'Gallery',
  nodes: 'Nodes', projects: 'Projects', guild: 'Guild standings',
  evals: 'Output quality', invite: 'Connect', config: 'Configuration',
};
/* The line under the title. Each is a claim about the view, so each says only
   what the view shows: no counts that a capped endpoint cannot back. */
const TAB_SUBS = {
  overview: 'Pitch work, and watch the machines answer',
  runs: 'Every pipeline this orchestrator has completed',
  gallery: 'Work the network finished, with the code it produced',
  nodes: 'Machines offering compute, and the work moving between them',
  projects: 'Work that remembers what it already built',
  guild: 'Compute contribution points — accepted work, not payment',
  evals: 'What the eval harness recorded, and how much of it is chance',
  invite: 'Machines that do the work, and apps that ask for it',
  config: 'What this coordinator loaded when it started, and what each setting does',
};

function showTab(name, opts) {
  if (TABS.indexOf(name) === -1) name = 'overview';
  currentTab = name;
  // Two of the four banners are scoped to a view, so the banner has to be
  // re-evaluated on every switch rather than only when health is polled.
  renderStatus();
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
  const main = $('main');
  if (main) main.classList.toggle('is-bleed', name === 'overview');
  const title = $('view-title');
  if (title) title.textContent = TAB_TITLES[name] || name;
  setText('view-sub', TAB_SUBS[name] || '');
  // A deep link should survive a refresh; without this, "Runs" is unshareable.
  if (!opts || opts.pushHash !== false) {
    try { history.replaceState(null, '', '#' + name); } catch (e) {}
  }
  if (isDrawer()) closeDrawer();

  if (name === 'gallery') loadGallery();
  if (name === 'guild') loadStandings();
  if (name === 'runs') loadHistory();
  if (name === 'evals') loadEvals();
  if (name === 'invite') loadInvite();
  if (name === 'config') loadConfig();
  // Evidence is only fetched while this view is open, so opening it has to ask
  // rather than waiting up to 3s for the next tick to notice.
  if (name === 'nodes') refresh();
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

/* ── The status model, wired to the page ──────────────────────────
   One poller. It replaces four indicators that each derived their own truth
   — the fail-open banner, the persistence-degraded banner, the locked
   screen's health line and the rail's inference line — and could disagree
   with one another. The rail's line was deleted rather than moved.

   The state machine itself is in _status_model.js, which has no DOM, no
   timers and no fetch, so tests/test_status_model.py can run the design's
   six-transport rig against it. Everything below is the page half. */

const statusState = STATUS_MODEL.create();
const STATUS_POLL_MS = STATUS_MODEL.CONSTANTS.POLL_SEC * 1000;
const OPERATOR_POLL_MS = STATUS_MODEL.CONSTANTS.OPERATOR_POLL_SEC * 1000;

/* Last-known values for the four polled cells, and whether each one came from
   a poll that answered. Values are held rather than blanked when the
   coordinator goes quiet: the number stays readable and the hollow lamp says
   it is not current. Blanking would lose information; leaving it filled would
   lie about its age. */
const statusText = {inference: null, nodes: null, running: null, queued: null};
const statusFresh = {inference: false, nodes: false, running: false, queued: false};

/* The four states that earn words. Copy is the design's, verbatim.
   `scope` is the tab this banner is worth interrupting; null means every view.
   `suppress` is where a more actionable version of the same warning lives.
   It applies only once that view has drawn its own version: a Config view
   that failed to load carries no warning, and hiding this one there would
   leave the page saying nothing at all. */
const BANNER_SUPPRESSED_ON = ['config'];
const BANNERS = {
  gate: {
    tone: 'is-danger',
    scope: null,
    suppress: BANNER_SUPPRESSED_ON,
    head: 'Anyone who can reach this address can read runs, machines and projects',
    body: 'No viewer key is set, so the gate is letting every request through. Set ' +
          'viewer_key and restart before this host is reachable by anyone but you.',
  },
  commit: {
    tone: 'is-danger',
    scope: null,
    head: 'Finished work cannot be written down right now',
    body: 'A run counts once it is recorded, so nothing will be published until this ' +
          'clears. Anything already recorded is unaffected, and pitches will be ' +
          'refused rather than lost.',
  },
  inference: {
    /* warn, not danger: nothing can be built, but every past run stays
       readable. Scoped to the view you pitch from. */
    tone: 'is-warn',
    scope: 'overview',
    head: 'Ollama is not reachable — nothing can be built right now',
    body: 'Start it with ollama serve and this clears on its own. Connected machines ' +
          'stay registered and pick work up again by themselves.',
  },
  silent: {
    tone: 'is-quiet',
    scope: null,
    head: 'No answer from the coordinator',
    body: 'Every count on screen is the last one it gave. Nothing here is current, ' +
          'and an empty queue on this screen is not a claim that the swarm is idle.',
  },
};

/* A poll that never returns is silence, not a pending answer. Without this a
   hung socket would hold the last state on screen indefinitely — the exact
   defect this model exists to prevent, arriving by a different route. */
function fetchWithTimeout(url, ms) {
  let ctl = null;
  try { ctl = new AbortController(); } catch (e) {}
  const opts = {credentials: 'same-origin'};
  if (ctl) opts.signal = ctl.signal;
  const timer = setTimeout(() => { if (ctl) ctl.abort(); }, ms);
  return fetch(url, opts).finally(() => clearTimeout(timer));
}

/* One poll of /health, every POLL_SEC.

   /health is public, so it is fetched plainly rather than through apiFetch:
   this request cannot 401, and a 401 from anywhere else must never reach the
   model. The coordinator refusing *this browser* is not the coordinator being
   unwell. */
async function pollStatus() {
  const now = Date.now();
  let health = null;
  try {
    const resp = await fetchWithTimeout('/health', STATUS_POLL_MS);
    if (!resp.ok) {
      // It answered, and refused. link is a probe here, so one 502 from a
      // restarting proxy holds rather than flipping the whole bar.
      STATUS_MODEL.pollAnswered(statusState, {ok: false}, now);
    } else {
      health = await resp.json();
      STATUS_MODEL.pollAnswered(statusState, {
        ok: true,
        inference: health.ollama === 'connected',
        // Only an explicit false is a statement. A server too old to carry the
        // field has not told us the gate is open, and we must not say it did.
        gate: health.private_routes_protected !== false,
      }, now);
    }
  } catch (e) {
    STATUS_MODEL.pollMissed(statusState, now);
  }

  if (health) {
    /* The INFERENCE word is set in renderStatus from the derived facet, not
       from this payload. One probe saying no is not an outage, and writing
       "offline" here would put that word on screen beside a lamp still saying
       not-heard-back — the cell contradicting its own lamp. */
    statusText.nodes = String(health.nodes_online);
    statusFresh.inference = true;
    statusFresh.nodes = true;

    /* The subtask queue, under a label that says so. The archived handoff had
       Overview read "running now" and "queued" off /health: it has no running
       count at all, and its tasks_pending is this number rather than the job
       queue. Ported as written, one screen would have carried QUEUED over two
       different numbers. RUNNING and QUEUED are the status bar's, from
       /metrics, and there is exactly one of each. */

  } else {
    statusFresh.inference = false;
    statusFresh.nodes = false;
  }

  /* RUNNING and QUEUED are jobs, from /metrics. /health.tasks_pending is the
     subtask queue — a different number, and showing it under these labels
     would be wrong. It has its own tile on Overview, labelled as itself.

     /metrics is private, so a 401 here is this browser's session rather than
     the coordinator's health: it means these two cells have no current value,
     and it touches nothing else. */
  try {
    const met = await apiJson('/metrics');
    statusText.running = String(met.jobs_running);
    statusText.queued = String(met.jobs_queued);
    statusFresh.running = true;
    statusFresh.queued = true;
    setText('stat-done', met.tasks_completed_total);
    const latEl = $('stat-latency');
    if (latEl) latEl.textContent = met.avg_task_latency_seconds != null
      ? met.avg_task_latency_seconds + 's' : '—';
    // Name whose balance this is — anyone can open this dashboard.
    const hostEl = $('credit-host');
    if (hostEl && met.orchestrator_id) {
      hostEl.textContent = met.orchestrator_id;
      identName(met.orchestrator_id);
    }
    const credEl = $('credit-value');
    if (credEl) credEl.textContent = met.orchestrator_credits ?? 0;
    setText('ident-points', met.orchestrator_credits ?? 0);
  } catch (e) {
    statusFresh.running = false;
    statusFresh.queued = false;
  }

  renderStatus();
}

/* mode and lock, every OPERATOR_POLL_SEC. Neither can change while the process
   runs — the lock is held for its life, so a running console can never observe
   it lost — which is why both cells carry no lamp. The poll exists to fetch
   them once and to pick them up again after a reconnect, not to claim they are
   fresh. The two-coordinator case arrives here as a preflight warning. */
async function pollOperator() {
  try {
    const d = await apiJson('/v1/operator/health');
    setText('cell-mode-v', d.deployment_mode || '—');
    setText('cell-lock-v', d.single_coordinator_lock ? 'held' : 'not held');
    setText('cell-mode-why', d.deployment_mode === 'trusted_alpha'
      ? 'Startup refused to run on any preflight error, so every check held when this process started.'
      : 'The compatibility default: preflight warns instead of refusing to start.');
    setText('cell-lock-why', d.single_coordinator_lock
      ? 'This process holds its state directory, so a second coordinator aimed at it fails at once.'
      : 'This process has not finished starting.');
    const warnings = d.preflight_warnings || [];
    const lockCell = $('cell-lock-v');
    if (lockCell) {
      lockCell.title = warnings.length
        ? warnings.join(' · ')
        : 'Held for the life of this process';
    }
  } catch (e) {
    // Leave both cells as they are. They carry no lamp, so they are making no
    // claim about now, and a failed fetch is not news about a constant.
  }
}

function setText(id, value) {
  const el = $(id);
  if (el) el.textContent = String(value);
}

/* Filled square for a fresh answer, hollow for none — colour only says which
   answer. That is what makes the third value survive greyscale, video
   compression and a reader who does not separate these hues. */
function toneClass(state) {
  if (state === 'good') return 'is-ok';
  if (state === 'bad') return 'is-bad';
  // A recovering facet answered, so its lamp fills. It is warn rather than
  // green because one yes is not two.
  if (state === 'warn' || state === 'recovering') return 'is-warn';
  return 'is-unknown';
}

function paintLamp(el, tone) {
  if (!el) return;
  const big = el.classList.contains('is-lg');
  el.className = 'lamp' + (big ? ' is-lg' : '') + ' ' + toneClass(tone);
}

function paintWord(el, word, tone, base) {
  if (!el) return;
  el.className = base + ' ' + toneClass(tone);
  el.textContent = word;
}

/* One polled cell. `state` is the facet's value once the dependency rule has
   been applied; `text` is the last value the coordinator gave, or null if it
   never gave one.

   A value that has stopped being current is held and greyed rather than
   blanked. Blanking would throw away the last thing the coordinator said,
   which is exactly what the silence banner then promises is still on screen —
   and an empty queue cell reads as "the swarm is idle", which is a claim
   nobody is in a position to make. The hollow lamp and the age carry the
   "not now" instead. */
function paintCell(cellId, valueId, state, text) {
  const cell = $(cellId);
  if (!cell) return;
  const known = text !== null && text !== undefined;
  const shown = known ? state : 'unknown';
  paintLamp(cell.querySelector('.lamp'), shown);
  cell.classList.toggle('is-stale', shown === 'unknown');
  cell.classList.toggle('is-bad', shown === 'bad');
  setText(valueId, known ? text : '—');
}

function renderBanner(d) {
  const el = $('banner');
  if (!el) return;
  const spec = d.banner ? BANNERS[d.banner] : null;
  const suppressed = spec && spec.suppress && spec.suppress.indexOf(currentTab) !== -1
    && !!document.querySelector('#' + currentTab + '-body [data-gate-open]');
  const outOfScope = spec && spec.scope && spec.scope !== currentTab;
  if (!spec || suppressed || outOfScope) {
    el.hidden = true;
    return;
  }
  el.className = 'banner ' + spec.tone;
  setText('banner-head', d.banner === 'silent'
    ? spec.head + ' for ' + d.bannerAge
    : spec.head);
  setText('banner-body', spec.body);
  el.hidden = false;
}

/* Derive once, paint everywhere. Called on every poll and once a second in
   between, because the age has to keep moving even when nothing answers —
   a still age is how a dead coordinator reads as a live one. */
function renderStatus() {
  const d = STATUS_MODEL.derive(statusState, Date.now());

  paintLamp($('statusbar-lamp'), d.tone);
  paintWord($('statusbar-word'), d.severity, d.tone, 'statusbar-word');
  paintLamp($('statuspill-lamp'), d.tone);
  paintWord($('statuspill-word'), d.severity, d.tone, 'statuspill-word');

  const ageEl = $('statusbar-age');
  if (ageEl) ageEl.classList.toggle('is-stale', d.ageStale);
  setText('statusbar-age-text', d.ageText);
  const pillAge = $('statuspill-age');
  if (pillAge) {
    pillAge.textContent = d.ageText;
    pillAge.classList.toggle('is-stale', d.ageStale);
  }

  /* `ready` and `offline` are the confirmed states, so the word follows the
     facet rather than the last payload. While the facet is unknown the last
     confirmed word is held and greyed, exactly like the counts. */
  if (d.values.inference === 'good' || d.values.inference === 'recovering') {
    statusText.inference = 'ready';
  } else if (d.values.inference === 'bad') {
    statusText.inference = 'offline';
  }

  /* NODES, RUNNING and QUEUED are counts the answer carried rather than facets
     of their own, so their freshness is the link's. `statusFresh` narrows that
     for the two that come from /metrics: a 401 there means those two are not
     current even while /health is answering. */
  const served = d.answering ? (d.recovering.link ? 'recovering' : 'good') : 'unknown';
  const cellState = (key, state) => (statusFresh[key] ? state : 'unknown');
  paintCell('cell-inference', 'cell-inference-v',
            cellState('inference', d.values.inference), statusText.inference);
  paintCell('cell-nodes', 'cell-nodes-v',
            cellState('nodes', served), statusText.nodes);
  paintCell('cell-running', 'cell-running-v',
            cellState('running', served), statusText.running);
  paintCell('cell-queued', 'cell-queued-v',
            cellState('queued', served), statusText.queued);

  /* The Overview tile and the locked screen read the same derivation rather
     than deriving again, so neither can disagree with the bar. */
  const tile = $('stat-status');
  if (tile) {
    const state = d.values.inference;
    const word = state === 'bad' ? 'unavailable'
               : state === 'unknown' ? 'no answer'
               : state === 'recovering' ? 'answering again'
               : 'connected';
    const cls = state === 'bad' ? 'is-down'
              : state === 'unknown' ? 'is-unknown'
              : state === 'recovering' ? 'is-unknown'
              : 'is-ok';
    tile.className = 'stat-status ' + cls;
    tile.innerHTML = '<i aria-hidden="true"></i>' + escHtml(word);
  }

  paintVital('vital-nodes', cellState('nodes', served), statusText.nodes,
             Number(statusText.nodes) > 0 ? 'ok' : 'idle');
  paintVital('vital-inference', cellState('inference', d.values.inference), statusText.inference,
             d.values.inference === 'bad' ? 'bad' : 'ok', 'vital-inference-v');
  paintVital('vital-running', cellState('running', served), statusText.running,
             Number(statusText.running) > 0 ? 'warn' : 'idle', 'vital-running-v');
  paintVital('vital-queued', cellState('queued', served), statusText.queued,
             Number(statusText.queued) > 0 ? 'warn' : 'idle', 'vital-queued-v');

  const cfgDot = $('nav-config-dot');
  if (cfgDot) cfgDot.hidden = d.values.gate !== 'bad';

  const lockedLamp = $('locked-lamp');
  if (lockedLamp) paintLamp(lockedLamp, d.tone);
  const lockedLine = $('locked-health-line');
  if (lockedLine) lockedLine.textContent = d.severity + ' · ' + d.ageText;

  renderBanner(d);
}

/* One vital on Overview's line. `state` is the cell's freshness, exactly as
   the bar's cell gets it; `tone` is what a fresh value means. A stale value
   stays readable and stops claiming to be now, like the bar's. */
function paintVital(id, state, text, tone, valueId) {
  const el = $(id);
  if (!el) return;
  const known = text != null && text !== '';
  el.className = 'ov-vital is-' + (state === 'unknown' || !known ? 'stale' : tone);
  const v = valueId ? $(valueId) : el.querySelector('.ov-vital-v');
  if (v) v.textContent = known ? text : '—';
}

/* Collapsed, the bar is the pill: same derived state, nothing new. */
function toggleStatusBar() {
  const bar = $('statusbar');
  if (!bar) return;
  const collapsed = bar.classList.toggle('is-collapsed');
  try { localStorage.setItem('mycelium-statusbar', collapsed ? 'collapsed' : 'open'); } catch (e) {}
  const pill = $('statuspill'), chev = $('statusbar-collapse');
  if (pill) pill.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  if (chev) chev.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  if (collapsed) setStatusDrawer(false);
}

function setStatusDrawer(open) {
  const drawer = $('statusbar-drawer');
  if (!drawer) return;
  drawer.hidden = !open;
  const age = $('statusbar-age');
  age.setAttribute('aria-expanded', open ? 'true' : 'false');
  age.title = open ? 'Hide what is behind these values' : 'Where these values come from';
  setText('statusbar-chev', open ? '\u2304' : '\u2303');
}

// ── Nodes polling ────────────────────────────────────────────────
async function refresh() {
  try {
    const [nodes, evidence] = await Promise.all([
      apiJson('/nodes'),
      // Optional: a deployment with evidence off, or an older server, simply
      // has no observations to show. That is a state the row renders, not an
      // error that should empty the view.
      maybeLoadEvidence(),
    ]);
    if (evidence) capabilityEvidence = evidence;

    const navWrap = $('nav-node-wrap');
    if (navWrap) {
      // An empty count reads as a rendering bug. Show it only once there is
      // a count to put in it, lit only when a machine is connected.
      const n = (nodes.nodes || []).length;
      setText('nav-node-count', n);
      navWrap.hidden = n === 0;
      navWrap.classList.toggle('is-ok', n > 0);
      const lamp = $('nav-node-lamp');
      if (lamp) lamp.classList.toggle('is-ok', n > 0);
    }

    renderNodes(nodes);
    paintSwarmPanel(nodes);
  } catch (e) {
    // Deliberately silent. This used to paint "offline" on the shell from
    // here, which meant a request this browser was not authorised to make —
    // or a single dropped packet — reported the coordinator as down. Whether
    // the coordinator answered is the status model's question, and it asks it
    // of /health, which is public and cannot 401.
  }
}

/* Node state: a filled square, a colour AND a word. Never colour alone — this
   view is filmed, compressed, and read by people who do not all separate these
   hues. */
function nodeState(n) {
  if (n.enrollment_status === 'revoked') return {key: 'revoked', word: 'revoked'};
  if (n.current_task) return {key: 'building', word: 'building'};
  return {key: 'idle', word: 'idle — holding nothing'};
}

/* Enrollment is durable identity; a session is not. A compatibility session
   records work against nothing that survives a reconnect, which is a fact the
   operator needs, so it is stated rather than left blank. */
function enrollmentLine(n) {
  if (n.enrollment_status === 'revoked') {
    return {cls: 'is-revoked', label: escHtml(n.enrollment_id || 'revoked'),
            note: 'revoked · offered no further work'};
  }
  if (!n.enrolled || !n.enrollment_id) {
    return {cls: 'is-warn', label: 'not enrolled',
            note: 'compatibility session · work recorded against nothing durable'};
  }
  return {cls: 'is-ok', label: escHtml(n.enrollment_id),
          note: 'durable identity · survives reconnect and relabelling'};
}

/* Capability evidence, from /v1/operator/capability-evidence. Deliberately not
   from /nodes: that endpoint excludes observation records on purpose, so
   reading a sample count off a node record would always render "none" and read
   as "this machine has produced nothing" rather than "this endpoint does not
   carry it".

   The endpoint states `affects_routing: false` and calls agreement
   `bounded_output_comparison_not_correctness`. Both are repeated in the row,
   because a sample count with neither caveat reads as a score. */
let capabilityEvidence = null;
let _evidenceLoadedAt = 0;

/* The evidence endpoint aggregates per scope and computes shadow diagnostics,
   which is real work. refresh() runs every 3 seconds and only the Nodes view
   shows any of it, so this fetches at most every 30 seconds and only while
   that view is open. Everywhere else the last answer is reused, and before the
   first one the row says "not loaded" rather than "none". */
const EVIDENCE_MAX_AGE_MS = 30000;
let _evidenceInFlight = null;

function maybeLoadEvidence() {
  if (currentTab !== 'nodes') return Promise.resolve(null);
  if (capabilityEvidence && Date.now() - _evidenceLoadedAt < EVIDENCE_MAX_AGE_MS) {
    return Promise.resolve(null);
  }
  // Opening the view calls refresh() directly while the 3s interval is also
  // running, so two ticks can arrive before either has an answer to cache.
  // Sharing the in-flight promise is what actually makes this one request;
  // a freshness check alone still let the first few through.
  if (_evidenceInFlight) return _evidenceInFlight;
  _evidenceInFlight = apiJson('/v1/operator/capability-evidence')
    .then(d => { _evidenceLoadedAt = Date.now(); return d; })
    .catch(() => null)
    .finally(() => { _evidenceInFlight = null; });
  return _evidenceInFlight;
}

function evidenceLine(n) {
  const ev = capabilityEvidence;
  if (!ev) {
    return `<div class="node-evidence is-none">observations
      <span class="node-evidence-v is-absent">not loaded</span></div>`;
  }
  if (ev.mode === 'off') {
    return `<div class="node-evidence is-none">observations
      <span class="node-evidence-v">collection is off</span>
      <span class="node-evidence-note">nothing is being recorded about this machine's
        behaviour</span></div>`;
  }

  const minimum = Number(ev.minimum_samples || 0);
  const scopes = (ev.scopes || []).filter(s =>
    (n.enrollment_id && s.enrollment_id === n.enrollment_id) ||
    (!n.enrollment_id && s.node_label === n.node_id));
  const samples = scopes.reduce((sum, s) => sum + Number(s.observation_count || 0), 0);

  // Below the configured minimum is an explicit state, not a small number and
  // not an empty cell. A rate computed from two samples is not a finding.
  if (samples < minimum || samples === 0) {
    return `<div class="node-evidence is-none">observations
      <span class="node-evidence-v">${samples} of ${minimum} — insufficient evidence</span>
      <span class="node-evidence-note">shadow only; never affects which machine gets
        work</span></div>`;
  }
  return `<div class="node-evidence">observations
    <span class="node-evidence-v">${samples} sample${samples === 1 ? '' : 's'}</span>
    <span class="node-evidence-note">shadow only; never affects which machine gets work,
      and agreement between two runs is not correctness</span></div>`;
}

function renderNodes(nodes) {
  const nodesList = $('nodes-list');
  const summary = $('nodes-summary');
  if (!nodesList) return;

  renderNodeMap(nodes);

  if (nodes.count === 0) {
    if (summary) summary.textContent = 'No machines connected.';
    nodesList.innerHTML = `
      <div class="empty-state">
        <div class="icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="5" r="2.2"/><circle cx="5" cy="18" r="2.2"/><circle cx="19" cy="18" r="2.2"/><path d="M10.5 6.8 6.5 15.8M13.5 6.8l4 9M7.4 18h9.2"/></svg></div>
        <p>No machines connected yet.<br><button class="link-btn" type="button" data-tab="invite">How a machine joins →</button></p>
      </div>`;
    return;
  }

  const list = nodes.nodes || [];
  if (summary) {
    summary.textContent = list.length === 1
      ? '1 machine connected'
      : `${list.length} machines connected`;
  }

  /* Rows, not an eight-column table. There are 2-5 of these and each is an
     entity you read individually — whose machine, what is it building, what is
     its identity. The live-status line needs room to be a sentence, which no
     column width allows. */
  nodesList.innerHTML = list.map(n => {
    const st = nodeState(n);
    const enr = enrollmentLine(n);

    const spec = [
      n.model, n.platform && n.machine ? `${n.platform} / ${n.machine}` : n.platform,
      n.cpu_count ? `${n.cpu_count} CPU` : null,
      n.ram_gb ? `${n.ram_gb} GB` : null,
      n.gpu && n.gpu !== 'none' ? n.gpu : 'no GPU',
    ].filter(Boolean).map(escHtml).join(' · ');

    const live = n.current_task
      ? `Building ${escHtml(n.current_task)} now.`
      : 'Offering compute and holding nothing. It is handed a subtask when one is ready and its dependencies are met.';

    // Served by /nodes; the descriptor body itself deliberately is not.
    const descriptor = n.capability_descriptor_hash
      ? `<div class="node-kv"><span class="node-k">capability descriptor</span>
           <span class="node-v mono">v${escHtml(n.capability_descriptor_version || '1')}
             · ${escHtml(String(n.capability_descriptor_hash).slice(0, 16))}</span></div>`
      : `<div class="node-kv"><span class="node-k">capability descriptor</span>
           <span class="node-v is-absent">not reported by this machine</span></div>`;

    const claims = (n.claimed_capabilities || n.capabilities || [])
      .filter(c => !String(c).startsWith('model:'));
    const claimsHtml = claims.length
      ? claims.map(c => `<span class="chip">${escHtml(c)}</span>`).join('')
      : '<span class="node-v is-absent">none declared</span>';

    return `
      <article class="node-row" id="nodecard-${escHtml(n.node_id)}">
        <div class="node-row-main">
          <div class="node-ident">
            <span class="node-square is-${st.key}" aria-hidden="true"></span>
            <span class="node-id mono">${escHtml(n.node_id)}</span>
            <span class="node-state is-${st.key}">${escHtml(st.word)}</span>
          </div>
          <div class="node-spec mono">${spec}</div>
          <div class="node-live">${live}</div>
          <div class="node-kv"><span class="node-k">enrollment</span>
            <span class="node-v ${enr.cls} mono">${enr.label}</span>
            <span class="node-note">${escHtml(enr.note)}</span></div>
          ${descriptor}
          <div class="node-kv"><span class="node-k">claims</span>
            <span class="node-v">${claimsHtml}</span></div>
          ${evidenceLine(n)}
        </div>
        <div class="node-row-figures">
          <div class="node-fig"><span class="node-fig-n mono">${escHtml(n.lifetime_tasks_completed ?? n.tasks_completed ?? 0)}</span>
            <span class="node-fig-k">TASKS · LIFETIME</span></div>
          <div class="node-fig"><span class="node-fig-n mono">${escHtml(n.lifetime_contribution_points ?? n.credits_earned ?? 0)}</span>
            <span class="node-fig-k">POINTS · LIFETIME</span></div>
          <div class="node-fig-actions">
            <button type="button" class="btn-quiet is-sm"
                    data-node="${escHtml(JSON.stringify(n))}">Details</button>
            <a class="node-fig-link" href="/node/${encodeURIComponent(n.node_id)}">machine page →</a>
          </div>
        </div>
      </article>`;
  }).join('');
}

/* The map. Deterministic grid: gx is the pipeline column, gy the lane. Sorted
   by node_id so the same fleet lays out identically on every open — a view that
   settles differently each time is wrong for something being recorded.

   It draws machines and no task lines. Per-unit node assignment is not served
   (handoff §8.2): `observed_placements` and per-unit `depends_on` exist, but
   nothing says which machine holds which unit, so an edge here would be an
   invented assignment. The note under the map says that rather than drawing a
   plausible-looking lie. */
function renderNodeMap(nodes) {
  const stage = $('nodemap-stage');
  const meta = $('nodemap-meta');
  const note = $('nodemap-note');
  const legend = $('nodemap-legend');
  if (!stage) return;

  const list = (nodes.nodes || []).slice().sort((a, b) =>
    String(a.node_id).localeCompare(String(b.node_id)));

  if (meta) {
    meta.textContent = list.length
      ? `live · ${list.length} machine${list.length === 1 ? '' : 's'}`
      : 'live · nothing connected';
  }

  if (!list.length) {
    stage.innerHTML = '<div class="nodemap-empty">No machines are connected, so there is no flow to draw. '
      + 'The coordinator still runs work on itself.</div>';
    if (legend) legend.innerHTML = '';
    if (note) note.textContent = '';
    return;
  }

  const LANE = 46, TOP = 34, COORD_X = 62, WORKER_X = 300;
  const height = Math.max(140, TOP + list.length * LANE + 24);
  const parts = [];

  // Coordinator at the edge, not the centre. Planning and review are its work;
  // the hardware doing the building belongs to other people.
  parts.push(`<line class="nodemap-spine" x1="${COORD_X}" y1="${TOP}" x2="${COORD_X}" y2="${height - 30}"></line>`);
  parts.push(`<rect class="nodemap-square is-coordinator" x="${COORD_X - 6}" y="${TOP - 6}" width="12" height="12"></rect>`);
  parts.push(`<text class="nodemap-label-text" x="${COORD_X + 14}" y="${TOP + 4}">this machine · plans and reviews</text>`);

  list.forEach((n, i) => {
    const y = TOP + (i + 1) * LANE;
    const st = nodeState(n);
    // A hairline from the coordinator column to each machine: the coordinator
    // brokers every handoff, so this is "connected to the coordinator" and not
    // a task edge. Machines never connect to each other directly.
    parts.push(`<line class="nodemap-link" x1="${COORD_X}" y1="${y}" x2="${WORKER_X - 10}" y2="${y}"></line>`);
    parts.push(`<rect class="nodemap-square is-${st.key}" x="${WORKER_X - 6}" y="${y - 6}" width="12" height="12"></rect>`);
    parts.push(`<text class="nodemap-label-text" x="${WORKER_X + 14}" y="${y + 4}">${escHtml(n.node_id)} · ${escHtml(st.word)}</text>`);
  });

  stage.innerHTML =
    `<svg viewBox="0 0 720 ${height}" role="img" preserveAspectRatio="xMinYMin meet"
          aria-label="${escHtml(list.length)} machines connected to this coordinator">
       ${parts.join('\n')}
     </svg>`;

  if (legend) {
    legend.innerHTML = [
      ['is-coordinator', 'coordinator'],
      ['is-building', 'building'],
      ['is-idle', 'holding nothing'],
      ['is-revoked', 'revoked'],
    ].map(([cls, label]) =>
      `<span class="nodemap-key"><i class="nodemap-swatch ${cls}" aria-hidden="true"></i>${label}</span>`
    ).join('');
  }

  if (note) {
    note.textContent = 'Lines run to the coordinator because it brokers every handoff — '
      + 'machines never connect to each other directly. No line is drawn between machines '
      + 'for a task: which machine holds which unit of work is not served by the API, and '
      + 'drawing it would mean inventing the assignment.';
  }
}

// ── The thread ───────────────────────────────────────────────────
/* A pitch is a turn: the ask, then the agent's reply led by the step
   timeline (HANDOFF-UI §16.2). The timeline's durations are measured from
   the coordinator's own event timestamps, not from an animation clock, so
   they are the real time each stage took. A step marker is round because it
   reports progress; every status lamp in the console is square, and the two
   must not be confused. */
const STRATEGY_WORDS = {
  auto: 'auto', dag: 'plan & split', ensemble: 'several tries', direct: 'one shot',
};
let pitchStrategy = 'auto';
const turns = {};

function fmtDur(ms) {
  if (ms == null || ms < 0) return '—';
  const secs = Math.round(ms / 1000);
  if (secs < 60) return `${secs}s`;
  return `${Math.floor(secs / 60)}m ${String(secs % 60).padStart(2, '0')}s`;
}

function turnSteps(t, now) {
  // A pitch the coordinator never accepted has no stages at all: say that,
  // rather than drawing a run that attempted anything.
  if (t.status === 'failed' && !t.jobId) {
    return [
      {label: 'PLAN', state: 'stopped', detail: 'the coordinator did not accept this pitch', dur: '—'},
      {label: 'BUILD', state: 'waiting', detail: 'did not run', dur: '—'},
      {label: 'REVIEW', state: 'waiting', detail: 'did not run', dur: '—'},
    ];
  }
  const done = t.status === 'complete' || t.status === 'failed';
  const planned = t.planAt != null;
  const steps = [];
  // PLAN. Only the planning strategy decomposes; a finished run with no plan
  // event attempted the whole task, and says so rather than drawing a step
  // that never happened.
  if (planned) {
    steps.push({label: 'PLAN', state: 'done',
      detail: `${t.subtasks} ${t.subtasks === 1 ? 'subtask' : 'subtasks'}`,
      dur: fmtDur(t.planAt - t.startedAt)});
  } else if (done) {
    steps.push({label: 'PLAN', state: 'skipped',
      detail: 'no plan — this run attempted the whole task', dur: '—'});
  } else {
    steps.push({label: 'PLAN', state: 'active', detail: 'the planner is working',
      dur: fmtDur(now - t.startedAt)});
  }
  // BUILD.
  const buildLabel = t.subtasks ? `BUILD ${Math.min(t.builds, t.subtasks)}/${t.subtasks}` : 'BUILD';
  if (t.reviewAt != null || (done && t.status === 'complete')) {
    const end = t.reviewAt != null ? t.reviewAt : t.doneAt;
    steps.push({label: buildLabel, state: 'done',
      detail: t.subtasks ? `${Math.min(t.builds, t.subtasks)} of ${t.subtasks} built` : 'built',
      dur: planned ? fmtDur(end - t.planAt) : '—'});
  } else if (planned && !done) {
    steps.push({label: buildLabel, state: 'active',
      detail: t.lastBuilt ? `last built: ${t.lastBuilt}` : 'waiting on the first subtask',
      dur: fmtDur(now - t.planAt)});
  } else {
    steps.push({label: buildLabel, state: done ? 'stopped' : 'waiting',
      detail: done ? 'stopped before building' : 'waiting for the plan', dur: '—'});
  }
  // REVIEW.
  if (t.reviewAt != null && done) {
    steps.push({label: 'REVIEW', state: t.status === 'failed' ? 'stopped' : 'done',
      detail: t.status === 'failed' ? 'stopped during review' : 'assembled and graded',
      dur: fmtDur(t.doneAt - t.reviewAt)});
  } else if (t.reviewAt != null) {
    steps.push({label: 'REVIEW', state: 'active', detail: 'assembling and grading',
      dur: fmtDur(now - t.reviewAt)});
  } else {
    steps.push({label: 'REVIEW', state: done ? (t.status === 'failed' ? 'stopped' : 'done') : 'waiting',
      detail: done ? (t.status === 'failed' ? 'did not run' : 'graded') : 'waiting for builders', dur: '—'});
  }
  return steps;
}

function stepsHtml(steps) {
  return steps.map((st, i) => `
    <li class="step is-${st.state}${i === steps.length - 1 ? ' is-last' : ''}">
      <span class="step-rail" aria-hidden="true"><i class="step-mark"></i><i class="step-spine"></i></span>
      <span class="step-body">
        <span class="step-label">${escHtml(st.label)}</span>
        <span class="step-detail">${escHtml(st.detail)}</span>
      </span>
      <span class="step-dur">${escHtml(st.dur)}</span>
    </li>`).join('');
}

function paintTurn(pid) {
  const t = turns[pid];
  if (!t) return;
  const now = Date.now();
  const stepsEl = $(`pstages-${pid}`);
  if (stepsEl) stepsEl.innerHTML = stepsHtml(turnSteps(t, now));
  const meta = $(`pmeta-${pid}`);
  if (meta) {
    const parts = [];
    if (t.jobId) parts.push(t.jobId.replace(/^job_/, 'job ').slice(0, 16));
    parts.push(STRATEGY_WORDS[t.strategy] || t.strategy);
    const end = t.doneAt != null ? t.doneAt : now;
    parts.push(t.doneAt != null ? fmtDur(end - t.startedAt) : `${fmtDur(end - t.startedAt)} elapsed`);
    meta.textContent = parts.join(' · ');
  }
  paintRunPanel();
}

function turnFromEvent(ev) {
  // Events carry no pitch id, so they belong to the one turn still open on
  // this page. Two concurrent pitches from one page would share the stream;
  // the timeline then shows the newest, and the run page has each exactly.
  const open = Object.keys(turns).map(Number).filter(k => turns[k].doneAt == null).sort((a, b) => b - a);
  return open.length ? turns[open[0]] : null;
}

function noteEventForTurn(ev) {
  const t = turnFromEvent(ev);
  if (!t) return;
  const at = ev.time ? Date.parse(ev.time) : Date.now();
  if (ev.type === 'plan' && t.planAt == null) {
    t.planAt = at;
    t.subtasks = Number.isInteger(ev.subtask_count)
      ? ev.subtask_count : (Array.isArray(ev.subtasks) ? ev.subtasks.length : 0);
  } else if (ev.type === 'build') {
    t.builds += 1;
    if (ev.subtask) t.lastBuilt = ev.subtask;
  } else if (ev.type === 'review_start' && t.reviewAt == null) {
    t.reviewAt = at;
  }
  paintTurn(t.pid);
}

function setStrategy(key) {
  pitchStrategy = key;
  document.querySelectorAll('.ov-strategy [data-strategy]').forEach(b =>
    b.setAttribute('aria-checked', b.dataset.strategy === key ? 'true' : 'false'));
  paintStrategyNote();
}

/* Only the planning strategy can carry a project; ensemble and direct reject
   one outright (execution/service.py). So choosing either detaches the
   project in front of you, and says so, instead of letting the pair 422
   after you press Pitch. */
function paintStrategyNote() {
  const note = $('ov-strategy-note');
  const project = $('active-project-id').value;
  const drops = !!project && (pitchStrategy === 'ensemble' || pitchStrategy === 'direct');
  note.hidden = !drops;
  if (drops) {
    note.textContent = `${STRATEGY_WORDS[pitchStrategy][0].toUpperCase()}${STRATEGY_WORDS[pitchStrategy].slice(1)} `
      + `cannot carry a project. This pitch runs detached from ${$('project-context-name').textContent}: `
      + 'nothing in its memory is read, and the result is not filed as one of its iterations.';
  }
}

async function pitchTask() {
  const input = $('pitch-input');
  const btn = $('pitch-btn');
  const task = input.value.trim();
  if (!task) return;
  const projectId = $('active-project-id').value || null;
  const detached = !!projectId && (pitchStrategy === 'ensemble' || pitchStrategy === 'direct');
  const projectName = projectId && !detached ? $('project-context-name').textContent : null;

  btn.disabled = true;
  const pid = Date.now();
  turns[pid] = {pid, task, strategy: pitchStrategy, startedAt: pid, planAt: null, subtasks: 0,
                builds: 0, reviewAt: null, doneAt: null, status: 'running', jobId: null,
                lastBuilt: null, projectName};

  const askMeta = [projectName ? `into ${projectName}` : (detached ? 'detached · no project' : 'no project'),
                   STRATEGY_WORDS[pitchStrategy], 'pitched just now'].join(' · ');
  $('ov-empty').hidden = true;
  $('pipeline-log').insertAdjacentHTML('beforeend', `
    <article class="turn" id="pipeline-${pid}">
      <div class="turn-ask"><div class="turn-bubble">
        <div class="turn-text">${escHtml(task)}</div>
        <div class="turn-meta">${escHtml(askMeta)}</div>
      </div></div>
      <div class="turn-reply">
        <span class="turn-mark" aria-hidden="true">&#9672;</span>
        <div class="turn-body">
          <div class="turn-verdict">
            <span class="turn-state is-running" id="pstatus-${pid}">RUNNING</span>
            <span class="turn-runmeta" id="pmeta-${pid}"></span>
          </div>
          <ol class="steps" id="pstages-${pid}" aria-label="Where this run is"></ol>
          <pre class="token-stream" id="token-stream-${pid}" hidden></pre>
          <div class="turn-result" id="presult-${pid}" hidden></div>
        </div>
      </div>
    </article>`);
  paintTurn(pid);
  $(`pipeline-${pid}`).scrollIntoView({block: 'nearest'});
  showOvTab('run');

  const ticker = setInterval(() => {
    if (!turns[pid] || turns[pid].doneAt != null) { clearInterval(ticker); return; }
    paintTurn(pid);
  }, 1000);

  try {
    const body = {task, strategy: pitchStrategy};
    if (projectId && !detached) body.project_id = projectId;
    const resp = await apiFetch('/pitch/async', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok || !data.job_id) {
      throw new Error(typeof data.detail === 'string' ? data.detail : `the coordinator answered ${resp.status}`);
    }
    turns[pid].jobId = data.job_id;
    // Re-key the token stream element so _appendToken can find it by job_id.
    const tsEl = $(`token-stream-${pid}`);
    if (tsEl) tsEl.id = `token-stream-${data.job_id}`;
    paintTurn(pid);
    input.value = '';
    await _pollJobCompletion(data.job_id, pid);
  } catch (e) {
    finishTurn(pid, {status: 'failed', error: e && e.message ? e.message : 'the pitch was not accepted'});
  }
  clearInterval(ticker);
  btn.disabled = false;
}

async function _pollJobCompletion(jobId, pid) {
  while (true) {
    await new Promise(r => setTimeout(r, 3000));
    try {
      const job = await apiJson(`/jobs/${jobId}`);
      if (job.status === 'complete' || job.status === 'failed') {
        finishTurn(pid, {
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

function finishTurn(pid, result) {
  const t = turns[pid];
  if (!t) return;
  t.status = result.status === 'failed' ? 'failed' : 'complete';
  t.doneAt = Date.now();
  if (!t.subtasks && (result.plan || []).length) t.subtasks = result.plan.length;
  paintTurn(pid);

  const state = $(`pstatus-${pid}`);
  if (state) {
    if (t.status === 'failed') {
      state.className = 'turn-state is-failed';
      state.textContent = 'FAILED';
    } else {
      // The verdict is the run's own chip: lifecycle and checks are separate
      // claims, and "completed" is not "passed" (HANDOFF-UI §7).
      state.outerHTML = verdictChip(result.rating, result.code_precheck_error);
    }
  }
  const ts = result.project_dir ? result.project_dir.split(/[\\/]/).pop() : '';
  const out = $(`presult-${pid}`);
  if (out) {
    const plan = (result.plan || []).map(st => `
      <span class="turn-file" title="${escHtml(st.title || '')}">${escHtml(String(st.id))}
        <span class="turn-file-k">${escHtml(st.title || '')}</span></span>`).join('');
    out.innerHTML = `
      ${result.error ? `<div class="error-box">${escHtml(result.error)}</div>` : ''}
      ${plan ? `<div class="turn-files">${plan}</div>` : ''}
      <div class="turn-links">
        ${ts ? `<button type="button" class="ov-link" data-run="${escHtml(ts)}" title="Open the full run detail">run detail &rarr;</button>
                <a class="ov-link" href="/run/${encodeURIComponent(ts)}" title="The run's own page, the link to share">run page &#8599;</a>` : ''}
        <button type="button" class="ov-link" data-template="${escHtml(t.task)}" title="Put this task back in the composer">pitch again</button>
      </div>`;
    out.hidden = false;
  }
  paintRunPanel();
}

/* The RUN panel: the newest turn while it runs, and the last one after. The
   tab is absent when there is nothing to show (§15.5b). */
function paintRunPanel() {
  const keys = Object.keys(turns).map(Number).sort((a, b) => b - a);
  const tab = document.querySelector('.ov-tab[data-ovtab="run"]');
  const el = $('ov-run-panel');
  if (!keys.length) { if (tab) tab.hidden = true; return; }
  if (tab) tab.hidden = false;
  const t = turns[keys[0]];
  const now = Date.now();
  const running = t.doneAt == null;
  el.innerHTML = `
    <div class="ov-run-head">
      <span class="ov-run-k">${running ? 'ACTIVE RUN' : 'LAST RUN'}</span>
      ${running ? '<span class="ov-run-live"><i aria-hidden="true"></i>running</span>' : ''}
      <span class="ov-run-elapsed">${escHtml(fmtDur((t.doneAt || now) - t.startedAt))}</span>
    </div>
    <div class="ov-run-task">${escHtml(t.task)}</div>
    <div class="ov-run-meta">${escHtml([t.jobId || 'not accepted yet', STRATEGY_WORDS[t.strategy]].join(' · '))}</div>
    <ol class="steps is-compact">${stepsHtml(turnSteps(t, now))}</ol>
    <button type="button" class="ov-link ov-link-row" data-scroll-turn="${t.pid}"
            title="Scroll the thread to this run">show in thread &rarr;</button>`;
  paintOvWindowMeta();
}

// ── The floating window ─────────────────────────────────────────
let ovTabPref = 'activity';
function availableOvTabs() {
  return [...document.querySelectorAll('.ov-tab')].filter(b => !b.hidden).map(b => b.dataset.ovtab);
}
function showOvTab(name) {
  ovTabPref = name;
  paintOvTabs();
}
/* The choice is a preference resolved against what is available, so a panel
   that disappears (a tab hidden because it has nothing to show) falls back to
   ACTIVITY instead of leaving the window blank. */
function paintOvTabs() {
  const avail = availableOvTabs();
  const active = avail.indexOf(ovTabPref) !== -1 ? ovTabPref : (avail[0] || 'activity');
  document.querySelectorAll('.ov-tab').forEach(b =>
    b.setAttribute('aria-selected', b.dataset.ovtab === active ? 'true' : 'false'));
  document.querySelectorAll('.ov-panel').forEach(p => { p.hidden = p.dataset.ovpanel !== active; });
  $('ov-window').classList.toggle('is-activity', active === 'activity');
  paintOvWindowMeta();
}
function paintOvWindowMeta() {
  const active = (document.querySelector('.ov-tab[aria-selected="true"]') || {}).dataset?.ovtab;
  let meta = '';
  if (active === 'activity') meta = wsConnected ? 'websocket · live' : 'reconnecting';
  else if (active === 'swarm') meta = $('ov-swarm-summary').textContent;
  else if (active === 'run') meta = '';
  setText('ov-window-meta', meta);
}
function toggleOvWindow() {
  const w = $('ov-window');
  const min = w.classList.toggle('is-min');
  const b = $('ov-window-min');
  b.setAttribute('aria-expanded', min ? 'false' : 'true');
  b.title = min ? 'Show the window again' : 'Shrink the window to its title bar';
  b.textContent = min ? '+' : '\u2013';
}

/* The SWARM panel: the same /nodes answer the Nodes view renders. */
function paintSwarmPanel(nodes) {
  const list = nodes.nodes || [];
  const building = list.filter(n => n.status === 'busy' || n.current_task).length;
  setText('ov-swarm-summary', list.length
    ? `${list.length} online · ${building} building`
    : 'none connected');
  $('ov-swarm-list').innerHTML = list.length ? list.map(n => {
    const busy = n.status === 'busy' || n.current_task;
    return `
      <button type="button" class="ov-swarm-row" data-tab="nodes" title="Open Nodes — ${escHtml(n.node_id)}">
        <span class="ov-swarm-top"><i class="lamp ${busy ? 'is-warn' : 'is-ok'}" aria-hidden="true"></i>
          <span class="ov-swarm-id">${escHtml(n.node_id)}</span>
          <span class="ov-swarm-pts">${escHtml(String(n.credits_earned || 0))} pts</span></span>
        <span class="ov-swarm-spec">${escHtml([n.platform, n.cpu_count ? n.cpu_count + ' CPU' : '', n.ram_gb ? n.ram_gb + ' GB' : ''].filter(Boolean).join(' · '))}</span>
        <span class="ov-swarm-task${busy ? ' is-busy' : ''}">${busy ? escHtml('▶ ' + (n.current_task || 'building')) : 'waiting for work'}</span>
      </button>`;
  }).join('') : '<p class="ov-swarm-none">No machines connected. The coordinator still works: builders run locally, one at a time.</p>';
  paintOvWindowMeta();
}


let _currentModalTimestamp = null;

/* Run detail in the console.

   The modal used to build its own layout out of the pieces: a plan list, a
   file-chip row and the review blob. That layout and templates/run.html were
   two drawings of one structure, and they had already drifted -- the modal
   showed no defects and no not-checked state, and neither showed the three
   axes at all.

   Now the server builds the structure once in run_detail.py and both surfaces
   render the same markup. This function fetches it and puts it in the panel.
   Everything the console shows about a run -- the verdict, the three axes read
   separately, the waves from depends_on, the manifest, the envelope sentence
   -- comes down in that fragment, so there is nothing here that can disagree
   with the run page.

   The fragment is escaped where it is built, and it arrives from this
   origin's own viewer-gated route. */
async function viewRun(timestamp) {
  const panel = $('modal-run-detail');
  try {
    _currentModalTimestamp = timestamp;
    const data = await apiJson(`/history/${encodeURIComponent(timestamp)}`);

    // The dialog's accessible name. Visually hidden -- the surface below
    // states the title, and a dialog that says it twice is the parallel
    // layout this port exists to remove.
    $('modal-title').textContent = data.task || 'Run detail';

    // An empty fragment means the server could not build it. That costs the
    // panel and nothing else -- /history/{timestamp} still answers, because
    // it has other consumers -- so the modal says so rather than opening blank.
    panel.innerHTML = data.detail_html || (
      '<div class="empty-state"><p>This run’s detail could not be built.<br>'
      + 'The run itself is still there; open its page for the server-rendered view.</p></div>'
    );
    openModal('output-modal');
  } catch (e) {
    console.error('Failed to load run:', e);
    panel.innerHTML = '<div class="empty-state"><p>This run could not be loaded.<br>'
      + 'It may have been pruned, or the coordinator may be unreachable.</p></div>';
    openModal('output-modal');
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
    row('Points', n.credits_earned || 0),
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
    _setNodeIdle(ev.node_id);
    if (ev.credits_earned > 0) loadStandings();
    return;
  }
  if (ev.type === 'node_blacklisted') { _setNodeBlacklisted(ev.node_id, ev.blacklist_seconds); return; }

  const label = EVENT_LABELS[ev.type];
  if (!label) return;  // skip unknown events
  noteEventForTurn(ev);
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

  // Stacked, not a three-column grid: at 316px the grid wrapped every
  // message (§15.5b). Agent and time on one mono line, the message beneath.
  const log = $('event-log');
  const empty = $('event-empty');
  if (empty) empty.remove();
  log.insertAdjacentHTML('beforeend', `<div class="log-entry">
    <div class="log-head"><span class="log-agent ${label.cls}">${label.agent}</span>
      <span class="log-time">${t}</span></div>
    <div class="log-event">${msg}</div>
  </div>`);
  const pane = log.closest('.ov-window-body') || log;
  pane.scrollTop = pane.scrollHeight;
}

/* Live state on a row. The square, its colour and the word move together —
   there is no state that is colour only. The glow pulse and the credit-pop
   flash are deliberately gone: a 22px animated halo and a number flying off a
   row are decoration, and on a view whose job is reporting state they compete
   with the state itself. */
function _setNodeSquare(nodeId, stateKey, word) {
  const row = $(`nodecard-${nodeId}`);
  if (!row) return null;
  const square = row.querySelector('.node-square');
  const label = row.querySelector('.node-state');
  if (square) square.className = `node-square is-${stateKey}`;
  if (label) { label.className = `node-state is-${stateKey}`; label.textContent = word; }
  return row;
}

function _setNodeBusy(nodeId, taskTitle) {
  const row = _setNodeSquare(nodeId, 'building', 'building');
  if (!row) return;
  const live = row.querySelector('.node-live');
  if (live) live.textContent = `Building ${taskTitle} now.`;
}

function _setNodeIdle(nodeId) {
  const row = _setNodeSquare(nodeId, 'idle', 'idle — holding nothing');
  if (!row) return;
  const live = row.querySelector('.node-live');
  if (live) {
    live.textContent = 'Offering compute and holding nothing. It is handed a subtask '
      + 'when one is ready and its dependencies are met.';
  }
}

function _setNodeBlacklisted(nodeId, seconds) {
  const row = _setNodeSquare(nodeId, 'down', `no work offered for ${seconds}s`);
  if (!row) return;
  const live = row.querySelector('.node-live');
  if (live) {
    // The live line is a sentence for exactly this reason: a column could not
    // hold it, and "CIRCUIT OPEN" alone tells the operator nothing actionable.
    live.textContent = `Repeated failures — no work is being offered to this machine for `
      + `${seconds}s, then it is tried again on its own.`;
  }
  setTimeout(() => _setNodeIdle(nodeId), seconds * 1000);
}

function connectWebSocket() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws/events`);
  ws.onopen = () => { wsConnected = true; paintOvWindowMeta(); };
  ws.onmessage = (e) => {
    try {
      const ev = JSON.parse(e.data);
      appendEvent(ev);
      if (ev.id && ev.id > eventCursor) eventCursor = ev.id;
    } catch (_) {}
  };
  ws.onclose = (e) => {
    wsConnected = false;
    paintOvWindowMeta();
    // 4401 is the server saying this browser has no viewer credential. It will
    // say the same thing every time, so reconnecting is a loop that never ends
    // and never reports anything. Show the locked screen and stop.
    if (e && e.code === 4401) { showLockedScreen(); return; }
    if (viewerLocked) return;
    setTimeout(connectWebSocket, 3000);
  };
  ws.onerror = () => ws.close();
}

// Polling fallback — only used if WebSocket never connects
async function pollEvents() {
  if (wsConnected) return;
  try {
    const data = await apiJson(`/events?since=${eventCursor}`);
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
    const data = await apiJson(url);
    const el = $('history-list');
    // /history returns at most 50, so a full page is a lower bound, not a total.
    if (!q) {
      const navRuns = $('nav-runs-count');
      if (navRuns) {
        navRuns.textContent = data.count >= 50 ? '50+' : String(data.count);
        navRuns.hidden = data.count === 0;
      }
    }

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
          ${verdictChip(r.rating, r.code_precheck_error)}
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
    const data = await apiJson('/standings');
    const el = $('standings-list');

    if (!data.standings.length) {
      el.innerHTML = '<div class="empty-state"><p>No contributions yet.<br>Points are recorded when a machine builds, reviews or pitches.</p></div>';
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

// ── Evals ────────────────────────────────────────────────────────
/* The server builds the whole view in evals_view.py and this puts it in the
   panel, the same arrangement run detail uses. Nothing here formats a number,
   so nothing here can print the pass rate the view exists not to print.

   Asked for when the view opens and never on a timer: the record is committed
   files, and it does not change while someone is reading it. */
async function loadEvals() {
  const el = $('evals-body');
  try {
    const data = await apiJson('/evals');
    el.innerHTML = data.evals_html || (
      '<div class="empty-state"><p>The eval record was read but its view could not be built.</p></div>'
    );
  } catch (e) {
    if (e instanceof ViewerLocked) return;
    el.innerHTML = '<div class="empty-state"><p>The eval record could not be loaded.<br>'
      + 'The coordinator may be unreachable.</p></div>';
  }
}

// ── Config ───────────────────────────────────────────────────────
/* Built in config_view.py, the same arrangement as Evals. The route reduces
   every credential to a word before the fragment exists, so nothing here
   handles a value that could be one.

   Asked for when the view opens and never on a timer: configuration is read
   once, at startup, and does not change until the process restarts. The
   banner is re-evaluated afterwards, because whether the global warning is
   suppressed here depends on this fragment having drawn its own. */
async function loadConfig() {
  const el = $('config-body');
  try {
    const data = await apiJson('/v1/operator/config');
    el.innerHTML = data.config_html || (
      '<div class="empty-state"><p>The settings were read but their view could not be built.</p></div>'
    );
  } catch (e) {
    if (e instanceof ViewerLocked) return;
    el.innerHTML = '<div class="empty-state"><p>This coordinator\'s settings could not be loaded.<br>'
      + 'The coordinator may be unreachable.</p></div>';
  }
  renderStatus();
}

// ── Connect ──────────────────────────────────────────────────────
/* Built in invite_view.py, the same arrangement as Config: the route reads
   this coordinator's settings for the warning panel and reduces every key to
   a word before the fragment exists. Asked for when the view opens. */
async function loadInvite() {
  const el = $('invite-body');
  try {
    const data = await apiJson('/v1/operator/invite');
    el.innerHTML = data.invite_html || (
      '<div class="empty-state"><p>The settings were read but this view could not be built.</p></div>'
    );
  } catch (e) {
    if (e instanceof ViewerLocked) return;
    el.innerHTML = '<div class="empty-state"><p>How this coordinator is set up could not be loaded.<br>'
      + 'The coordinator may be unreachable.</p></div>';
  }
}

function copyCommand(text) {
  const done = () => showToast('Copied');
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => prompt('Copy this:', text));
  } else {
    prompt('Copy this:', text);   // non-HTTPS contexts have no clipboard API
  }
}

// ── Projects ─────────────────────────────────────────────────────
async function loadProjects() {
  try {
    const data = await apiJson('/projects');
    const el = $('projects-list');
    const navProjects = $('nav-project-count');
    if (navProjects) {
      const n = (data.projects || []).length;
      navProjects.textContent = n;
      navProjects.hidden = n === 0;
    }

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
    const resp = await apiFetch('/projects', {
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
  $('ov-project-dot').classList.add('is-on');
  setText('ov-memory-note', `${projectId} · its memory goes into the next pitch`);
  $('clear-project').hidden = false;
  showTab('overview');
  $('pitch-input').placeholder = `Pitch the next change to ${projectName}`;
  paintStrategyNote();
  $('pitch-input').focus();
}

function clearProjectContext() {
  $('active-project-id').value = '';
  $('project-context-name').textContent = 'No project';
  $('ov-project-dot').classList.remove('is-on');
  setText('ov-memory-note', 'no memory carried in');
  $('clear-project').hidden = true;
  $('pitch-input').placeholder = 'Describe what you want built';
  paintStrategyNote();
}

// ── Gallery ──────────────────────────────────────────────────────
async function loadGallery() {
  try {
    const data = await apiJson('/gallery');
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
            ${distBadge(c.mode)}${verdictChip(c.rating, c.code_precheck_error)}
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
  el.innerHTML = _TEMPLATES.map(t =>
    `<button type="button" class="template-chip" data-template="${escHtml(t)}"
             title="Put this suggestion in the composer">${escHtml(t)}</button>`
  ).join('');
}

// ── Download / share ─────────────────────────────────────────────
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
                             '[data-fork], [data-share], [data-continue-project], [data-copy]');
  if (!t) return;

  if (t.hasAttribute('data-copy')) { copyCommand(t.dataset.copy); return; }

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
});

// Clicking the backdrop closes a dialog — but only the backdrop itself.
['output-modal', 'node-modal'].forEach(id => {
  $(id).addEventListener('click', (e) => { if (e.target === $(id)) closeModal(id); });
});

$('statuspill').addEventListener('click', toggleStatusBar);
$('statusbar-collapse').addEventListener('click', toggleStatusBar);

$('nav-toggle').addEventListener('click', toggleNav);
$('rail-toggle').addEventListener('click', toggleNav);
document.querySelectorAll('.ov-strategy [data-strategy]').forEach(b =>
  b.addEventListener('click', () => setStrategy(b.dataset.strategy)));
document.querySelectorAll('.ov-tab').forEach(b =>
  b.addEventListener('click', () => showOvTab(b.dataset.ovtab)));
$('ov-window-min').addEventListener('click', toggleOvWindow);
document.addEventListener('click', (e) => {
  const run = e.target.closest('[data-run]');
  if (run) { viewRun(run.dataset.run); return; }
  const turn = e.target.closest('[data-scroll-turn]');
  if (turn) {
    const el = $(`pipeline-${turn.dataset.scrollTurn}`);
    if (el) el.scrollIntoView({block: 'start', behavior: 'smooth'});
  }
});
$('ident-toggle').addEventListener('click', () => setIdentMenu($('ident-menu').hidden));
$('ident-scrim').addEventListener('click', () => setIdentMenu(false));
// Every item navigates and closes in one step, so none leaves the menu open
// behind a view change (§18.3).
$('ident-menu').addEventListener('click', (e) => {
  if (e.target.closest('.ident-item, .global-link')) setIdentMenu(false);
});
$('statusbar-age').addEventListener('click', () => setStatusDrawer($('statusbar-drawer').hidden));
$('palette-open').addEventListener('click', openPalette);
$('palette').addEventListener('click', (e) => {
  const cmd = e.target.closest('[data-palette]');
  if (cmd) { runPalette(Number(cmd.dataset.palette)); return; }
  if (e.target === $('palette')) closePalette();
});
$('palette-input').addEventListener('input', () => { _paletteActive = 0; renderPalette(); });
$('palette-input').addEventListener('keydown', (e) => {
  const n = paletteMatches().length;
  if (e.key === 'ArrowDown') { e.preventDefault(); _paletteActive = Math.min(n - 1, _paletteActive + 1); renderPalette(); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); _paletteActive = Math.max(0, _paletteActive - 1); renderPalette(); }
  else if (e.key === 'Enter') { e.preventDefault(); runPalette(_paletteActive); }
});
$('theme-toggle').addEventListener('click', toggleTheme);
$('focus-pitch').addEventListener('click', focusPitch);
$('pitch-btn').addEventListener('click', pitchTask);
$('clear-project').addEventListener('click', clearProjectContext);
$('new-project').addEventListener('click', promptNewProject);
$('modal-share-btn').addEventListener('click', copyShareLink);

$('pitch-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) pitchTask();
});

$('history-search').addEventListener('input', () => {
  clearTimeout(_historySearchTimer);
  _historySearchTimer = setTimeout(loadHistory, 250);
});

document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
    e.preventDefault();
    if ($('palette').hidden) openPalette(); else closePalette();
    return;
  }
  if (e.key !== 'Escape') return;
  if (!$('palette').hidden) { closePalette(); return; }
  if (!$('ident-menu').hidden) { setIdentMenu(false); $('ident-toggle').focus(); return; }
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
wireLockedScreen();
$('main').classList.add('is-bleed');
paintRunPanel();
paintOvTabs();

// Restore the view from the URL. #run=<ts> opens a run; #gallery selects a
// view. Before this, reloading on #gallery silently landed on Overview.
(function restoreFromHash() {
  const runMatch = (location.hash || '').match(/#run=([^&]+)/);
  if (runMatch) { viewRun(decodeURIComponent(runMatch[1])); return; }
  const name = (location.hash || '').slice(1);
  if (TABS.indexOf(name) !== -1) showTab(name, {pushHash: false});
})();

/* The bar opens in whichever form it was left in. */
try {
  if (localStorage.getItem('mycelium-statusbar') === 'collapsed') toggleStatusBar();
} catch (e) {}

/* Paint before the first poll rather than after it. On load the state is
   not-heard-back, never ok — there is nothing to be confident about before
   the coordinator has answered once, and a lamp that starts green is a lamp
   that has already lied. */
renderStatus();

connectWebSocket();
pollStatus();
pollOperator();
refresh();
loadHistory();
loadStandings();
loadProjects();

setInterval(pollStatus, STATUS_POLL_MS);
setInterval(pollOperator, OPERATOR_POLL_MS);
/* The age has to keep moving between polls. A still age on a dead coordinator
   is how the old dashboard read as a live one, and it is also what makes the
   15s and 60s thresholds fire when a request hangs rather than fails: no miss
   is ever recorded for a poll that simply never comes back. */
setInterval(renderStatus, 1000);
setInterval(refresh, 3000);
setInterval(pollEvents, 3000);   // fallback only — no-ops when WS is connected
setInterval(loadHistory, 15000);
setInterval(loadStandings, 10000);
setInterval(loadProjects, 20000);
