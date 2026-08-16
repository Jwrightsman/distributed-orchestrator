"""
The /try page's two states.

`public_pitch` is off by default, which means the state most visitors see is
the closed one — so it is the state that has to be designed. A disabled button
explains nothing: it looks like the site is broken rather than deliberately
gated, and it gives someone who arrived from a post no reason to stay.

The closed panel says why the door is shut, shows what is behind it, and
offers the two real ways in: look at something the swarm already built, or
run a swarm yourself.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from config import get as get_config
from dashboard import render
from routes_run import esc
from server_state import OUTPUT_DIR, _PUBLIC_RATE_MAX, _PUBLIC_TASK_MAX

router = APIRouter()

_INVITE_URL = (
    "https://github.com/Jwrightsman/distributed-orchestrator"
    "/issues/new?template=join-the-network.yml"
)


def _latest_run() -> str:
    """The newest run on disk, for "here is one it already built"."""
    if not OUTPUT_DIR.exists():
        return ""
    for d in sorted(OUTPUT_DIR.iterdir(), reverse=True):
        if d.is_dir() and (d / "full_log.json").exists():
            return d.name
    return ""


def _open_panel() -> str:
    """The live form. Every control is labelled and every update announced."""
    return f"""
  <div class="box">
    <label class="sr-only" for="task">Describe the task you want built</label>
    <textarea id="task" maxlength="{_PUBLIC_TASK_MAX}"
              placeholder="e.g. Write a bash script that renames photos by their EXIF date"
              aria-describedby="limits"></textarea>
    <div class="row">
      <span class="counter"><span id="count">0</span>/{_PUBLIC_TASK_MAX}</span>
      <button class="go" id="go" type="button">Pitch it</button>
    </div>
    <p class="note" id="limits">Local models only, so expect minutes rather than
       seconds. Limit: {_PUBLIC_RATE_MAX} tasks an hour.</p>

    <!-- The one thing a screen reader must not miss: a run takes minutes, so
         every status change has to be spoken rather than only drawn. -->
    <p class="status-msg" id="msg" role="status" aria-live="polite"></p>

    <div class="stages" id="stages" role="status" aria-live="polite" hidden>
      <div class="stage" id="st-plan"><span class="dot" aria-hidden="true"></span>
        Planning <span class="detail" id="d-plan"></span></div>
      <div class="stage" id="st-build"><span class="dot" aria-hidden="true"></span>
        Building <span class="detail" id="d-build"></span></div>
      <div class="stage" id="st-review"><span class="dot" aria-hidden="true"></span>
        Reviewing &amp; assembling <span class="detail" id="d-review"></span></div>
    </div>

    <a class="btn btn-primary" id="run-link" href="#" hidden>Open this run's page &#8594;</a>
    <div class="result" id="result" tabindex="0" hidden></div>
  </div>"""


def _closed_panel(origin: str) -> str:
    latest = _latest_run()
    see_one = (
        f'<a class="btn btn-primary" href="/run/{esc(latest)}">See one it already built</a>'
        if latest else
        '<a class="btn btn-primary" href="/dashboard#gallery">See what the swarm has built</a>'
    )
    return f"""
  <div class="closed">
    <div class="closed-head">
      <span class="badge">INVITE ONLY</span>
      <span class="what">Public pitching is switched off on this orchestrator</span>
    </div>
    <div class="closed-body">
      <p>
        A pitch is not a chat message — it spends <b>real minutes of CPU on other
        people's computers</b>. This network is a handful of volunteer machines, so
        the door opens a few people at a time rather than to everyone at once.
      </p>
      <p>
        Nothing is hidden behind it. Every task the swarm has ever built is public,
        with the plan, the machines that did the work, and the reviewer's verdict.
      </p>

      <div class="would">
        <div class="would-step"><span class="n">1</span><p>You describe a task in plain English.</p></div>
        <div class="would-step"><span class="n">2</span><p>A <b>planner</b> splits it into a few smaller jobs.</p></div>
        <div class="would-step"><span class="n">3</span><p><b>Builders</b> write the pieces in parallel, each on a different machine.</p></div>
        <div class="would-step"><span class="n">4</span><p>A <b>reviewer</b> assembles and grades the result, and you get a permanent link to it.</p></div>
      </div>

      <div class="ways">
        {see_one}
        <a class="btn btn-ghost" href="{esc(_INVITE_URL)}" target="_blank" rel="noopener">Ask for an invite</a>
      </div>

      <p class="note is-spaced">Or skip the queue entirely — run the whole thing on your own machine:</p>
      <button class="cmd" id="cmd" type="button" title="Click to copy">
        <span class="p" aria-hidden="true">$</span><span id="cmd-text">python join.py {esc(origin)}</span>
      </button>
      <p class="note" id="cmd-note">Any machine with 8&nbsp;GB of RAM · it asks before installing anything</p>
    </div>
  </div>

  <script>
  (function () {{
    var cmd = document.getElementById('cmd');
    if (!cmd) return;
    cmd.addEventListener('click', function () {{
      var text = document.getElementById('cmd-text').textContent;
      if (navigator.clipboard) navigator.clipboard.writeText(text);
      document.getElementById('cmd-note').textContent =
        'Copied — it asks before installing anything';
    }});
  }})();
  </script>"""


@router.get("/try", response_class=HTMLResponse)
async def try_page(request: Request):
    origin = str(request.base_url).rstrip("/")
    is_open = bool(get_config().get("public_pitch", False))
    return render("try.html", PITCH_PANEL=_open_panel() if is_open else _closed_panel(origin))
