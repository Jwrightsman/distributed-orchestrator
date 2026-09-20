"""
The /try page's two states.

`public_pitch` is off by default, which means the state most visitors see is
the closed one — so it is the state that has to be designed. A disabled button
explains nothing: it looks like the site is broken rather than deliberately
gated, and it gives someone who arrived from a post no reason to stay.

The open state states the endpoint's real profile before anyone types. The
public endpoint does not split work or send it to other machines: it runs one
attempt of the direct strategy on this machine, under a hard deadline and an
output cap (routes_pitch.public_pitch). `PUBLIC_PROFILE` is that profile in
words, and a test holds it to the code that sets it.

Progress and the result are read from the share link the pitch returns. The
page is public, and /jobs and /history are private routes, so a page that
polled them showed nothing on any coordinator with a viewer key set.
"""

import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from config import get as get_config
from dashboard import render
from execution.publication import (
    LegacyRunNotPublished,
    require_legacy_run_publication,
)
from routes_run import esc
from server_state import OUTPUT_DIR, _PUBLIC_RATE_MAX, _PUBLIC_TASK_MAX

router = APIRouter()

_INVITE_URL = (
    "https://github.com/Jwrightsman/distributed-orchestrator"
    "/issues/new?template=join-the-network.yml"
)
_REPO_URL = "https://github.com/Jwrightsman/distributed-orchestrator"

# (value, label). Each value is a literal public_pitch sets; the test reads
# them out of routes_pitch.py.
PUBLIC_PROFILE = (
    ("1 attempt", "NO SPLITTING"),
    ("this machine", "NEVER SENT OUT"),
    ("120s", "HARD DEADLINE"),
    ("64 KB", "OUTPUT CAP"),
)


def _latest_run() -> str:
    """The newest run on disk, for "here is one it already built"."""
    if not OUTPUT_DIR.exists():
        return ""
    for d in sorted(OUTPUT_DIR.iterdir(), reverse=True):
        log_file = d / "full_log.json"
        if not d.is_dir() or not log_file.exists():
            continue
        try:
            log = json.loads(log_file.read_text(encoding="utf-8"))
            require_legacy_run_publication(d, log)
        except (json.JSONDecodeError, LegacyRunNotPublished, OSError):
            continue
        return d.name
    return ""


def _profile_strip() -> str:
    cells = "".join(
        f'<div class="prof"><span class="prof-v">{esc(v)}</span><span class="prof-k">{esc(k)}</span></div>'
        for v, k in PUBLIC_PROFILE
    )
    return f'<div class="profile" aria-label="What the public endpoint runs">{cells}</div>'


def _open_panel() -> str:
    """The live form. Every control is labelled and every update announced."""
    return f"""
  {_profile_strip()}
  <div class="panel">
    <div class="panel-head">
      <span class="panel-k">YOUR TASK</span>
      <span class="panel-meta">limit {_PUBLIC_RATE_MAX} / hour</span>
    </div>
    <label class="sr-only" for="task">Describe the task you want built</label>
    <textarea id="task" maxlength="{_PUBLIC_TASK_MAX}"
              placeholder="e.g. Write a bash script that renames photos by their EXIF date"
              aria-describedby="limits"></textarea>
    <div class="panel-foot">
      <span class="counter"><span id="count">0</span>/{_PUBLIC_TASK_MAX}</span>
      <button class="go" id="go" type="button" title="Runs one attempt on this machine · POST /public/pitch, rate-limited">Pitch it</button>
    </div>
  </div>
  <p class="note" id="limits">Local models only, so expect minutes rather than seconds.
    {_PUBLIC_RATE_MAX} tasks an hour from one address, {_PUBLIC_TASK_MAX} characters each.</p>

  <!-- The one thing a screen reader must not miss: a run takes minutes, so
       every status change has to be spoken rather than only drawn. -->
  <p class="status-msg" id="msg" role="status" aria-live="polite"></p>

  <div class="panel" id="progress" hidden>
    <ol class="stages" id="stages" aria-live="polite" aria-label="Where this attempt is">
      <li class="stage" id="st-accepted"><i aria-hidden="true"></i><span class="stage-k">ACCEPTED</span>
        <span class="stage-d">queued · one public task at a time per address</span><span class="stage-t" id="t-accepted"></span></li>
      <li class="stage" id="st-build"><i aria-hidden="true"></i><span class="stage-k">BUILD</span>
        <span class="stage-d" id="d-build">one attempt, running locally</span><span class="stage-t" id="t-build"></span></li>
      <li class="stage" id="st-checked"><i aria-hidden="true"></i><span class="stage-k">CHECKED</span>
        <span class="stage-d" id="d-checked">waiting for the attempt</span><span class="stage-t" id="t-checked"></span></li>
    </ol>
    <div class="result-wrap" id="result-wrap" hidden>
      <div class="panel-k">RESULT</div>
      <pre class="result" id="result" tabindex="0"></pre>
    </div>
  </div>
  <a class="btn btn-primary" id="run-link" href="#" hidden>Open the shared result &#8594;</a>
  <p class="note" id="share-note" hidden>That link works for one hour and hides which machine ran
    it. It reaches this one task and nothing else here. Structural checks mean the file parses:
    read generated code before you run it.</p>"""


def _closed_panel(origin: str, runs_public: bool) -> str:
    latest = _latest_run() if runs_public else ""
    see_one = (
        f'<a class="btn btn-primary" href="/run/{esc(latest)}">See one it already built</a>'
        if latest else
        '<a class="btn btn-primary" href="/">What this is</a>'
    )
    visibility = (
        "Nothing is hidden behind it. Every finished run on this coordinator has its own page, with "
        "the plan, the machines that did the work, and the reviewer's verdict."
        if runs_public else
        "Runs on this coordinator are private: its owner shares one deliberately, with a link that "
        "expires and can be revoked."
    )
    return f"""
  <div class="panel closed">
    <div class="panel-head">
      <span class="badge">INVITE ONLY</span>
      <span class="closed-what">Public pitching is switched off on this orchestrator</span>
    </div>
    <div class="closed-body">
      <p>A pitch is not a chat message: it spends <b>real minutes of CPU</b>. This network is a
        handful of invited machines, so the door opens a few people at a time rather than to
        everyone at once.</p>
      <p>{visibility}</p>
      <p>When it is open, a public pitch is deliberately small: one attempt, on this machine, under
        a two-minute deadline. It is never split up and never handed to anyone else's computer.</p>

      <div class="ways">
        {see_one}
        <a class="btn btn-ghost" href="{esc(_INVITE_URL)}" target="_blank" rel="noopener">Ask for an invite</a>
      </div>

      <p class="note is-spaced">Or run your own coordinator, on your own machine, with no one's
        permission. The repository's README walks through it:</p>
      <a class="cmd" href="{esc(_REPO_URL)}" target="_blank" rel="noopener" title="The repository">
        <span class="p" aria-hidden="true">$</span><span>git clone {esc(_REPO_URL)}</span>
      </a>
    </div>
  </div>"""


@router.get("/try", response_class=HTMLResponse)
async def try_page(request: Request):
    origin = str(request.base_url).rstrip("/")
    settings = get_config()
    is_open = bool(settings.get("public_pitch", False))
    # With no viewer key every run page is public; with one, none is.
    runs_public = not str(settings.get("viewer_key", "") or "").strip()
    return render(
        "try.html",
        PITCH_PANEL=_open_panel() if is_open else _closed_panel(origin, runs_public),
    )
