"""The integrations page: what this person connected the agent to.

One card per service: PJe, Microsoft, DJEN and Telegram. Each says whether
it is connected, since when and when last used, and connects or disconnects
it here. Every card is decided by action name on the staff row read again
for this request; every change is a POST from this site with the form token,
and the person is always the session's, never the form's.

Microsoft is connected with the device-code flow, driven by the page
(`core/services/microsoft_link.py`): the code to type is shown here, the
code to poll with stays on the server, sealed.
"""
from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from core.config import settings
from core.models import StaffMember
from core.repositories import microsoft_accounts, pje_credentials, publications
from core.repositories.connection import get_pool
from core.schedule import local_now
from core.services import authorization, microsoft_link, pje
from core.integrations import datajud
from core.web import actions
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

HOME = "/web/integracoes/"
router = APIRouter(prefix=HOME.rstrip("/"), tags=["web"],
                   dependencies=[Depends(remember_theme)])

# A form token, a confirmation and the form overhead.
_MAX_BODY = 1024
SCRIPT = f"{HOME}integrations.js"
_SCRIPT_BYTES = (Path(__file__).parent / "static" / "integrations.js").read_bytes()
# This page runs its own script and asks this host how the sign-in went.
POLICY = HEADERS["Content-Security-Policy"] + "; script-src 'self'; connect-src 'self'"

NOTES = {
    "pje_removido": ("Acesso ao PJe removido. Se quiser, troque a senha no próprio PJe: "
                     "é o único jeito de invalidar o que já foi usado."),
    "pje_nada": "Você não tem acesso ao PJe cadastrado.",
    "ms_conectada": "Conta Microsoft conectada.",
    "ms_desconectada": ("Conta Microsoft desconectada. Para cancelar a autorização na "
                        "própria Microsoft, use a página de segurança da sua conta."),
    "ms_nada": "Você não tem conta Microsoft conectada.",
    "ms_aguardando": ("Ainda não vi a autorização. Termine no site da Microsoft e toque "
                      "em Já autorizei de novo."),
    "ms_recusada": "A conexão não foi concluída. Peça um novo código.",
    "ms_sem_codigo": "O código expirou. Peça um novo código.",
    "ms_erro": "A Microsoft não respondeu agora. Tente de novo em instantes.",
    "ms_ja_conectada": "Você já tem uma conta Microsoft conectada.",
    "ms_indisponivel": "A conexão com a Microsoft não está disponível neste servidor.",
    "refused": "Recarregue a página e tente de novo.",
}

STYLE = """
.cards{display:grid;gap:1.2rem;margin-top:2rem;grid-template-columns:repeat(auto-fill,minmax(20rem,1fr))}
.card{padding:1.2rem 1.3rem;border:1px solid var(--line);border-radius:var(--radius-lg);
 background:var(--panel);display:grid;gap:.8rem;align-content:start;min-width:0}
.card h2{margin:0;font-size:1.05rem}
.card p{margin:0;overflow-wrap:anywhere}
.card .state{color:var(--mid);font-size:.92rem}
.card .state.on{color:var(--text)}
.card form{margin:0}
.card .row{display:flex;flex-wrap:wrap;gap:.6rem}
.card button{width:auto;margin:0}
.card .code{font-size:1.4rem;letter-spacing:.12em;font-weight:600}
.card .follows{list-style:none;margin:0;padding:0;display:grid;gap:.5rem}
.card .follows li{display:flex;align-items:center;justify-content:space-between;gap:.8rem}
.card .follows button{flex:none;white-space:nowrap;min-height:2.4rem;padding:.4rem .9rem}
.card details>summary{cursor:pointer;padding:.3rem 0}
.card details form{display:grid;gap:.7rem;padding:.6rem 0}
.card label{display:grid;gap:.3rem;font-size:.85rem}
.card input,.card select{margin:0}
.card .hint{font-size:.85rem;color:var(--muted)}
"""


def _switched_on() -> None:
    if not settings.web_enabled:
        raise HTTPException(status_code=404)


async def _enter(conn, request: Request, *,
                 poll: bool = False) -> tuple[StaffMember, str] | Response:
    """The person, re-read for this request, or the answer for nobody. The
    script's poll is a look, not use: it does not move the lock clock."""
    signed_in = await current_staff(conn, request, activity=not poll)
    if signed_in is None:
        if poll:
            return PlainTextResponse("ended", status_code=401, headers=HEADERS)
        return signed_out(request)
    return signed_in


async def _posted(request: Request) -> dict[str, str] | Response:
    if from_another_site(request):
        return refused_from_another_site()
    form = await read_form(request, max_bytes=_MAX_BODY)
    if not csrf_matches(request.cookies.get(COOKIE) or "", form.get("csrf", "")):
        return page("Pedido recusado", "<h1 class=bad>Pedido recusado</h1>"
                    f"<p>{NOTES['refused']}</p>", status=403)
    return form


def _back(note: str = "") -> Response:
    return RedirectResponse(f"{HOME}?aviso={note}" if note else HOME, status_code=303)


def _allowed(staff: StaffMember, action: authorization.Action) -> bool:
    return authorization.decide(staff, action).allowed


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _day(when: datetime | None) -> str:
    return f"{local_now(when):%d/%m/%Y}" if when else ""


def _button(csrf: str, path: str, label: str, *, css: str = "quiet",
            extra: str = "") -> str:
    return (f"<form method=post action='{HOME}{path}'>"
            f"<input type=hidden name=csrf value='{_e(csrf)}'>{extra}"
            f"<button type=submit class={css}>{_e(label)}</button></form>")


def _card(title: str, body: str) -> str:
    return f"<section class=card><h2>{_e(title)}</h2>{body}</section>"


_UNAVAILABLE = "<p class=state>Não disponível neste servidor.</p>"


def _pje_card(staff: StaffMember, csrf: str, credential) -> str:
    if not settings.pje_enabled:
        return _card("PJe", _UNAVAILABLE)
    if credential is not None:
        used = (f" · último uso {_day(credential.last_used_at)}"
                if credential.last_used_at else "")
        state = (f"<p class='state on'>Cadastrado em {_day(credential.enrolled_at)}"
                 f"{used}</p>")
    else:
        state = "<p class=state>Não cadastrado.</p>"
    if assistant_open_to(staff):
        enrol = (f"<form method=post action='{pje.PANEL_PAGE}'>"
                 f"<input type=hidden name=csrf value='{_e(csrf)}'>"
                 f"<button type=submit>{'Substituir' if credential else 'Cadastrar'}"
                 "</button></form>")
    else:
        enrol = "<p class=hint>Cadastre pelo Telegram com /pje.</p>"
    remove = _button(csrf, "pje/remover", "Remover") if credential is not None else ""
    return _card("PJe", f"{state}<div class=row>{enrol}{remove}</div>"
                 "<p class=hint>A senha é digitada só na página de cadastro, "
                 "nunca no chat.</p>")


def _microsoft_card(csrf: str, account, sign_in) -> str:
    title = "Microsoft (OneDrive)"
    if account is not None:
        used = f" · último uso {_day(account.last_used_at)}" if account.last_used_at else ""
        return _card(title,
                     f"<p class='state on'>Conectada: {_e(account.account_name)}</p>"
                     f"<p class=hint>Desde {_day(account.connected_at)}{used}</p>"
                     + _button(csrf, "microsoft/desconectar", "Desconectar"))
    if not microsoft_link.available():
        return _card(title, _UNAVAILABLE)
    if sign_in is not None:
        uri = sign_in.verification_uri
        where = (f"<a href='{_e(uri)}' target=_blank rel='noopener noreferrer'>{_e(uri)}</a>"
                 if uri.startswith("https://") else _e(uri))
        left = max(1, int((sign_in.expires_at - local_now()).total_seconds() // 60))
        return (f"<section class=card data-ms-wait data-intervalo={sign_in.interval_seconds} "
                f"data-expira={int(sign_in.expires_at.timestamp())} "
                f"data-csrf='{_e(csrf)}'><h2>{_e(title)}</h2>"
                f"<p>Abra {where} e digite o código:</p>"
                f"<p class=code>{_e(sign_in.user_code)}</p>"
                f"<p class=hint>Vale por mais {left} min. Entre com a sua conta e autorize; "
                "a sua senha é digitada só no site da Microsoft.</p>"
                "<p class=hint data-ms-status role=status></p>"
                f"<div class=row>{_button(csrf, 'microsoft/verificar', 'Já autorizei', css='primary')}"
                f"{_button(csrf, 'microsoft/conectar', 'Novo código')}</div></section>")
    return _card(title, "<p class=state>Não conectada.</p>"
                 "<p class=hint>Para o agente ler e trazer os seus arquivos do OneDrive. "
                 "Você autoriza no site da Microsoft e pode revogar quando quiser.</p>"
                 + _button(csrf, "microsoft/conectar", "Conectar", css="primary"))


def _djen_card(staff: StaffMember, csrf: str, oabs: list[str], processes: list[str]) -> str:
    title = "DJEN (publicações)"
    if assistant_open_to(staff):
        offered: tuple[tuple[actions.ActionKey, str], ...] = (
            ("oab", "Acompanhar minha OAB"), ("acompanhar", "Acompanhar processo"))
        forms = "".join(
            f"<details><summary>{_e(label)}</summary>{actions.action_form(key, csrf)}</details>"
            for key, label in offered)
        return _card(title, "<p class=hint>Aviso de toda publicação nova no DJEN para o "
                     "que você acompanha. A confirmação chega no Assistente.</p>"
                     + actions.follows(csrf, oabs, processes) + forms)
    items = [f"OAB {oab}" for oab in oabs]
    items += [f"Processo {datajud.format_process_number(n)}" for n in processes]
    listed = ("<ul class=follows>" + "".join(f"<li>{_e(item)}</li>" for item in items)
              + "</ul>") if items else "<p class=state>Você ainda não acompanha nada.</p>"
    return _card(title, listed + "<p class=hint>Gerencie pelo Telegram: /oab, /acompanhar, "
                 "/desacompanhar.</p>")


def _telegram_card(staff: StaffMember) -> str:
    state = ("<p class='state on'>Ligado ao bot.</p>" if staff.chat_id
             else "<p class=state>Peça acesso pelo bot do escritório no Telegram.</p>")
    return _card("Telegram", state + "<p class=hint>É por onde chegam os avisos e os links "
                 "de acesso a este painel.</p>")


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def integrations_page(request: Request, aviso: str = "") -> Response:
    _switched_on()
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _enter(conn, request)
        if isinstance(entered, Response):
            return entered
        staff, session = entered
        credential = (await pje_credentials.get_credential(conn, staff.id)
                      if _allowed(staff, "pje.connect") else None)
        account = sign_in = None
        if _allowed(staff, "microsoft.connect"):
            account = await microsoft_accounts.get_account(conn, staff.id)
            sign_in = await microsoft_accounts.live_sign_in(conn, staff.id)
        oabs, processes = await publications.list_own_watches(conn, staff.id)
    csrf = csrf_for(session)
    cards = []
    if _allowed(staff, "pje.connect"):
        cards.append(_pje_card(staff, csrf, credential))
    if _allowed(staff, "microsoft.connect"):
        cards.append(_microsoft_card(csrf, account, sign_in))
    if _allowed(staff, "publications.watch"):
        cards.append(_djen_card(staff, csrf, oabs, processes))
    cards.append(_telegram_card(staff))
    note = (f"<p class=note role=status>{_e(NOTES[aviso])}</p>" if aviso in NOTES else "")
    body = (f"<h1>Integrações</h1><p class=sub>O que o agente acessa em seu nome. "
            f"Cada conexão é só sua.</p>{note}<div class=cards>{''.join(cards)}</div>"
            f"<script type=module src='{SCRIPT}'></script>")
    return page("Integrações", body, headers={"Content-Security-Policy": POLICY},
                wide=True, style=STYLE, head=app_lock_meta(staff),
                navigation=nav(staff, session))


@router.get("/integrations.js", include_in_schema=False)
async def script() -> Response:
    _switched_on()
    return Response(_SCRIPT_BYTES, media_type="text/javascript; charset=utf-8",
                    headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})


@router.post("/pje/remover", include_in_schema=False)
async def remove_pje(request: Request) -> Response:
    _switched_on()
    form = await _posted(request)
    if isinstance(form, Response):
        return form
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _enter(conn, request)
        if isinstance(entered, Response):
            return entered
        staff, session = entered
        if not _allowed(staff, "pje.connect"):
            return _back("refused")
        if form.get("confirmar") != "1":
            csrf = csrf_for(session)
            return page(
                "Remover acesso ao PJe",
                "<h1>Remover o seu acesso ao PJe?</h1>"
                "<p>O agente deixa de consultar as suas intimações e autos. Você pode "
                "cadastrar de novo quando quiser.</p>"
                + _button(csrf, "pje/remover", "Remover",
                          extra="<input type=hidden name=confirmar value=1>")
                + f"<p><a href='{HOME}'>Cancelar</a></p>",
                head=app_lock_meta(staff), navigation=nav(staff, session))
        removed = await pje_credentials.forget_credential(conn, staff.id)
    return _back("pje_removido" if removed else "pje_nada")


@router.post("/microsoft/conectar", include_in_schema=False)
async def connect_microsoft(request: Request) -> Response:
    _switched_on()
    form = await _posted(request)
    if isinstance(form, Response):
        return form
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _enter(conn, request)
        if isinstance(entered, Response):
            return entered
        staff, _session = entered
        if not _allowed(staff, "microsoft.connect"):
            return _back("refused")
        started = await microsoft_link.begin(conn, staff)
    if isinstance(started, str):
        return _back(f"ms_{started}")
    return _back()


@router.post("/microsoft/verificar", include_in_schema=False)
async def check_microsoft(request: Request) -> Response:
    _switched_on()
    form = await _posted(request)
    if isinstance(form, Response):
        return form
    by_script = form.get("via") == "script"
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _enter(conn, request, poll=by_script)
        if isinstance(entered, Response):
            return entered
        staff, _session = entered
        if not _allowed(staff, "microsoft.connect"):
            return _back("refused")
        outcome = await microsoft_link.check(conn, staff)
    if by_script:
        return PlainTextResponse(outcome, headers=HEADERS)
    return _back(f"ms_{outcome}")


@router.post("/microsoft/desconectar", include_in_schema=False)
async def disconnect_microsoft(request: Request) -> Response:
    _switched_on()
    form = await _posted(request)
    if isinstance(form, Response):
        return form
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _enter(conn, request)
        if isinstance(entered, Response):
            return entered
        staff, _session = entered
        if not _allowed(staff, "microsoft.connect"):
            return _back("refused")
        removed = await microsoft_accounts.disconnect(conn, staff.id)
    return _back("ms_desconectada" if removed else "ms_nada")


__all__ = ["HOME", "NOTES", "router"]
