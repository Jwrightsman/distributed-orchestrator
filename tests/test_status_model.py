"""The status model's state machine, driven the way the design drives it.

`docs/design/status-model-2026-09-07/Mycelium Status System.dc.html` §03 is a
live rig: six transports, each one a different way for a coordinator to behave,
run against the same state machine. This file is that rig as a test. The six
transports below are the design's own, verbatim:

    healthy   answers every poll
    dead      answers nothing, ever — `null` on every poll
    flapping  alternates: no answer, answer, no answer, answer
    offline   answers, and says Ollama is unavailable
    nocommit  a write was refused with execution_persistence_unavailable
    failopen  answers, and says private_routes_protected is false

The defect this exists to catch is specific. The dashboard used to paint from
the last successful poll and never expire it, so a coordinator that died five
minutes ago still reported as connected. Every assertion here is about that:
green is never a cached value, silence is never mistaken for health, and a
silent coordinator is never made to say something it did not say.

WHY THIS SHELLS OUT TO NODE
The state machine ships to browsers, so it is JavaScript. Reimplementing it in
Python to test it would be testing a different program. `templates/_status_model.js`
is deliberately free of the DOM, of timers and of fetch so it can be required
directly — the browser gets the same file with the same numbers in it.

WHY EVERY RULE IS ALSO CHECKED AGAINST A SYNTHETIC VIOLATION
A rule asserted only against a correct implementation can pass because the
scenario never reached the code it was meant to police.
`test_every_rule_rejects_a_deliberately_broken_model` re-runs each rule against
a copy of the model with that rule removed, and fails if the rule still passes.
That is the same discipline test_console_language.py applies to its regexes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MODEL_JS = ROOT / "templates" / "_status_model.js"
NODE = shutil.which("node")

# The rig compresses the wall clock exactly as the design's does: one step is
# one poll, and the clock is supplied rather than waited for. The thresholds
# are real; only the waiting is skipped.


# Locally, a missing Node skips this file. In CI that would be a green suite
# proving nothing, so the guard against it lives in tests/test_status_bar.py —
# deliberately *outside* this module, because a module-level skipif applies to
# every test in its module, and a guard sitting under the condition it guards
# against is not a guard. That was found after the guard had already shipped
# here and CI had already gone green past it.
pytestmark = pytest.mark.skipif(
    NODE is None, reason="node is not installed; the status model rig needs it"
)


# ── The harness ──────────────────────────────────────────────────────────

_DRIVER = r"""
const MODEL_PATH = process.env.MYCELIUM_STATUS_MODEL;
const PLAN = JSON.parse(process.env.MYCELIUM_STATUS_PLAN);
const M = require(MODEL_PATH);
const POLL_MS = M.CONSTANTS.POLL_SEC * 1000;

/* Wall-clock time is supplied, never read: a test that waits fifteen real
   seconds to check a fifteen-second threshold is a test nobody runs. */
let now = 1000000;
const state = M.create();
const frames = [];

function snapshot(label) {
  const d = M.derive(state, now);
  frames.push({
    label: label,
    t: (now - 1000000) / 1000,
    severity: d.severity,
    tone: d.tone,
    values: d.values,
    recovering: d.recovering,
    linkUp: d.linkUp,
    heard: d.heard,
    ageText: d.ageText,
    ageStale: d.ageStale,
    silentSeconds: d.silentSeconds,
    banner: d.banner,
  });
}

snapshot('load');
for (const stepDef of PLAN) {
  const kind = stepDef.do;
  if (kind === 'advance') {
    now += stepDef.seconds * 1000;
  } else {
    now += (stepDef.gap === undefined ? M.CONSTANTS.POLL_SEC : stepDef.gap) * 1000;
    if (kind === 'miss') M.pollMissed(state, now);
    else if (kind === 'refuse') M.pollAnswered(state, {ok: false}, now);
    else if (kind === 'answer') {
      M.pollAnswered(state, {
        ok: true,
        inference: stepDef.inference !== false,
        gate: stepDef.gate !== false,
      }, now);
    } else if (kind === 'writeRefused') M.writeRefused(state, now);
    else if (kind === 'writeAccepted') M.writeAccepted(state);
    else throw new Error('unknown step ' + kind);
  }
  snapshot(stepDef.label || kind);
}
process.stdout.write(JSON.stringify(frames));
"""


def run(plan, model_source: str | None = None, tmp_path: Path | None = None):
    """Drive the model through `plan` and return one frame per step.

    `plan` is a list of dicts. `{"do": "answer"}` is a poll that answered
    healthily; `miss` is no answer at all; `refuse` is an answer with a
    non-ok status; `advance` moves the clock without polling, which is how a
    hung request is expressed. Every step advances the clock by POLL_SEC
    unless it names its own `gap`.
    """
    path = MODEL_JS
    if model_source is not None:
        assert tmp_path is not None
        path = tmp_path / "_status_model.js"
        path.write_text(model_source, encoding="utf-8")

    # `node -e` with the inputs in the environment, so the rig writes no file
    # into the repository. A full suite run already leaves three artifacts
    # behind and a fourth would be one more thing to gitignore.
    out = subprocess.run(
        [NODE, "-e", _DRIVER],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={
            **os.environ,
            "MYCELIUM_STATUS_MODEL": str(path.resolve()),
            "MYCELIUM_STATUS_PLAN": json.dumps(plan),
        },
    )
    assert out.returncode == 0, f"driver failed:\n{out.stderr}"
    return json.loads(out.stdout)


def answers(n, **kw):
    return [dict(do="answer", **kw) for _ in range(n)]


def misses(n):
    return [{"do": "miss"} for _ in range(n)]


# ── The six transports ───────────────────────────────────────────────────

def test_healthy_settles_on_ok_and_never_earns_a_banner():
    """The resting state: every facet confirmed within the last two polls."""
    frames = run(answers(6))
    assert frames[0]["severity"] == "no answer", "the load frame claimed something"
    # One good poll is `recovering`; green arrives on the second.
    assert frames[1]["severity"] == "recovering"
    assert frames[2]["severity"] == "ok"
    for f in frames[2:]:
        assert f["severity"] == "ok"
        assert f["tone"] == "ok"
        assert f["banner"] is None, "ok earned a banner"
        assert f["ageText"].startswith("as of ")


def test_dead_backend_yields_four_unknowns_and_zero_bads():
    """The dependency rule, stated as bluntly as it can be tested.

    A coordinator that answers nothing cannot tell you inference is down, that
    the gate is open, or that commits are failing. Saying any of those would be
    inventing the answer.
    """
    frames = run(misses(12))
    for f in frames:
        assert "bad" not in f["values"].values(), (
            f"a silent coordinator produced a bad: {f['values']} at t+{f['t']}s"
        )
    final = frames[-1]
    assert set(final["values"].values()) == {"unknown"}, final["values"]
    assert final["severity"] == "no answer"
    assert final["tone"] == "unknown"


def test_flapping_never_flashes_green():
    """Alternating answer and silence — the design's `n % 2` transport.

    One good poll after a bad or unknown one reads `recovering`, so a backend
    that answers every other poll oscillates between `no answer` and
    `recovering` and never once shows green."""
    plan = []
    for i in range(1, 13):
        plan.append({"do": "miss"} if i % 2 == 0 else {"do": "answer"})
    frames = run(plan)
    assert any(f["severity"] == "recovering" for f in frames), (
        "the rig never reached recovering, so this assertion proves nothing"
    )
    for f in frames:
        assert f["severity"] != "ok", f"flapping flashed green at t+{f['t']}s"


def test_inference_offline_is_confirmed_twice_and_warns_rather_than_alarms():
    """A probe has a 5s timeout, so one no is not an outage. Past runs stay
    readable, which is why this is warn and not danger."""
    frames = run(answers(3) + answers(1, inference=False) + answers(3, inference=False))
    one_no = frames[4]
    assert one_no["values"]["inference"] == "good", "one probe failure moved the lamp"
    assert one_no["severity"] == "ok"
    confirmed = frames[5]
    assert confirmed["values"]["inference"] == "bad", "two failures did not confirm it"
    assert confirmed["severity"] == "inference offline"
    assert confirmed["tone"] == "warn", "inference offline rendered as danger"
    assert confirmed["banner"] == "inference"


def test_nocommit_flips_on_the_first_observation():
    """A 503 on a write is a statement, not a probe. It needs no second
    opinion, and it outranks an inference outage."""
    frames = run(answers(3) + [{"do": "writeRefused"}])
    assert frames[3]["severity"] == "ok", "the run never reached a settled state"
    assert frames[4]["values"]["commit"] == "bad"
    assert frames[4]["severity"] == "not committing"
    assert frames[4]["banner"] == "commit"


def test_failopen_flips_on_the_first_poll_and_outranks_everything():
    """`unprotected` is ranked first because the console looks perfectly fine
    in this state — which is exactly what makes it the worst one."""
    frames = run(answers(2) + answers(1, gate=False))
    assert frames[3]["values"]["gate"] == "bad"
    assert frames[3]["severity"] == "unprotected"
    assert frames[3]["banner"] == "gate"

    # Worst wins: with everything wrong at once, the gate still names the word.
    worst = run(answers(2) + answers(2, gate=False, inference=False)
                + [{"do": "writeRefused"}])[-1]
    assert worst["values"] == {"link": "good", "inference": "bad",
                               "commit": "bad", "gate": "bad"}, worst["values"]
    assert worst["severity"] == "unprotected"


# ── The hysteresis, rule by rule ─────────────────────────────────────────

def test_the_initial_state_is_never_ok():
    """On load there is nothing to be confident about. A lamp that starts
    green has already lied once before the first answer arrives."""
    load = run([])[0]
    assert load["severity"] == "no answer"
    assert load["tone"] == "unknown"
    assert load["heard"] is False
    assert set(load["values"].values()) == {"unknown"}
    assert load["ageText"].startswith("no answer for ")


def test_one_missed_poll_changes_nothing_but_the_age():
    frames = run(answers(3) + misses(1))
    before, after = frames[3], frames[4]
    assert after["values"] == before["values"], "one dropped poll moved a facet"
    assert after["severity"] == "ok" == before["severity"]
    assert after["banner"] is None, "one dropped poll earned a banner"
    assert after["silentSeconds"] > before["silentSeconds"], "the age stood still"


def test_two_misses_go_hollow():
    frames = run(answers(3) + misses(2))
    assert frames[4]["severity"] == "ok", "the first miss should have held"
    assert frames[5]["severity"] == "no answer"
    assert frames[5]["tone"] == "unknown", "not-heard-back is not hollow"
    assert set(frames[5]["values"].values()) == {"unknown"}


def test_fifteen_seconds_of_silence_goes_hollow_even_with_one_miss():
    """Whichever comes first. A request that hangs rather than fails produces
    no second miss to count, and this is the branch that catches it."""
    frames = run(answers(3) + [{"do": "miss"}] + [{"do": "advance", "seconds": 11}])
    one_miss = frames[4]
    assert one_miss["severity"] == "ok", "the miss count already expired it"
    assert one_miss["silentSeconds"] < 15
    hollow = frames[5]
    assert hollow["silentSeconds"] >= 15
    assert hollow["severity"] == "no answer"
    assert set(hollow["values"].values()) == {"unknown"}


def test_sixty_seconds_earns_words_and_nothing_sooner_does():
    frames = run(answers(3) + misses(2) + [{"do": "advance", "seconds": 30}]
                 + [{"do": "advance", "seconds": 30}])
    quiet = frames[5]
    assert quiet["severity"] == "no answer" and quiet["banner"] is None, (
        "silence earned a banner before 60s"
    )
    assert frames[6]["silentSeconds"] < 60 and frames[6]["banner"] is None
    loud = frames[7]
    assert loud["silentSeconds"] >= 60
    assert loud["banner"] == "silent"


def test_recovery_needs_two_good_polls():
    frames = run(answers(2) + answers(2, inference=False) + answers(2))
    assert frames[4]["severity"] == "inference offline"
    first_good = frames[5]
    assert first_good["severity"] == "recovering", "one good poll went straight to green"
    assert first_good["tone"] == "warn"
    assert first_good["banner"] is None, "recovering earned a banner"
    assert frames[6]["severity"] == "ok"


def test_a_stated_fact_never_waits_for_a_second_poll():
    """Both stated facts, against the probe that does wait."""
    gate = run(answers(2) + answers(1, gate=False))
    assert gate[3]["values"]["gate"] == "bad"

    commit = run(answers(2) + [{"do": "writeRefused"}])
    assert commit[3]["values"]["commit"] == "bad"

    probe = run(answers(2) + answers(1, inference=False))
    assert probe[3]["values"]["inference"] == "good", (
        "a probe was believed on its first no"
    )


def test_a_write_that_lands_clears_the_refusal_over_two_polls():
    frames = run(answers(2) + [{"do": "writeRefused"}]
                 + [{"do": "writeAccepted"}] + answers(2))
    assert frames[3]["severity"] == "not committing"
    # writeAccepted only releases the latch; it does not turn the lamp green.
    assert frames[4]["values"]["commit"] == "bad"
    assert frames[5]["severity"] == "recovering"
    assert frames[6]["severity"] == "ok"


def test_link_leaving_up_takes_every_served_facet_with_it():
    """A confirmed bad, then silence. The bad must not survive the silence —
    the coordinator is no longer saying it."""
    frames = run(answers(2) + answers(2, inference=False) + misses(2))
    assert frames[4]["values"]["inference"] == "bad"
    gone = frames[6]
    assert gone["linkUp"] is False
    assert gone["values"] == {"link": "unknown", "inference": "unknown",
                              "commit": "unknown", "gate": "unknown"}, gone["values"]


def test_a_refusing_coordinator_reports_no_answer_rather_than_a_verdict():
    """A status code is an answer about the transport, not about inference,
    commits or the gate. Those three follow the dependency rule."""
    frames = run(answers(3) + [{"do": "refuse"}, {"do": "refuse"}])
    final = frames[-1]
    assert final["values"]["inference"] == "unknown"
    assert final["values"]["commit"] == "unknown"
    assert final["values"]["gate"] == "unknown"
    assert final["severity"] == "no answer"


def test_the_age_of_the_last_answer_is_always_present():
    plans = {
        "load": [],
        "healthy": answers(4),
        "dead": misses(4),
        "flapping": [{"do": "miss"}, {"do": "answer"}, {"do": "miss"}, {"do": "answer"}],
        "offline": answers(4, inference=False),
        "failopen": answers(4, gate=False),
    }
    for name, plan in plans.items():
        for f in run(plan):
            assert f["ageText"], f"{name} rendered no age at t+{f['t']}s"
            assert f["ageText"].endswith("s"), f"{name}: {f['ageText']!r}"
            assert f["silentSeconds"] >= 0


def test_no_facet_renders_a_cached_value_as_current():
    """The original defect, as one assertion: after the coordinator stops
    answering, nothing on screen still claims to be a current fact."""
    frames = run(answers(4) + misses(2))
    live, stale = frames[4], frames[6]
    assert live["values"] == {"link": "good", "inference": "good",
                              "commit": "good", "gate": "good"}
    assert set(stale["values"].values()) == {"unknown"}
    assert stale["ageText"].startswith("no answer for ")


def test_the_constants_are_the_ones_the_design_states():
    frames = run([])  # any run; this reads the module through the same path
    assert frames  # the driver ran
    source = MODEL_JS.read_text(encoding="utf-8")
    for name, value in [
        ("POLL_SEC", 5), ("CONFIRM_BAD", 2), ("CONFIRM_GOOD", 2),
        ("MISS_LIMIT", 2), ("SILENT_SEC", 15), ("BANNER_SEC", 60),
        ("OPERATOR_POLL_SEC", 30),
    ]:
        assert f"var {name} = {value};" in source, f"{name} is not {value}"


# ── The rules, checked against a model with each one removed ─────────────

# Each entry is (name, what to break in the source, which test must then fail).
# The replacement is a real regression someone could plausibly write, not a
# syntax error: the point is to prove the assertion is load-bearing.
VIOLATIONS = [
    (
        "the dependency rule",
        ("      return answering ? raw(key) : 'unknown';",
         "      return raw(key);"),
        "dependency",
    ),
    (
        "misses expiring a facet",
        ("      if (f.miss >= MISS_LIMIT || silentSeconds(state, nowMs) >= SILENT_SEC) {\n"
         "        f.v = 'unknown';\n"
         "      }",
         "      if (false) { f.v = 'unknown'; }"),
        "misses",
    ),
    (
        "silence expiring the link",
        ("    var linkValue = (silent >= SILENT_SEC) ? 'unknown' : raw('link');",
         "    var linkValue = raw('link');"),
        "silence",
    ),
    (
        "two good polls before green",
        ("        if (f.good >= CONFIRM_GOOD) f.v = 'good';\n"
         "        else f.recovering = true;   // rule 4",
         "        f.v = 'good';"),
        "recovery",
    ),
    (
        "two bad probes before a bad",
        ("    if (f.bad >= CONFIRM_BAD) f.v = 'bad';",
         "    f.v = 'bad';"),
        "probe",
    ),
    (
        "the initial state being unknown",
        ("    return {v: 'unknown', good: 0, bad: 0, miss: 0, recovering: false};",
         "    return {v: 'good', good: 0, bad: 0, miss: 0, recovering: false};"),
        "initial",
    ),
    (
        "the 60s banner threshold",
        ("    else if (values.link === 'unknown' && silent >= BANNER_SEC) banner = 'silent';",
         "    else if (values.link === 'unknown') banner = 'silent';"),
        "banner",
    ),
    (
        "worst-facet-wins ordering",
        ("    if (values.gate === 'bad') { severity = 'unprotected'; tone = 'bad'; }",
         "    if (false) { severity = 'unprotected'; tone = 'bad'; }"),
        "severity",
    ),
]


def _broken(find: str, replace: str) -> str:
    source = MODEL_JS.read_text(encoding="utf-8")
    assert find in source, (
        "the violation no longer matches the model, so this check is inert:\n" + find
    )
    return source.replace(find, replace, 1)


# The scenario each rule is proved load-bearing by, and what breaking it does.
PROBES = {
    # Silence rather than misses: pollMissed() also expires every facet at
    # source, so a miss-driven probe would pass with the derive-time guard
    # removed. A request that hangs produces no miss to count, and that is the
    # case only the guard in derive() catches.
    "dependency": (
        answers(2) + answers(2, inference=False) + [{"do": "advance", "seconds": 20}],
        lambda f: f[-1]["values"]["inference"] == "unknown",
    ),
    # Two misses, not three: by the third the silence rule would expire the
    # link on its own, and this probe would pass whether the miss count works
    # or not — the exact vacuous pass this whole block exists to rule out.
    "misses": (answers(3) + misses(2), lambda f: f[-1]["severity"] == "no answer"),
    "silence": (
        answers(3) + [{"do": "miss"}, {"do": "advance", "seconds": 11}],
        lambda f: f[-1]["severity"] == "no answer",
    ),
    "recovery": (
        answers(2) + answers(2, inference=False) + answers(1),
        lambda f: f[-1]["severity"] == "recovering",
    ),
    "probe": (
        answers(2) + answers(1, inference=False),
        lambda f: f[-1]["values"]["inference"] == "good",
    ),
    "initial": ([], lambda f: f[0]["severity"] == "no answer"),
    "banner": (answers(2) + misses(2), lambda f: f[-1]["banner"] is None),
    "severity": (
        answers(2) + answers(1, gate=False),
        lambda f: f[-1]["severity"] == "unprotected",
    ),
}


@pytest.mark.parametrize("name,edit,probe", VIOLATIONS, ids=[v[0] for v in VIOLATIONS])
def test_every_rule_rejects_a_deliberately_broken_model(name, edit, probe, tmp_path):
    """Remove one rule from the model; the check that guards it must notice.

    Without this, a rule could be asserted in a scenario that never reaches it
    — green because nothing was exercised rather than because it held.
    """
    plan, holds = PROBES[probe]

    intact = run(plan)
    assert holds(intact), (
        f"the probe for {name!r} does not hold against the real model, so it "
        "cannot prove anything about a broken one"
    )

    broken = run(plan, model_source=_broken(*edit), tmp_path=tmp_path)
    assert not holds(broken), (
        f"removing {name} from the model changed nothing the checks look at, "
        "so that rule is not actually tested"
    )
