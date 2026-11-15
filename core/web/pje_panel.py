"""PJe enrolment from the panel: the page the panel's `/pje` answer leads to.

On Telegram, `/pje` answers with a single-use link to the credential form
(`core/web/pje_enrolment.py`). The panel cannot do that: it records every
turn it answers, and a live enrolment token must never sit in the
conversation. So the panel's answer names this page, which holds nothing,
and the token is minted here, behind the signed-in session.

Looking creates nothing: the GET shows a button. The POST, checked for
another site and for the session's form token, opens the enrolment exactly
as the bot does (`pje.open_enrolment`) and sends the browser to the form.
Who may is decided by action name (`pje.connect`), on the staff row read
again for this request. Behind `WEB_ASSISTANT`, as the assistant's page.
"""
from __future__ import annotations

import html
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from core.config import settings
from core.models import StaffMember
from core.repositories.connection import get_pool
from core.services import authorization, pje
from core.web.pages import (
    HEADERS,
    from_another_site,
    page,
    read_form,
    refused_from_another_site,
    remember_theme,
)
from core.web.staff_web import (
    COOKIE,
    app_lock_meta,
    assistant_open_to,
    csrf_for,
    csrf_matches,
    current_staff,
    nav,
    signed_out,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix=pje.PANEL_PAGE.rstrip("/"), tags=["web"],
                   dependencies=[Depends(remember_theme)])

# A form token and the form overhead.
_MAX_BODY = 1024

_NOT_CONFIGURED = ("O acesso ao PJe ainda não está configurado neste servidor. "
                   "Avise o administrador.")
_NOT_OPENED = "Não consegui gerar o link agora. Tente em instantes."


def _switched_on() -> None:
    """Off, this address does not exist, as the assistant's own."""
    if not settings.web_enabled or settings.web_assistant == "off":
        raise HTTPException(status_code=404)


async def _enter(conn, request: Request) -> tuple[StaffMember, str] | Response:
    """The person, re-read for this request, or the answer for nobody, for
    somebody outside the assistant's switch, or for a rank without
    `pje.connect`."""
    signed_in = await current_staff(conn, request)
    if signed_in is None:
        return signed_out(request)
    staff, session = signed_in
    if not assistant_open_to(staff):
        raise HTTPException(status_code=404)
    decision = authorization.decide(staff, "pje.connect")
    if not decision.allowed:
        return _said(staff, session, "Acesso ao PJe", decision.reason, status=403)
    if not settings.pje_enabled:
        return _said(staff, session, "Acesso ao PJe", _NOT_CONFIGURED, status=404)
    return staff, session


def _said(staff: StaffMember, session: str, title: str, text: str, *,
          status: int) -> HTMLResponse:
    return page(title, f"<h1>{html.escape(title)}</h1><p class=bad>{html.escape(text)}</p>",
                status=status, head=app_lock_meta(staff), navigation=nav(staff, session))


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def enrolment_page(request: Request) -> Response:
    """A button, and nothing opened: an enrolment exists only after the POST."""
    _switched_on()
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _enter(conn, request)
    if isinstance(entered, Response):
        return entered
    staff, session = entered
    court = html.escape(settings.pje_court)
    return page(
        "Cadastrar acesso ao PJe",
        f"<h1>Cadastrar seu acesso ao {court}</h1>"
        "<p class=sub>Continue para a página de cadastro. Ela vale uma vez só e "
        f"expira em {pje.enrolment_minutes()} minutos.</p>"
        f"<form method=post action='{pje.PANEL_PAGE}'>"
        f"<input type=hidden name=csrf value='{csrf_for(session)}'>"
        "<button type=submit>Continuar para o cadastro</button></form>"
        "<p class=note>Digite a senha só na página de cadastro, nunca no chat. Se "
        "você já tem um acesso cadastrado, o novo substitui o anterior. Para "
        "remover, envie <strong>/pje remover</strong> no assistente.</p>",
        head=app_lock_meta(staff), navigation=nav(staff, session),
    )


@router.post("/", include_in_schema=False)
async def open_enrolment(request: Request) -> Response:
    """Open the enrolment, once per press, and go to its form. The token
    travels only in this answer's address, which is never cached and never
    sent on as a referrer."""
    _switched_on()
    if from_another_site(request):
        return refused_from_another_site()
    form = await read_form(request, max_bytes=_MAX_BODY)
    if not csrf_matches(request.cookies.get(COOKIE) or "", form.get("csrf", "")):
        return page("Pedido recusado",
                    "<h1 class=bad>Pedido recusado</h1>"
                    "<p>Recarregue a página e tente de novo.</p>", status=403)
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _enter(conn, request)
        if isinstance(entered, Response):
            return entered
        staff, session = entered
        try:
            token = await pje.open_enrolment(conn, staff.id)
        except Exception:
            logger.exception("Could not open a PJe enrolment from the panel")
            return _said(staff, session, "Acesso ao PJe", _NOT_OPENED, status=503)
    return RedirectResponse(f"/pje/{token}", status_code=303, headers=HEADERS)


__all__ = ["router"]
