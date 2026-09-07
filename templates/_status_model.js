/* One status model for the whole console — injected into pages by dashboard.py
   at their STATUS_MODEL_JS marker comment.

   Ported from docs/design/status-model-2026-09-07/ §01. Every constant, facet
   name, transition and severity word below is that file's, not a judgement
   made here.

   THE DEFECT THIS EXISTS TO PREVENT
   The dashboard used to paint its indicator from the last successful poll and
   never expire it, so a coordinator that died five minutes ago still reported
   as connected. Green is therefore never a cached value here: a filled lamp
   requires a fresh confirmation, and the age of that confirmation is on screen
   at all times.

   FOUR RULES THAT ARE EASY TO BREAK BY ACCIDENT
   1. The dependency rule. When `link` leaves `up`, every facet the coordinator
      serves becomes `unknown` — immediately, unconditionally, and never `bad`.
      A silent coordinator cannot tell you inference is down; saying it is
      would be inventing the answer. Only `link: up` plus an explicit negative
      is a confirmed bad.
   2. On load the state is not-heard-back, never ok. There is nothing to be
      confident about before the first answer.
   3. A probe confirms twice; a stated fact is believed once. `ollama` is the
      coordinator's own 5s probe of Ollama, so one no is not an outage. A 503
      on a write and `private_routes_protected: false` are statements about
      configuration and need no second opinion.
   4. One good poll after a bad or unknown one reads `recovering`, never green.
      A flapping backend must not flash.

   This file is deliberately free of the DOM, of timers, and of fetch. It takes
   observations in and hands derived state back, so tests/test_status_model.py
   can run the design's six-transport rig against it directly. Everything that
   touches a page lives in _dashboard.js or in the page's own script. */

'use strict';

var STATUS_MODEL = (function () {
  'use strict';

  /* Shipped intervals and dwell, in the units the design states them in. */
  var POLL_SEC = 5;        // /health
  var OPERATOR_POLL_SEC = 30;  // /v1/operator/health
  var CONFIRM_BAD = 2;     // polls of a probe saying no
  var CONFIRM_GOOD = 2;    // polls of yes before green returns
  var MISS_LIMIT = 2;      // polls with no answer before hollow
  var SILENT_SEC = 15;     // or this long since the last answer
  var BANNER_SEC = 60;     // silence that earns words

  var FACETS = ['link', 'inference', 'commit', 'gate'];
  /* Facets the coordinator serves — the ones the dependency rule governs.
     `link` is the poll itself and is nobody's dependent. */
  var SERVED = ['inference', 'commit', 'gate'];

  function facet() {
    /* `unknown`, not `good`. Rule 2. */
    return {v: 'unknown', good: 0, bad: 0, miss: 0, recovering: false};
  }

  function create() {
    return {
      facets: {link: facet(), inference: facet(), commit: facet(), gate: facet()},
      /* Wall-clock ms of the last poll the coordinator answered. `null` until
         it answers once — which is what makes the initial age honest. */
      lastAnswerAt: null,
      startedAt: null,
      /* A write refused with execution_persistence_unavailable latches here.
         Nothing but a write can clear it: only a write can tell you whether
         the coordinator can write. */
      commitRefused: false,
      polls: 0,
    };
  }

  function begin(state, nowMs) {
    if (state.startedAt === null) state.startedAt = nowMs;
  }

  /* Seconds since the coordinator last answered — or since this page loaded,
     if it never has. Never null, so no caller has to special-case the state
     the console spends its first five seconds in. */
  function silentSeconds(state, nowMs) {
    var since = state.lastAnswerAt !== null ? state.lastAnswerAt
              : (state.startedAt !== null ? state.startedAt : nowMs);
    return Math.max(0, (nowMs - since) / 1000);
  }

  /* One facet, one observation.

     verdict:
       'good'      the poll said yes
       'probe_no'  a probe said no — needs CONFIRM_BAD before it is believed
       'stated_no' the payload said no — believed on this poll
       'miss'      no answer at all */
  function step(state, key, verdict, nowMs) {
    var f = state.facets[key];
    f.recovering = false;

    if (verdict === 'miss') {
      f.miss += 1;
      f.good = 0;
      f.bad = 0;
      /* Two consecutive misses, or SILENT_SEC since the last answer, whichever
         comes first. The second half also lives in derive(), because a poll
         that hangs forever produces no misses to count. */
      if (f.miss >= MISS_LIMIT || silentSeconds(state, nowMs) >= SILENT_SEC) {
        f.v = 'unknown';
      }
      return;
    }

    f.miss = 0;

    if (verdict === 'good') {
      f.bad = 0;
      f.good += 1;
      if (f.v !== 'good') {
        if (f.good >= CONFIRM_GOOD) f.v = 'good';
        else f.recovering = true;   // rule 4
      }
      return;
    }

    f.good = 0;
    f.bad += 1;
    if (verdict === 'stated_no') {
      f.v = 'bad';                  // rule 3: no second opinion needed
      return;
    }
    if (f.bad >= CONFIRM_BAD) f.v = 'bad';
    /* else: hold. A probe has a 5s timeout and one no is not an outage. */
  }

  /* The coordinator answered.

     reading:
       {ok: false}                      it answered, and refused — a status code
       {ok: true, inference: bool,      a health payload
                  gate: bool}

     `inference` is /health.ollama === 'connected'; `gate` is
     /health.private_routes_protected. `commit` is not in any payload — it is
     driven by writes, through writeRefused()/writeAccepted() below. */
  function pollAnswered(state, reading, nowMs) {
    begin(state, nowMs);
    state.polls += 1;

    if (reading && reading.ok === false) {
      /* It answered and refused. link is a probe here, so a single 502 from a
         restarting proxy holds rather than flipping. The refusal carried no
         payload, so every served facet takes a miss — the dependency rule
         applied at the source, not only at the point of rendering. */
      step(state, 'link', 'probe_no', nowMs);
      SERVED.forEach(function (k) { step(state, k, 'miss', nowMs); });
      return;
    }

    step(state, 'link', 'good', nowMs);
    state.lastAnswerAt = nowMs;
    step(state, 'inference', reading.inference === false ? 'probe_no' : 'good', nowMs);
    step(state, 'commit', state.commitRefused ? 'stated_no' : 'good', nowMs);
    step(state, 'gate', reading.gate === false ? 'stated_no' : 'good', nowMs);
  }

  /* No answer at all — a timeout, a network error, a closed port. Every facet
     takes the miss, including the three the coordinator serves. This is the
     dependency rule: a silent coordinator reports on nothing. */
  function pollMissed(state, nowMs) {
    begin(state, nowMs);
    state.polls += 1;
    FACETS.forEach(function (k) { step(state, k, 'miss', nowMs); });
  }

  /* A write answered 503 execution_persistence_unavailable. Stated, so it is
     believed on this observation rather than the next poll. */
  function writeRefused(state, nowMs) {
    begin(state, nowMs);
    state.commitRefused = true;
    step(state, 'commit', 'stated_no', nowMs);
  }

  /* A write was accepted, which is the only thing that can prove the
     coordinator is writing again. Clearing the latch does not turn the lamp
     green: the next two polls do that, one of them reading `recovering`. */
  function writeAccepted(state) {
    state.commitRefused = false;
  }

  function fmtAge(seconds) {
    var s = Math.floor(seconds);
    if (s < 60) return s + 's';
    var m = Math.floor(s / 60);
    return m + 'm ' + (s % 60) + 's';
  }

  /* facets -> one severity, one lamp, one age, at most one banner.

     Pure: it never mutates state, so a caller may derive as often as it likes
     — which is how the age on screen keeps moving between polls. */
  function derive(state, nowMs) {
    var f = state.facets;
    var silent = silentSeconds(state, nowMs);

    /* A facet that answered yes once after a bad or unknown one is neither.
       It is no longer asserting the old value — it answered, and it said yes
       — but one yes is not two, so it is not green either. `recovering` is a
       value of its own here rather than a flag beside a stale one, because a
       flag beside a stale one is invisible: the severity ordering would read
       the stale `bad` or `unknown` first and `recovering` would never show.

       The design's own §03 rig has that bug — its severity() reads f.v and
       never reaches its own `recovering` branch. Its tables are unambiguous
       that the state is meant to be seen ("one good poll after a bad one
       reads recovering, never green"), so the tables win. */
    function raw(key) {
      var x = f[key];
      return x.recovering ? 'recovering' : x.v;
    }

    /* Silence expires link even when no miss was ever recorded, which is the
       case a poll that hangs produces. Whichever comes first. */
    var linkValue = (silent >= SILENT_SEC) ? 'unknown' : raw('link');

    /* Did the coordinator answer the most recent poll? `recovering` counts:
       it answered, which is the whole question the dependency rule asks. A
       link on its way back up has not left `up`; a silent or refusing one
       has. */
    var answering = (linkValue === 'good' || linkValue === 'recovering');
    var linkUp = linkValue === 'good';

    /* The dependency rule at the point of reading. Never `bad`. */
    function valueOf(key) {
      if (key === 'link') return linkValue;
      return answering ? raw(key) : 'unknown';
    }

    var values = {};
    var recovering = {};
    FACETS.forEach(function (k) {
      values[k] = valueOf(k);
      recovering[k] = values[k] === 'recovering';
    });

    var anyUnknown = FACETS.some(function (k) { return values[k] === 'unknown'; });
    var anyRecovering = FACETS.some(function (k) { return recovering[k]; });

    /* Worst facet wins, and the word is the condition's own name. "degraded"
       is never shown; it says nothing. `unprotected` is ranked first because
       the console looks perfectly fine in that state. */
    var severity, tone;
    if (values.gate === 'bad') { severity = 'unprotected'; tone = 'bad'; }
    else if (values.commit === 'bad') { severity = 'not committing'; tone = 'bad'; }
    /* warn, not danger: every agent calls the local model so nothing can
       start, but past runs stay readable. */
    else if (values.inference === 'bad') { severity = 'inference offline'; tone = 'warn'; }
    else if (anyUnknown) { severity = 'no answer'; tone = 'unknown'; }
    else if (anyRecovering) { severity = 'recovering'; tone = 'warn'; }
    else { severity = 'ok'; tone = 'ok'; }

    /* A banner is spent, not decoration: it appears only when the reader's
       next action will fail, or when something is true they cannot infer from
       a lamp. Four states earn words; recovering, ok and a single dropped
       poll do not. */
    var banner = null;
    if (values.gate === 'bad') banner = 'gate';
    else if (values.commit === 'bad') banner = 'commit';
    else if (values.inference === 'bad') banner = 'inference';
    else if (values.link === 'unknown' && silent >= BANNER_SEC) banner = 'silent';

    return {
      severity: severity,
      tone: tone,
      values: values,
      recovering: recovering,
      /* `answering` is what a cell's freshness hangs on; `linkUp` is the
         narrower "confirmed good". A count read off a recovering poll is
         fresh — it came back this second — so its lamp fills. */
      answering: answering,
      linkUp: linkUp,
      heard: state.lastAnswerAt !== null,
      silentSeconds: silent,
      /* Always rendered. This is the cell the old dashboard had no equivalent
         of, and its absence is what let a five-minute-old answer read as now. */
      ageText: (answering ? 'as of ' : 'no answer for ') + fmtAge(silent),
      ageStale: silent >= 2 * POLL_SEC,
      banner: banner,
      bannerAge: fmtAge(silent),
    };
  }

  return {
    CONSTANTS: {
      POLL_SEC: POLL_SEC,
      OPERATOR_POLL_SEC: OPERATOR_POLL_SEC,
      CONFIRM_BAD: CONFIRM_BAD,
      CONFIRM_GOOD: CONFIRM_GOOD,
      MISS_LIMIT: MISS_LIMIT,
      SILENT_SEC: SILENT_SEC,
      BANNER_SEC: BANNER_SEC,
    },
    FACETS: FACETS,
    SERVED: SERVED,
    create: create,
    pollAnswered: pollAnswered,
    pollMissed: pollMissed,
    writeRefused: writeRefused,
    writeAccepted: writeAccepted,
    derive: derive,
    fmtAge: fmtAge,
  };
})();

/* So the rig in tests/test_status_model.py can require this file directly.
   There is no build step and no bundler; in a browser this block does not
   run and STATUS_MODEL is a plain global, exactly as _dashboard.js expects. */
if (typeof module !== 'undefined' && module.exports) module.exports = STATUS_MODEL;
