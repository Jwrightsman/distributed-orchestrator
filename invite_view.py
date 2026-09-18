"""The Connect view — how a machine joins, how an app pitches, and what each costs.

The archived design (`docs/design/console-2026-08-28`, "Invite") drew this view
as static copy. Several of its sentences no longer match source, and
`docs/design/HANDOFF-DELTA.md` §11 records each one. The view is rebuilt from
the code that does the joining and the pitching, so:

1. **The join command is the installer's.** `worker_installer.py` replaced the
   `join.py` one-liner the design shows. It asks for the address and reads the
   invitation code with echo off, so the command carries neither, and this
   view prints no address it would have to guess.

2. **The consent list is the installer's own paragraph, shortened.** Each item
   names the phrase in `worker_installer.consent_text` it stands for, and a
   test requires every phrase to still be there.

3. **The warning reads this coordinator's settings.** Which authorities are
   off, whether HTTPS is declared, whether a machine may join without an
   enrolment of its own. A credential becomes a word through
   `config_view.credential_state`; no value reaches this module's output.

4. **An app needs two keys, not one.** `mcp_server.py` sends `PITCH_KEY` to
   submit and `VIEWER_KEY` to read the job back, because `/jobs/{id}` and
   `/projects` are private routes. Neither key is scoped to one app, so the
   panel names both levers and what each cuts off.

5. **The tools are the MCP server's.** `MCP_TOOLS` lists names a test finds
   registered in `mcp_server.py`, in both directions.

Colour is never carried inline. Panel primitives come from
`templates/_run_detail.css` and the rest from `templates/_dashboard.css`, so
`tests/test_theme.py` holds the no-hardcoded-colour rule over this module too.
"""

from __future__ import annotations

import html as _html
from typing import Any, Mapping

import config
from config_view import AUTHORITIES, OFF, SHORT, credential_state, duration_words

# What joining costs the host, each item with the phrase in
# `worker_installer.consent_text` it stands for. `tone` is `cost` for what the
# owner gives up and `limit` for what the software will not do.
CONSENT = (
    ("cost", "About 2.5 GB of disk for the model, if it is not already there.",
     ("about 2.5 GB of disk",)),
    ("cost", "Their processor at full load, in bursts of minutes, on prompts other "
             "people submit.",
     ("your processor running flat out, in bursts of minutes",)),
    ("cost", "You see the answers their machine produces, that it is connected, and "
             "how much work it has done.",
     ("can see the answers your computer produces",
      "that your machine is connected and how much work it has done")),
    ("limit", "Nothing is downloaded or written until they type yes. Closing the window "
              "stops it, and one command removes everything the installer set up.",
     ("One command removes", "everything this installer set up")),
    ("limit", "Their machine runs nothing it receives; it answers prompts as text. No "
              "inbound port is opened, and nothing is reported to anybody but this "
              "coordinator.",
     ("does not run", "no inbound port is opened",
      "nothing is reported to anybody except that one coordinator")),
)

# The five tools `mcp_server.py` registers, in the order `docs/MCP.md` lists
# them. A test requires this set and the registered set to agree.
MCP_TOOLS = (
    ("pitch_task", "Hand over a task, optionally into a project. Returns a job id "
                   "straight away."),
    ("get_job_status", "Queued, running, complete or failed, with the subtask plan once "
                       "there is one."),
    ("get_result", "The assembled output and the list of code files extracted from it."),
    ("list_projects", "The projects this coordinator remembers between runs."),
    ("continue_project", "Iterate on one, with what earlier runs built already in "
                         "context."),
)

JOIN_COMMANDS = (
    "git clone https://github.com/Jwrightsman/distributed-orchestrator",
    "cd distributed-orchestrator",
    "python worker_installer.py",
)

MCP_COMMAND = "claude mcp add swarm -- py /path/to/distributed-orchestrator/mcp_server.py"

# What each authority leaves open while it is off, in the words of the
# warning panel on this view.
_OPEN_WHILE_OFF = {
    "viewer_key": "anyone can read every run, machine and project",
    "node_secret": "any machine can join, whatever code is typed at the installer",
    "pitch_key": "anyone can spend these machines' compute",
}


def esc(value: Any) -> str:
    return _html.escape(str(value if value is not None else ""), quote=True)


def _code(text: str) -> str:
    return f'<code class="rd-code">{esc(text)}</code>'


# ── the record ───────────────────────────────────────────────────────


def _rate_words(settings: Mapping[str, Any]) -> str:
    count = settings.get("pitch_rate_max", config.DEFAULTS["pitch_rate_max"])
    window = settings.get("pitch_rate_window", config.DEFAULTS["pitch_rate_window"])
    per = "a minute" if window == 60 else f"per {duration_words(window) or f'{window} seconds'}"
    noun = "pitch" if count == 1 else "pitches"
    return f"{count} {noun} {per}, per address"


def build_record(settings: Mapping[str, Any]) -> dict:
    """Everything the view prints that depends on this coordinator."""
    authorities = {key: credential_state(key, settings.get(key)) for key in AUTHORITIES}
    return {
        "authorities": authorities,
        "open": [key for key in AUTHORITIES if authorities[key] == OFF],
        "short": [key for key in AUTHORITIES if authorities[key] == SHORT],
        "https_declared": bool(settings.get("https_enabled")),
        "enrollment_required": settings.get("node_enrollment_mode") == "required",
        "rate": _rate_words(settings),
        "consent": [{"tone": tone, "text": text} for tone, text, _ in CONSENT],
        "mcp_tools": [{"name": name, "what": what} for name, what in MCP_TOOLS],
    }


# ── the fragment ─────────────────────────────────────────────────────


def _divider(label: str) -> str:
    return f"""
      <div class="iv-divider"><span>{esc(label)}</span></div>"""


def _command_block(lines: tuple[str, ...], label: str) -> str:
    rows = "".join(
        f'<div class="iv-cmd-line"><span class="iv-prompt" aria-hidden="true">$</span>'
        f'<code>{esc(line)}</code></div>'
        for line in lines
    )
    return f"""
          <div class="iv-cmd">
            <div class="iv-cmd-lines">{rows}</div>
            <button class="iv-copy" type="button" data-copy="{esc(chr(10).join(lines))}"
                    title="Copy to the clipboard" aria-label="Copy {esc(label)}">copy</button>
          </div>"""


def _join_panel() -> str:
    return f"""
        <section class="rd-panel iv-panel" data-invite="join">
          <div class="rd-panel-head">
            <span class="rd-panel-label">THE COMMANDS</span>
            <span class="rd-panel-meta">8 GB of memory, Python, Ollama</span>
          </div>
          <div class="iv-body">{_command_block(JOIN_COMMANDS, "the join commands")}
            <p class="iv-note">The installer asks for two things you give them: this
              coordinator's {_code("https://")} address and the invitation code, which is
              {_code("node_secret")}. It refuses a plain {_code("http://")} address, and it
              reads the code with the echo off, never from the command line. On Windows,
              if {_code("python")} opens the Microsoft Store, type {_code("py")} instead.
              {_code("docs/JOIN.md")} walks through every step.</p>
          </div>
        </section>"""


def _consent_panel(record: dict) -> str:
    items = "".join(
        f'<li class="iv-consent-item"><span class="iv-mark is-{esc(c["tone"])}" '
        f'aria-hidden="true"></span><span>{esc(c["text"])}</span></li>'
        for c in record["consent"]
    )
    return f"""
        <section class="rd-panel iv-panel" data-invite="consent">
          <div class="rd-panel-head">
            <span class="rd-panel-label">WHAT THEY ARE AGREEING TO</span>
          </div>
          <ul class="iv-consent">{items}</ul>
          <div class="rd-panel-note">The installer prints all of this and waits for them to
            type yes, with no flag to skip it. {_code("join.py")} has a {_code("--yes")} for
            people scripting their own machines; an agent should not pass it on someone
            else's behalf.</div>
        </section>"""


def _share_warning(record: dict) -> str:
    items = []
    for key in record["open"]:
        items.append((key, _OPEN_WHILE_OFF[key]))
    for key in record["short"]:
        items.append((key, f"shorter than the {config.MIN_STATIC_CREDENTIAL_LENGTH} "
                           "characters trusted_alpha requires"))
    if not record["https_declared"]:
        items.append(("https_enabled", "false, and a machine elsewhere cannot join over "
                                       "plain http"))
    if not record["enrollment_required"]:
        items.append(("node_enrollment_mode", "a machine may register on the invitation "
                                              "code alone, with no enrolment of its own"))

    if not items:
        return """
        <section class="rd-panel iv-panel" data-invite="share" data-share-ready>
          <div class="rd-panel-head">
            <span class="iv-mark is-limit" aria-hidden="true"></span>
            <span class="rd-panel-label">BEFORE YOU SHARE THE ADDRESS</span>
          </div>
          <div class="iv-body"><p class="iv-note">All three keys are set, HTTPS is declared
            in front of this coordinator, and every machine must enrol. Give each person
            the address and the invitation code, and nothing else.</p></div>
        </section>"""

    rows = "".join(
        f'<li><span class="iv-warn-key">{esc(key)}</span>'
        f'<span class="iv-warn-what">— {esc(what)}</span></li>'
        for key, what in items
    )
    return f"""
        <section class="rd-panel iv-panel is-warn" data-invite="share">
          <div class="rd-panel-head">
            <span class="rd-panel-label">BEFORE YOU SHARE THE ADDRESS</span>
          </div>
          <div class="iv-body">
            <p class="iv-note">Sending this address to someone outside your own network makes
              the coordinator reachable from theirs. As it is configured now:</p>
            <ul class="iv-warn-list">{rows}</ul>
            <button class="iv-action" type="button" data-tab="config" title="Open configuration">Open config</button>
          </div>
        </section>"""


def _status_panel() -> str:
    return f"""
        <section class="rd-panel iv-panel" data-invite="status">
          <div class="rd-panel-head">
            <span class="rd-panel-label">CHECKING WITHOUT JOINING</span>
          </div>
          <div class="iv-body"><p class="iv-note">Anyone can call {_code("/status.json")}
            with no key: whether inference is up, the model, machines online, runs built,
            contributors and uptime. No task text and no hostnames. That is the safe way
            for someone to see the network is alive before deciding.</p></div>
        </section>"""


def _mcp_panel(record: dict) -> str:
    tools = "".join(
        f'<div class="iv-tool"><span class="iv-tool-name">{esc(t["name"])}</span>'
        f'<span class="iv-tool-what">{esc(t["what"])}</span></div>'
        for t in record["mcp_tools"]
    )
    return f"""
        <section class="rd-panel iv-panel" data-invite="mcp">
          <div class="rd-panel-head">
            <span class="rd-panel-label">CONNECT A CLIENT</span>
            <span class="rd-panel-meta">any MCP client</span>
          </div>
          <div class="iv-body">
            <p class="iv-lede">A machine adds compute. This adds a way to <em>ask</em> for
              work: the coordinator ships an MCP server, so an AI app can hand a task to
              these machines and collect the result without anyone opening this console.</p>
            {_command_block((MCP_COMMAND,), "the MCP command")}
            <p class="iv-note">That is Claude Code. Desktop apps want an
              {_code("mcpServers")} entry naming the same file, then a full quit and reopen
              before the tools appear. Either way the server reads
              {_code("ORCHESTRATOR_URL")}, {_code("PITCH_KEY")} when pitching needs a key,
              and {_code("VIEWER_KEY")} when reading does. {_code("docs/MCP.md")} has the
              block.</p>
          </div>
          <div class="iv-tools">
            <div class="iv-tools-head">WHAT IT CAN THEN DO</div>{tools}
          </div>
          <div class="rd-panel-note">Nothing here is a shortcut past the pipeline: it is the
            job API this console uses, {_code("/pitch/async")} and {_code("/jobs/{id}")}. A
            pitch returns a job id at once and the app polls, because a run takes minutes
            on CPU. {esc(record["rate"])}.</div>
        </section>"""


def _client_warning(record: dict) -> str:
    if record["enrollment_required"]:
        machines = ("Every machine on this network has its own enrolment, so you can "
                    "retire one and leave the rest working.")
    else:
        machines = ("A machine that joined through the installer has its own enrolment, "
                    "so you can retire it and leave the rest working. With "
                    f"{_code('node_enrollment_mode')} at {_code('compat')}, an older "
                    "worker may join without one.")
    return f"""
        <section class="rd-panel iv-panel is-warn" data-invite="client">
          <div class="rd-panel-head">
            <span class="rd-panel-label">A CLIENT IS NOT A MACHINE</span>
          </div>
          <div class="iv-body">
            <p class="iv-note">{machines} Apps have nothing of the kind. They present the
              same {_code("pitch_key")} to submit and the same {_code("viewer_key")} to read
              the result back, and that key reads every run, machine and project, not only
              their own. The network cannot tell one app from another, so each lever cuts
              off every app at once:</p>
            <ul class="iv-warn-list">
              <li><span class="iv-warn-key">change pitch_key</span><span class="iv-warn-what">— no app can submit</span></li>
              <li><span class="iv-warn-key">change viewer_key</span><span class="iv-warn-what">— no app can read, and every browser session ends</span></li>
            </ul>
            <p class="iv-note">So hand them to people, not to services you would struggle to
              reach again. And a run an app pitches lands in Runs looking exactly like your
              own: nothing records which door a pitch came through.</p>
          </div>
        </section>"""


def render(record: dict) -> str:
    return f"""
    <div class="iv" data-invite-view>{_divider("A MACHINE, TO ADD COMPUTE")}
      <div class="iv-row">
        <div class="iv-main">{_join_panel()}{_consent_panel(record)}
        </div>
        <div class="iv-side">{_share_warning(record)}{_status_panel()}
        </div>
      </div>{_divider("AN AI APP, TO PITCH WORK")}
      <div class="iv-row">
        <div class="iv-main">{_mcp_panel(record)}
        </div>
        <div class="iv-side">{_client_warning(record)}
        </div>
      </div>
    </div>"""
