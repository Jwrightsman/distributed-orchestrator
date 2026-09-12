"""What this coordinator loaded, for the console's Config view."""

from fastapi import APIRouter, Request

import config_view
from access_control import require_viewer
from config import get as get_config

router = APIRouter()


@router.get("/v1/operator/config")
def operator_config(request: Request):
    """Every setting this process loaded, with credentials reduced to set or off.

    **Operator-gated, not viewer-gated.** Unlike `/evals`, nothing here is
    committed to a public repository: it says which authorities are open,
    which addresses this machine talks to, and what preflight warned about. So
    it lives under `/v1/operator/`, which `deploy/Caddyfile.public` refuses at
    the edge, and the view that reads it is in the console, itself refused
    there. `require_viewer` is called here as well, so the route is refused by
    its own handler and not only by the middleware.

    With `viewer_key` empty this route is readable by anyone who can reach the
    address, like every other private route. That is the state the view's own
    banner exists to name, and `/v1/operator/health` already serves the
    preflight warnings that say which authorities are off.

    **No credential value is in the response.** Not in the record and not in
    the fragment: `config_view` reduces them to words before either is built.
    """
    require_viewer(request)
    record = config_view.build_record(
        get_config(), config_view.runtime_facts(request.app.state)
    )
    return {**record, "config_html": config_view.render(record)}
