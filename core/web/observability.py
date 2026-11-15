"""Who may open Grafana, asked by the edge proxy before every request to it.

Grafana lives at `/grafana/` on the pages' host, so the browser sends it the
same `__Host-session` cookie the pages read. Traefik's forwardAuth middleware
hands each request's headers to `GRAFANA_AUTH_PATH` first, and only a 2xx
from here lets the request through. The 2xx carries `GRAFANA_USER_HEADER`,
which Traefik copies onto the request it forwards; Grafana runs in auth-proxy
mode and believes that header, and nothing else, about who is calling.

Three things keep that header honest, and each is checked in
`tests/test_grafana_access.py`:

- Traefik removes any copy the client sent before it asks here, so a
  browser cannot name itself;
- Grafana accepts the header only from the subnet it shares with Traefik
  alone (`GF_AUTH_PROXY_WHITELIST`), so another container on the server
  cannot either;
- this address is outside `/web/`, `/pje/` and `/grafana/`, so no public
  route reaches it; Traefik calls it over the internal network.

The person is read again on every request, as on every page: a demotion or a
deactivation closes Grafana on the next click, not when a session ages out.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse

from core.config import settings
from core.repositories.connection import get_pool
from core.services import authorization
from core.web.pages import HEADERS, page
from core.web.staff_web import AppEntryRefused, SessionLocked, current_staff

logger = logging.getLogger(__name__)

router = APIRouter(tags=["observability"])

GRAFANA_AUTH_PATH = "/internal/grafana/auth"
GRAFANA_USER_HEADER = "X-WEBAUTH-USER"
ACTION = "observability.view"


def grafana_user(staff_id: int) -> str:
    """The login Grafana gets: the staff id, never the name. A name with an
    accent does not survive an HTTP header, and Grafana needs no name."""
    return f"staff-{staff_id}"


def _sign_in_first(request: Request) -> Response:
    """A page load goes to the pages' home, which explains how to sign in or
    asks for Face ID; Grafana's own scripts get a bare 401."""
    if request.headers.get("sec-fetch-mode") == "navigate":
        return RedirectResponse("/web/", status_code=303, headers=HEADERS)
    return Response(status_code=401, headers=HEADERS)


@router.get(GRAFANA_AUTH_PATH, include_in_schema=False)
async def grafana_auth(request: Request) -> Response:
    if not settings.web_enabled:
        return Response(status_code=404, headers=HEADERS)
    pool = await get_pool()
    async with pool.connection() as conn:
        try:
            signed_in = await current_staff(conn, request)
        except (SessionLocked, AppEntryRefused):
            signed_in = None
    if signed_in is None:
        return _sign_in_first(request)
    staff, _session = signed_in
    decision = authorization.decide(staff, ACTION)
    if not decision.allowed:
        logger.info("Grafana refused", extra={"staff_id": staff.id,
                                              "action": ACTION})
        return page("Acesso restrito",
                    "<h1 class=bad>Acesso restrito</h1>"
                    "<p>Os painéis de monitoramento são só do owner. "
                    "<a href='/web/'>Voltar ao painel</a>.</p>", status=403)
    return Response(status_code=204,
                    headers={**HEADERS, GRAFANA_USER_HEADER: grafana_user(staff.id)})


__all__ = ["ACTION", "GRAFANA_AUTH_PATH", "GRAFANA_USER_HEADER", "grafana_user", "router"]
