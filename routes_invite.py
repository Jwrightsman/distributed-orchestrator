"""How a machine joins and how an app pitches, for the console's Connect view."""

from fastapi import APIRouter, Request

import invite_view
from access_control import require_viewer
from config import get as get_config

router = APIRouter()


@router.get("/v1/operator/invite")
def operator_invite(request: Request):
    """The Connect view, with its warnings read from this coordinator's settings.

    **Operator-gated, for the reason `/v1/operator/config` is.** The warning
    panel says which authorities are off and whether HTTPS is declared, which
    is the same exposure the Config view carries. So it lives under
    `/v1/operator/`, refused at the edge by `deploy/Caddyfile.public`, and the
    handler calls `require_viewer` itself.

    **No credential value is in the response.** A key becomes `off`, `set` or
    `set · short` through `config_view.credential_state` before the record
    exists.
    """
    require_viewer(request)
    record = invite_view.build_record(get_config())
    return {**record, "invite_html": invite_view.render(record)}
