"""The Config view — what this coordinator loaded, and what each setting does.

`docs/design/HANDOFF-DELTA.md` §10 is the design pass this module implements.
The archived console drew this view from a hand-written list of keys and notes;
several of those notes described behaviour source no longer has, so the view
is built from `config.DEFAULTS` and from what this process recorded at startup,
and every note is a claim checked against the code that reads the key.

Rules this file holds, each of them easy to undo by accident:

1. **No credential value leaves this module.** `viewer_key`, `node_secret`,
   `pitch_key` and `provider_api_key` render as `off`, `set`, or `set · short`
   and nothing else: no length, no prefix, no mask with a character showing.
   The record the route serves is built from those words, so there is no field
   holding a value that a later template could print by mistake.

2. **Every key is placed on purpose.** `GROUPS` names every key in
   `config.DEFAULTS` exactly once, and a test fails when a key is added to
   config without being placed here. A new setting cannot reach this page
   without someone deciding whether it is a credential.

3. **An address shows its origin only.** Scheme, host and port. Credentials,
   path, query and fragment are dropped, because an OpenAI-compatible base URL
   or a collector endpoint can carry a token in any of them.

4. **A key nothing reads says so.** `INERT_KEYS` is the set of keys no module
   reads, established by a scan the tests repeat, and those rows say the value
   changes nothing rather than describing what the key once did.

5. **Unknown keys are named, never valued.** `config.json` may carry keys this
   version does not read — including a misspelt credential, which leaves its
   gate open without a word. Their names are listed so that can be seen.

Colour is never carried inline. Panel primitives come from
`templates/_run_detail.css` and the rest from `templates/_dashboard.css`, so
`tests/test_theme.py` holds the no-hardcoded-colour rule over this module too.
"""

from __future__ import annotations

import html as _html
import json
from datetime import datetime
from typing import Any, Mapping
from urllib.parse import urlsplit

import config
from server_state import _PUBLIC_MAX_ACTIVE, _PUBLIC_RATE_MAX, _PUBLIC_TASK_MAX

# The three independent authorities, and the one other credential config holds.
AUTHORITIES = ("viewer_key", "node_secret", "pitch_key")
CREDENTIAL_KEYS = AUTHORITIES + ("provider_api_key",)

# Keys holding an address that may embed a credential.
ADDRESS_KEYS = ("ollama_url", "provider_base_url", "tracing_endpoint")

# Keys nothing the coordinator runs reads. `status.py` prints two of them for a
# person, which is why the rows name it. `tests/test_config_view.py` rescans
# tracked source for readers and fails when this set and the scan disagree, in
# either direction.
INERT_KEYS = ("port", "role_model_map", "tracing_endpoint")

OFF = "off"
SET = "set"
SHORT = "set · short"

# What each authority guards, in the word the banner uses.
_GUARDS = {"viewer_key": "read", "node_secret": "join", "pitch_key": "spend"}

GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("DEPLOYMENT & OWNERSHIP", ("deployment_mode", "node_enrollment_mode")),
    ("AUTHORITIES · THREE SEPARATE KEYS", (
        "viewer_key", "node_secret", "pitch_key",
        "viewer_session_ttl_seconds", "viewer_cookie_secure",
        "public_pitch", "public_pitch_acknowledged",
    )),
    ("SERVER & TRANSPORT", (
        "bind_host", "port", "https_enabled", "private_overlay",
        "trust_proxy_headers", "pitch_rate_max", "pitch_rate_window",
    )),
    ("INFERENCE", (
        "model", "ollama_url", "context_tokens", "timeout", "planner_retries",
        "think", "temperature", "seed",
    )),
    ("EXTERNAL PROVIDER", (
        "provider", "provider_model", "provider_base_url", "provider_api_key",
        "provider_roles",
    )),
    ("ARTIFACTS & STORAGE", (
        "artifact_max_files", "artifact_max_file_bytes",
        "artifact_max_aggregate_bytes", "artifact_retention_seconds",
        "execution_artifacts_max_mb", "output_max_mb",
    )),
    ("VALIDATION", (
        "validator_execution_mode", "validator_subprocess_timeout_seconds",
        "validator_subprocess_memory_mb", "validator_subprocess_request_max_bytes",
        "validator_subprocess_response_max_bytes",
    )),
    ("SAMPLING & EVIDENCE", (
        "verify_rate", "capability_evidence_mode",
        "capability_evidence_min_samples", "role_model_map",
    )),
    ("TRACING", ("tracing_enabled", "tracing_export", "tracing_endpoint")),
)

_GROUP_NOTES = {
    "AUTHORITIES · THREE SEPARATE KEYS": (
        "Each key guards one thing and none stands in for another: reading, joining, "
        "spending. A key shows whether it is set, never its value."
    ),
    "SAMPLING & EVIDENCE": "Nothing in this group changes where a task is placed.",
}

# The durable set, each item with the tables that hold it. A test requires every
# table named here to be created somewhere in source.
WRITTEN_DOWN = (
    ("Runs, their outcomes and their sealed file lists",
     ("executions", "artifact_roots", "artifact_entries")),
    ("Leases and the results accepted against them",
     ("attempts", "accepted_result_receipts")),
    ("Points, in the contribution ledger", ("contributions",)),
    ("Enrolments — which machine did what, across restarts", ("node_enrollments",)),
    ("Share links, by hash, with their expiry and revocation", ("execution_shares",)),
    ("Idempotency keys, by hash, so a repeated pitch finds its run",
     ("execution_submissions",)),
)

# What a restart loses, each item with the phrases from `scripts/backup.py`'s
# `not_included` list it covers. A test requires every phrase to be covered.
GONE_ON_RESTART = (
    ("The queue, and anything still mid-flight",
     ("process-local scheduler queues", "in-flight work")),
    ("Worker sessions — every machine registers again",
     ("process-local node sessions",)),
    ("Model calls in progress and open event streams", ()),
    ("Which machines were connected a moment ago", ()),
)


def esc(value: Any) -> str:
    return _html.escape(str(value if value is not None else ""), quote=True)


def _code(name: str) -> str:
    return f'<code class="rd-code">{esc(name)}</code>'


def _breakable(name: str) -> str:
    """A key name that may wrap after an underscore and nowhere else."""
    return "_<wbr>".join(esc(part) for part in str(name).split("_"))


# ── classification ───────────────────────────────────────────────────


def credential_state(key: str, value: Any) -> str:
    """`off`, `set`, or `set · short`. The only thing a credential ever becomes.

    Enforcement treats any non-empty value as a gate, so `off` means exactly
    "the gate is open". Short is the static credential policy trusted_alpha
    refuses to start below; `provider_api_key` has no such policy.
    """
    if not value:
        return OFF
    if key in AUTHORITIES and not config.credential_meets_policy(value):
        return SHORT
    return SET


def shared_authorities(settings: Mapping[str, Any]) -> dict[str, list[str]]:
    """Which authorities hold the same value as which others. Names only.

    Compared stripped, the way `scripts/preflight.py` compares them, so this
    and the preflight warning agree about which keys collide.
    """
    stripped = {
        key: str(settings.get(key)).strip()
        for key in AUTHORITIES
        if settings.get(key) and str(settings.get(key)).strip()
    }
    return {
        key: [other for other in AUTHORITIES if other != key and stripped.get(other) == value]
        for key, value in stripped.items()
        if any(other != key and stripped.get(other) == value for other in AUTHORITIES)
    }


def address_origin(value: Any) -> tuple[str, bool]:
    """(what renders, whether anything was left out). Never the whole address."""
    if value is None or value == "":
        return "unset", False
    if not isinstance(value, str):
        return "set · not shown", True
    try:
        parts = urlsplit(value.strip())
        host = parts.hostname
        port = parts.port
    except ValueError:
        return "set · not shown", True
    if not parts.scheme or not host:
        return "set · not shown", True
    if ":" in host:
        host = f"[{host}]"
    origin = f"{parts.scheme.lower()}://{host}" + (f":{port}" if port else "")
    trimmed = bool(
        parts.username or parts.password or parts.path not in ("", "/")
        or parts.query or parts.fragment
    )
    return origin, trimmed


def plain_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, separators=(", ", ": "))
    return str(value)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def duration_words(seconds: Any) -> str | None:
    if type(seconds) is not int or seconds <= 0:
        return None
    for size, word in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size and seconds % size == 0:
            return _plural(seconds // size, word)
    if seconds >= 60:
        return f"{seconds / 60:.1f} minutes"
    return None


def byte_words(count: Any) -> str | None:
    if type(count) is not int or count <= 0:
        return None
    for size, unit in ((1024 ** 3, "GiB"), (1024 ** 2, "MiB"), (1024, "KiB")):
        if count >= size:
            whole = count / size
            return f"{whole:.0f} {unit}" if count % size == 0 else f"{whole:.1f} {unit}"
    return None


_DURATION_KEYS = (
    "timeout", "pitch_rate_window", "artifact_retention_seconds",
    "validator_subprocess_timeout_seconds",
)
_BYTE_KEYS = (
    "artifact_max_file_bytes", "artifact_max_aggregate_bytes",
    "validator_subprocess_request_max_bytes", "validator_subprocess_response_max_bytes",
)

# The clamp `access_control.issue_viewer_session` applies.
_SESSION_MIN, _SESSION_MAX = 60, 7 * 24 * 3600


# ── the rows ─────────────────────────────────────────────────────────


def _row(key: str, value: str, *, tone: str = "", hint: str | None = None,
         note: str = "", source: str = "config") -> dict:
    return {"key": key, "source": source, "value": value, "tone": tone,
            "hint": hint, "note": note}


def _authority_note(key: str, state: str, settings: Mapping[str, Any],
                    shared: Mapping[str, list[str]]) -> str:
    ttl = _effective_session_ttl(settings.get("viewer_session_ttl_seconds"))
    notes = {
        ("viewer_key", OFF): (
            "Reading. While this is empty every task, result, project, machine record "
            "and event is served to anyone who can reach this address."
        ),
        ("viewer_key", SET): (
            "Reading. Private routes answer 401 without it — sent as a header, a bearer "
            f"token, or a browser session of {esc(duration_words(ttl) or ttl)}. Rotating "
            "it ends every browser session at once."
        ),
        ("node_secret", OFF): (
            "Joining. While this is empty any machine that can reach this address can "
            "register and ask for work."
        ),
        ("node_secret", SET): (
            "Joining. A machine needs it to enrol. The viewer key does not stand in for it."
        ),
        ("pitch_key", OFF): (
            "Spending. While this is empty anyone who can reach this address can submit "
            "work to these machines."
        ),
        ("pitch_key", SET): (
            "Spending. Submitting a task needs this key, and neither other key stands in "
            "for it."
        ),
    }
    note = notes[(key, SET if state == SHORT else state)]
    if state == SHORT:
        note += (
            f" Shorter than the {config.MIN_STATIC_CREDENTIAL_LENGTH} characters "
            f"{_code('trusted_alpha')} requires, so that mode would refuse to start."
        )
    if shared.get(key):
        names = " and ".join(_code(other) for other in shared[key])
        note += f" It has the same value as {names}, so holding one grants both."
    return note


def _effective_session_ttl(value: Any) -> int:
    try:
        ttl = int(value)
    except (TypeError, ValueError):
        ttl = 8 * 3600
    return max(_SESSION_MIN, min(ttl, _SESSION_MAX))


def config_row(key: str, settings: Mapping[str, Any], runtime: Mapping[str, Any]) -> dict:
    """One configuration key, as the words its row prints."""
    value = settings.get(key, config.DEFAULTS.get(key))
    trusted = settings.get("deployment_mode") == "trusted_alpha"

    if key in CREDENTIAL_KEYS:
        state = credential_state(key, value)
        if key == "provider_api_key":
            note = (
                f"Sent to the provider, and only when {_code('provider')} and "
                f"{_code('provider_model')} are also set."
            )
            return _row(key, state, tone="muted", note=note)
        tone = {OFF: "warn", SHORT: "warn", SET: "ok"}[state]
        shared = shared_authorities(settings)
        if shared.get(key):
            tone = "warn"
        return _row(key, state, tone=tone,
                    note=_authority_note(key, state, settings, shared))

    if key in ADDRESS_KEYS:
        shown, trimmed = address_origin(value)
        hint = "origin only" if trimmed and not shown.startswith("set ·") else None
        if key == "tracing_endpoint":
            note = (
                "Nothing reads this key. When spans are exported they go wherever the "
                "process's own OpenTelemetry setup sends them, not to this address."
            )
            return _row(key, shown, tone="muted", hint="read by nothing", note=note)
        if key == "provider_base_url" and shown == "unset":
            return _row(key, shown, tone="muted",
                        note="Unset, so a configured provider is called at OpenAI's address.")
        return _row(key, shown, hint=hint)

    shown = plain_value(value)
    hint = None
    if key in _DURATION_KEYS:
        hint = duration_words(value)
    elif key in _BYTE_KEYS:
        hint = byte_words(value)

    if key == "deployment_mode":
        if value == "trusted_alpha":
            return _row(key, shown, tone="ok", note=(
                "Startup refuses to run with any preflight error, so every check it makes "
                "held when this process started."
            ))
        return _row(key, shown, tone="warn", note=(
            "The compatibility default: it warns instead of refusing. "
            f"{_code('trusted_alpha')} will not start on any preflight error — among them, "
            f"unless all three keys are set, distinct and at least "
            f"{config.MIN_STATIC_CREDENTIAL_LENGTH} characters; machines must enrol; HTTPS "
            "or a private overlay is declared and the cookie setting agrees with it; public "
            "pitching, if on, is acknowledged; and validators do not run inline. Switch "
            "before this address leaves your own network."
        ))
    if key == "node_enrollment_mode":
        if value == "required":
            return _row(key, shown, tone="ok", note=(
                f"{_code('node_secret')} admits a machine once; after that it returns with "
                "its own enrolment."
            ))
        return _row(key, shown, tone="warn", note=(
            "A machine may register with the shared secret alone, for local development. "
            "It then has no enrolment of its own, so nothing durable records which machine "
            "did its work and it cannot be revoked on its own."
        ))
    if key == "viewer_session_ttl_seconds":
        effective = _effective_session_ttl(value)
        words = duration_words(effective)
        if effective != value:
            return _row(key, shown, tone="warn", hint=f"clamped to {words or effective}",
                        note="A browser session lasts between a minute and seven days.")
        return _row(key, shown, hint=words)
    if key == "viewer_cookie_secure":
        agrees = bool(value) == bool(settings.get("https_enabled"))
        if not agrees:
            return _row(key, shown, tone="warn", note=(
                f"Does not match {_code('https_enabled')}. Preflight warns about this, and "
                f"{_code('trusted_alpha')} refuses to start with it."
            ))
        return _row(key, shown, note=f"Matches {_code('https_enabled')}, as preflight requires.")
    if key == "public_pitch":
        if value:
            return _row(key, shown, tone="warn", note=(
                f"Anyone can submit from {_code('/try')} with no key: {_PUBLIC_RATE_MAX} an "
                f"hour per address, {_PUBLIC_TASK_MAX} characters, {_PUBLIC_MAX_ACTIVE} at "
                "once across everyone."
            ))
        return _row(key, shown, tone="muted",
                    note=f"{_code('/try')} accepts nothing; its submit route answers 404.")
    if key == "public_pitch_acknowledged":
        if settings.get("public_pitch") and not value:
            return _row(key, shown, tone="warn", note=(
                "Public pitching is on without the abuse-risk acknowledgement. Preflight "
                f"warns, and {_code('trusted_alpha')} refuses to start."
            ))
        return _row(key, shown, tone="muted")
    if key == "bind_host":
        effective = runtime.get("bind_host")
        if effective and effective != value:
            return _row(key, shown, tone="muted", note=(
                f"A fallback. This process was started on {_code(effective)}, which is what "
                "preflight checked."
            ))
        return _row(key, shown, note=(
            "Used when the launch command names no host. Preflight checks the host the "
            "process was actually started on."
        ))
    if key == "port":
        return _row(key, shown, tone="muted", hint="not read by the server", note=(
            f"The server listens where its launch command put it. Only {_code('status.py')} "
            "looks at this, to find the server."
        ))
    if key in ("https_enabled", "private_overlay"):
        return _row(key, shown, note=(
            "A declaration, not enforcement: this key neither turns TLS on nor checks the "
            "overlay. It tells preflight what stands in front of this process."
        ))
    if key == "trust_proxy_headers":
        if value:
            return _row(key, shown, tone="warn",
                        note="Not supported by this coordinator. Preflight flags it.")
        return _row(key, shown, tone="muted")
    if key == "pitch_rate_max":
        return _row(key, shown, note=(
            f"Pitches per address per window, on {_code('/pitch')}, {_code('/pitch/async')} "
            f"and {_code('/pitch/distributed')}."
        ))
    if key == "context_tokens":
        return _row(key, shown, note="Ollama's own default of 4096 cuts long deliverables off.")
    if key == "timeout":
        return _row(key, shown, hint=hint, note="Per model call. The reviewer is the long one.")
    if key == "think":
        return _row(key, shown, tone="muted" if not value else "", note=(
            "Hidden reasoning on models that support it. Off, because on CPU it multiplies "
            "latency several times over."
        ))
    if key in ("temperature", "seed"):
        if value is None:
            return _row(key, shown, tone="muted", note=(
                "Unset, so the request carries none and Ollama's default applies."
            ))
        return _row(key, shown, note=(
            "Pinned for a measurement. A seed alone does not make a run reproducible; see "
            f"{_code('sampling.py')}."
        ))
    if key == "provider":
        effective = bool(value and settings.get("provider_api_key") and settings.get("provider_model"))
        if not value:
            return _row(key, shown, tone="muted",
                        note="No external provider. Every model call goes to Ollama.")
        if not effective:
            return _row(key, shown, tone="warn", note=(
                f"Named, but {_code('provider_api_key')} or {_code('provider_model')} is "
                "unset, so nothing is sent to it."
            ))
        return _row(key, shown, tone="warn", note=(
            "Calls for the roles below leave this machine for this provider."
        ))
    if key == "provider_roles":
        return _row(key, shown, note="The roles a configured provider answers for.")
    if key == "artifact_max_aggregate_bytes":
        return _row(key, shown, hint=hint, note=(
            "Per run. A tree over any artifact limit is refused with 413 rather than "
            "served in part."
        ))
    if key == "artifact_retention_seconds":
        return _row(key, shown, hint=hint,
                    note="A finished run's registered files may be deleted after this.")
    if key == "output_max_mb":
        return _row(key, shown, note=(
            f"The oldest runs in {_code('output/')} are deleted past this. 0 turns pruning off."
        ))
    if key == "validator_execution_mode":
        if value == "inline":
            return _row(key, shown, tone="warn", note=(
                "Parsers run inside the coordinator. Preflight warns, and "
                f"{_code('trusted_alpha')} refuses to start."
            ))
        return _row(key, shown, note="Parser-heavy validators run in a separate process.")
    if key == "verify_rate":
        rate = value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
        if trusted and rate:
            return _row(key, shown, tone="muted", hint="forced off", note=(
                f"{_code('trusted_alpha')} turns sampling off whatever this says."
            ))
        return _row(key, shown, tone="muted" if not rate else "", note=(
            "The share of builder tasks also sent to a second machine, to compare the shape "
            "of two answers. Agreement is not correctness, and sampling never moves a task. "
            "Each sample costs a whole extra model call, and sampling stops below two machines."
        ))
    if key == "capability_evidence_mode":
        return _row(key, shown, tone="muted" if value == "off" else "", note=(
            "Observation only: neither value changes eligibility, the order of work, or "
            "assignment."
        ))
    if key == "role_model_map":
        return _row(key, shown, tone="muted", hint="changes nothing", note=(
            "Nothing reads this key any more, so its value changes nothing about where work "
            f"goes. {_code('status.py')} still prints it."
        ))
    if key == "tracing_enabled":
        return _row(key, shown, tone="muted" if not value else "", note=(
            "Off means no trace header is read or written and no span is built."
        ))
    if key == "tracing_export":
        return _row(key, shown, tone="muted" if not value else "", note=(
            "Whether spans leave this machine. Needs the OpenTelemetry SDK installed and "
            f"{_code('tracing_enabled')} on."
        ))
    return _row(key, shown, hint=hint)


def _started(value: Any) -> str | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def runtime_rows(runtime: Mapping[str, Any]) -> list[dict]:
    """What this process recorded about itself at startup. Not configuration."""
    rows = []
    if runtime.get("lock_held"):
        started = _started(runtime.get("started_at"))
        since = f' · since <span class="cf-nowrap">{esc(started)}</span>' if started else ""
        rows.append(_row("coordinator lock", "held", tone="ok", source="runtime", note=(
            f"Instance {_code(runtime.get('instance_id'))} · process {esc(runtime.get('pid'))}"
            f"{since}. A second coordinator aimed at the same state directory fails at "
            "once and names this one."
        )))
    else:
        rows.append(_row("coordinator lock", "not held", tone="warn", source="runtime",
                         note="This process has not finished starting."))

    rows.append(_row("worker processes", "one", source="runtime", note=(
        "Not a setting. A launch asking for more is refused, and a second process could "
        "not take the lock: the queue and machine sessions live in this one."
    )))

    state_dir = runtime.get("state_dir")
    rows.append(_row("state directory", str(state_dir) if state_dir else "not recorded",
                     tone="" if state_dir else "muted", source="runtime",
                     note="Everything written down lives here, and the lock is taken on it."))

    warnings = list(runtime.get("preflight_warnings") or ())
    items = "".join(f"<li>{esc(message)}</li>" for message in warnings)
    rows.append(_row(
        "preflight", _plural(len(warnings), "warning") if warnings else "no warnings",
        tone="warn" if warnings else "ok", source="runtime",
        note=(f'<ul class="cf-list">{items}</ul>' if items else "")
        + "Checked once, when this process started. A warning names a setting, never its value.",
    ))

    rows.append(_row("last backup", "not recorded", tone="muted", source="runtime", note=(
        f"Nothing here records a backup. {_code('scripts/backup.py')} writes its archive "
        "outside the state directory and leaves no trace in it, so this page cannot say "
        "whether one exists."
    )))
    return rows


# ── the record ───────────────────────────────────────────────────────


def runtime_facts(state: Any) -> dict:
    """The startup facts `server.py` leaves on `app.state`, or their absence."""
    identity = getattr(state, "coordinator_identity", None)
    return {
        "lock_held": identity is not None,
        "instance_id": getattr(identity, "instance_id", None),
        "pid": getattr(identity, "pid", None),
        "started_at": getattr(identity, "started_at", None),
        "state_dir": getattr(state, "state_dir", None),
        "bind_host": getattr(state, "bind_host", None),
        "preflight_warnings": list(getattr(state, "preflight_warnings", ()) or ()),
    }


def build_record(settings: Mapping[str, Any], runtime: Mapping[str, Any] | None = None) -> dict:
    """Everything the view prints, and nothing a credential could hide in."""
    runtime = runtime or {}
    groups = []
    for name, keys in GROUPS:
        rows = [config_row(key, settings, runtime) for key in keys]
        if name == "DEPLOYMENT & OWNERSHIP":
            rows = runtime_rows(runtime) + rows
        groups.append({"name": name, "note": _GROUP_NOTES.get(name, ""), "rows": rows})

    return {
        "authorities": {key: credential_state(key, settings.get(key)) for key in AUTHORITIES},
        "groups": groups,
        "unknown_keys": sorted(str(key) for key in settings if key not in config.DEFAULTS),
        "written_down": [{"item": item, "tables": list(tables)} for item, tables in WRITTEN_DOWN],
        "gone_on_restart": [
            {"item": item, "covers": list(covers)} for item, covers in GONE_ON_RESTART
        ],
    }


# ── the fragment ─────────────────────────────────────────────────────


def _banner(record: dict) -> str:
    off = [key for key in AUTHORITIES if record["authorities"][key] == OFF]
    if not off:
        return ""
    reading_open = "viewer_key" in off
    names = (
        "All three keys are" if len(off) == 3
        else " and ".join(off) + (" is" if len(off) == 1 else " are")
    )
    verbs = [_GUARDS[key] for key in AUTHORITIES if key in off]
    verb_text = verbs[0] if len(verbs) == 1 else ", ".join(verbs[:-1]) + " and " + verbs[-1]
    tone = "is-danger" if reading_open else "is-warn"
    gate = " data-gate-open" if reading_open else ""
    return f"""
      <div class="banner {tone} cf-banner"{gate} role="status">
        <span class="banner-dot" aria-hidden="true"></span>
        <div class="banner-text">
          <div class="banner-head">{esc(names)} off — anyone who can reach this address can {esc(verb_text)}</div>
          <div class="banner-body">Each key guards one thing and none stands in for another:
            <span class="mono">viewer_key</span> for reading, <span class="mono">node_secret</span>
            for joining, <span class="mono">pitch_key</span> for spending. Fine on your own
            machine; set all three before the address is reachable by anyone else. See
            <span class="mono">docs/DEPLOY.md</span>.</div>
        </div>
      </div>"""


_TONES = {"ok": " is-ok", "warn": " is-warn", "muted": " is-muted", "": ""}


def _row_html(row: dict) -> str:
    runtime = row["source"] == "runtime"
    key_class = "cf-key is-runtime" if runtime else "cf-key"
    hint = f' <span class="cf-hint">{esc(row["hint"])}</span>' if row["hint"] else ""
    note = f'<div class="cf-note">{row["note"]}</div>' if row["note"] else ""
    return f"""
          <div class="cf-row" data-key="{esc(row['key'])}">
            <span class="{key_class}">{_breakable(row['key'])}</span>
            <span class="cf-val"><span class="cf-v{_TONES[row['tone']]}">{esc(row['value'])}</span>{hint}</span>
            {note}
          </div>"""


def _group_panel(group: dict) -> str:
    source = "this process · config.json" if group["name"] == "DEPLOYMENT & OWNERSHIP" else "config.json"
    note = f'<div class="rd-panel-note cf-group-note">{esc(group["note"])}</div>' if group["note"] else ""
    rows = "".join(_row_html(row) for row in group["rows"])
    return f"""
      <section class="rd-panel cf-group" data-group="{esc(group['name'])}">
        <div class="rd-panel-head">
          <span class="rd-panel-label">{esc(group['name'])}</span>
          <span class="rd-panel-meta">{source}</span>
        </div>{note}{rows}
      </section>"""


def _durability_panels(record: dict) -> str:
    written = "".join(f"<li>{esc(entry['item'])}</li>" for entry in record["written_down"])
    gone = "".join(f"<li>{esc(entry['item'])}</li>" for entry in record["gone_on_restart"])
    return f"""
      <section class="rd-panel cf-durable" data-durability="written">
        <div class="rd-panel-head">
          <span class="cf-mark is-ok" aria-hidden="true"></span>
          <span class="rd-panel-label">WRITTEN DOWN</span>
          <span class="rd-panel-meta">survives a restart</span>
        </div>
        <ul class="cf-durable-list">{written}</ul>
        <div class="rd-panel-note">{_code('scripts/backup.py')} copies this set together with
          {_code('config.json')}, so a backup holds all three keys and wants the same care
          they do. Nothing schedules one.</div>
      </section>
      <section class="rd-panel cf-durable" data-durability="gone">
        <div class="rd-panel-head">
          <span class="cf-mark is-warn" aria-hidden="true"></span>
          <span class="rd-panel-label">GONE ON RESTART</span>
          <span class="rd-panel-meta">never resumed</span>
        </div>
        <ul class="cf-durable-list">{gone}</ul>
        <div class="rd-panel-note">None of it resumes. A restart marks what it could not
          finish as interrupted, so a run that says interrupted was not quietly dropped.</div>
      </section>"""


def _unknown_panel(record: dict) -> str:
    names = record["unknown_keys"]
    if not names:
        return ""
    items = "".join(f'<li class="cf-key">{_breakable(name)}</li>' for name in names)
    return f"""
      <section class="rd-panel cf-unknown" data-unknown-keys>
        <div class="rd-panel-head">
          <span class="rd-panel-label">NOT READ BY THIS VERSION</span>
          <span class="rd-panel-meta">{_plural(len(names), 'key')}</span>
        </div>
        <ul class="cf-durable-list">{items}</ul>
        <div class="rd-panel-note">{_code('config.json')} carries these and nothing reads them,
          so their values are not shown. A misspelt key is ignored without a word — a misspelt
          {_code('viewer_key')} leaves reading open.</div>
      </section>"""


def render(record: dict) -> str:
    """The fragment the console puts in `#config-body`."""
    groups = "".join(_group_panel(group) for group in record["groups"])
    return f"""
    <div class="rd cf" data-config>
      {_banner(record)}
      <p class="cf-lede">Read-only, as this process loaded it when it started: a change to
        {_code('config.json')} takes effect on restart. A key that authorises something shows
        only whether it is set — never its value, its length, or any part of it — and an
        address shows only its scheme, host and port.</p>
      <div class="rd-body">
        <div class="rd-main">{groups}
        </div>
        <div class="rd-side">{_durability_panels(record)}{_unknown_panel(record)}
        </div>
      </div>
    </div>"""
