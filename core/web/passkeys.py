"""Face ID: a passkey enrolled on a device, signing in and unlocking with it.

Three ceremonies, each a pair of POSTs from `static/passkey.js`: one asks for
the options (with a challenge stored only hashed, spent once), the other hands
back what the device signed. py_webauthn verifies every answer against the
configured panel origin and its host as RP ID, always with user verification
required: its own default for a sign-in is not to require it, and a passkey
without Face ID is a factor nobody confirmed.

- **Enrolling** is open only to a session opened by link and authenticator
  minutes ago (`enrolment_window_open` decides, in SQL). A passkey session
  cannot add another passkey, and a stolen cookie cannot add its thief's
  device once the window has passed. The person is told on Telegram.
- **Signing in** names nobody until the credential does. The person's staff
  row is then read again, and their refusals share the authenticator's lock.
  The session it opens follows the device (`devices.is_phone`): on a phone
  the app's rule, 5 h and locked after 15 min; on a computer the browser's.
- **Unlocking** is the only thing a locked session may do, and only with a
  passkey of the same person.
- **Notices** (`/web/avisos/`): the installed app subscribes to Web Push from
  an unlocked app session only, and the subscription takes that session's
  passkey. A computer's passkey session lives by the browser rule and gets no
  push in this version. Off, or without the VAPID keys, these addresses do
  not exist either.

Every answer is JSON, refusals as `{"erro": ...}` with the copy the person
reads: a redirect answered to `fetch` would not move the page, so the script
moves it itself with `{"ir": ...}`. Behind `WEB_APP`: off, or with the pages
off, every address here answers like one that does not exist.
"""
from __future__ import annotations

import base64
import html
import json
import logging
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from cryptography.hazmat.primitives.asymmetric import ec
from psycopg import errors as pg_errors
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import parse_client_data_json
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from core.config import pages_base_url, push_ready, settings
from core.integrations import telegram
from core.models import StaffMember
from core.repositories import push_subscriptions, web_access
from core.repositories.connection import get_pool
from core.repositories.staff import ROLE_INTERN, get_staff_by_id, has_rank
from core.services import second_factor
from core.web.devices import is_phone, request_looks_like_phone
from core.web.pages import HEADERS, from_another_site, page, read_form, remember_theme
from core.web.staff_web import (
    COOKIE,
    FACE_ID_POLICY,
    FACE_ID_REFUSED,
    FACE_ID_SCRIPT,
    FACE_ID_STYLE,
    KEY_REFUSED,
    NOTICES_STYLE,
    SessionLocked,
    _safe_return,
    _set_cookie,
    cookie_seconds,
    csrf_for,
    csrf_matches,
    current_staff,
    nav,
    offers_notices,
    refusal_attribute,
    signed_out,
    web_app_open_to,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/web", tags=["web"], dependencies=[Depends(remember_theme)])

_NOTICES_BYTES = (Path(__file__).parent / "static" / "notices.js").read_bytes()


@router.get("/notices.js", include_in_schema=False)
async def notices_script() -> Response:
    _switched_on()
    return Response(_NOTICES_BYTES, media_type="text/javascript; charset=utf-8",
                    headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})


# A registration with its attestation is under 2 KiB; this leaves room and
# no more. The sign-in endpoints are public, and Starlette buffers the body.
_MAX_BODY = 16 * 1024
_RP_NAME = "Demo & Associados"

# What the person reads ("Errors" in the design), word for word.
WINDOW_PASSED = ("Para ativar o Face ID, entre de novo pelo link do /web e ative "
                 "logo em seguida.")
TOO_MANY = (f"Este acesso já tem {web_access.MAX_PASSKEYS} aparelhos com Face ID. "
            f"Remova um em Aparelhos para ativar este.")
TOLD = ("Face ID ativado em um aparelho novo para o seu acesso ao painel. Se não foi "
        "você, mande /web encerrar agora.")
TOLD_COMPUTER = ("Chave de acesso ativada em um computador para o seu acesso ao painel. "
                 "Se não foi você, mande /web encerrar agora.")
# The same refusals on a computer, which has a key and no Face ID. Chosen by
# the User-Agent of the request, like the page the person is on: wording only.
KEY_WINDOW_PASSED = ("Para ativar a chave de acesso, entre de novo pelo link do /web e "
                     "ative logo em seguida.")
KEY_TOO_MANY = (f"Este acesso já tem {web_access.MAX_PASSKEYS} chaves de acesso. "
                f"Remova uma em Aparelhos para ativar esta.")
_ON_A_COMPUTER = {FACE_ID_REFUSED: KEY_REFUSED, WINDOW_PASSED: KEY_WINDOW_PASSED,
                  TOO_MANY: KEY_TOO_MANY}

_SCRIPT_BYTES = (Path(__file__).parent / "static" / "passkey.js").read_bytes()


def _switched_on() -> None:
    """Off, these addresses do not exist: answered before any connection."""
    if not settings.web_enabled or settings.web_app == "off":
        raise HTTPException(status_code=404)


def _relying_party() -> tuple[str, str]:
    """The exact origin a device must have answered from, and its host as
    the RP ID. Without a configured address there is nothing to check
    against, which is a deployment error, said as one."""
    parts = urlsplit(pages_base_url() or "")
    if parts.scheme not in ("https", "http") or not parts.hostname:
        logger.error("Face ID needs WEB_BASE_URL (or PUBLIC_BASE_URL) to be set")
        raise HTTPException(status_code=503)
    return f"{parts.scheme}://{parts.netloc}", parts.hostname


def _refused(request: Request, message: str = FACE_ID_REFUSED,
             status: int = 400) -> JSONResponse:
    """A refusal in the words of the device asking: Face ID on a phone, the
    key on a computer."""
    phone = request_looks_like_phone(request)
    answer = {"erro": message if phone else _ON_A_COMPUTER.get(message, message)}
    if message == TOO_MANY:
        answer["aparelhos"] = "/web/aparelhos/"
    return JSONResponse(answer, status_code=status, headers=HEADERS)


def _go(where: str) -> JSONResponse:
    return JSONResponse({"ir": where}, headers=HEADERS)


def _options(options: Any) -> Response:
    return Response(options_to_json(options), media_type="application/json",
                    headers=HEADERS)


async def _read_json(request: Request, max_bytes: int = _MAX_BODY) -> dict[str, Any] | None:
    """The posted JSON object, or None. Capped as it arrives, like
    `read_form`: never more than the cap is held."""
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > max_bytes:
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            return None
        chunks.append(chunk)
    try:
        body = json.loads(b"".join(chunks) or b"{}")
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


async def _posted(request: Request, *, with_session: bool) -> dict[str, Any] | Response:
    """The body of a ceremony POST, or the refusal: another site, a body
    over the cap or not JSON, and, where a session asks, a form token that
    is not this session's. All before any connection is taken."""
    if from_another_site(request, without_session=not with_session):
        return _refused(request, status=403)
    body = await _read_json(request)
    if body is None:
        return _refused(request)
    if with_session and not csrf_matches(request.cookies.get(COOKIE) or "",
                                         _text(body, "csrf")):
        return _refused(request, status=403)
    return body


def _text(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    return value if isinstance(value, str) else ""


def _touch_points(body: dict[str, Any]) -> int | None:
    """The page's `navigator.maxTouchPoints`, or None for anything else.
    `type() is int` and not `isinstance`: JSON's true and false arrive as
    bools, which are ints to Python, and a Mac with `true` (1) would pass
    for a computer."""
    value = body.get("toque")
    if type(value) is int and 0 <= value <= 32:
        return value
    return None


def _challenge_of(credential: dict[str, Any]) -> bytes | None:
    """The challenge the device signed, read from its client data, so the
    stored one can be found and spent. Verification checks it again."""
    try:
        raw = credential["response"]["clientDataJSON"]
        return parse_client_data_json(base64url_to_bytes(raw)).challenge
    except Exception:
        return None


def _credential_id(credential: dict[str, Any]) -> bytes | None:
    try:
        return base64url_to_bytes(credential["rawId"])
    except Exception:
        return None


def _usable(staff: StaffMember | None) -> bool:
    """As `current_staff` decides: active, and holding a rank."""
    return staff is not None and has_rank(staff.role, ROLE_INTERN)


async def _count_failure(conn, staff_id: int) -> None:
    """A refused answer from a known credential counts toward the same lock
    as a wrong authenticator code."""
    locked_now = await web_access.record_failure(
        conn, staff_id, limit=second_factor.MAX_FAILURES,
        lock_seconds=second_factor.LOCK_SECONDS,
    )
    if locked_now:
        await second_factor.tell_about_lock(conn, staff_id, face_id=True)


# ── the page and its script ─────────────────────────────────────────────────

_FIRST_USE = (
    "<h2>Primeira vez neste aparelho?</h2>"
    "<ol class=steps>"
    "<li>No Telegram, mande <strong>/web</strong> e entre pelo link no Safari, com o "
    "código do autenticador.</li>"
    "<li>Na página inicial, toque em <strong>Ativar Face ID neste aparelho</strong>.</li>"
    "<li>No Safari, toque em Compartilhar e em <strong>Adicionar à Tela de "
    "Início</strong>.</li>"
    "<li>Abra o painel pelo ícone na Tela de Início e toque em <strong>Entrar com "
    "Face ID</strong>.</li></ol>"
)


# The computer's form. The key signs in from a tab and from the installed
# app alike; the script shows the Dock note only in Safari's Dock app, which
# keeps its own cookies, so a Telegram link (opened in Safari) never reaches it.
_DOCK_NOTE = ("Neste app do Safari, entre com a chave de acesso. O código funciona no "
              "Chrome, no Edge ou numa aba do Safari.")
_FIRST_USE_COMPUTER = (
    "<h2>Primeira vez neste computador?</h2>"
    "<ol class=steps>"
    "<li>No Telegram, mande <strong>/web</strong> e entre pelo link com o código do "
    "autenticador.</li>"
    "<li>Na página inicial, clique em <strong>Ativar a chave de acesso deste "
    "computador</strong>.</li>"
    "<li>Instale o painel: no Chrome ou no Edge, clique no ícone de instalar na barra "
    "de endereço; no Safari, use <strong>Arquivo &gt; Adicionar ao Dock</strong>.</li>"
    "<li>Abra o painel pelo ícone e entre com a chave de acesso, ou com o código no "
    "Chrome ou no Edge.</li></ol>"
)


@router.get("/faceid/", response_class=HTMLResponse, include_in_schema=False)
async def face_id_page(request: Request, volta: str = "") -> Response:
    """Unlock for a locked passkey session; sign-in for anyone else, in the
    phone's form or the computer's, by this request's User-Agent.

    Looking is not use: the session is read without moving its lock clock.
    On a phone the sign-in button shows only in the installed app (the
    script decides, by display mode); on a computer it shows in a tab too.
    The first-use steps always show below it: before a ceremony nobody here
    knows whether this person has a passkey.
    """
    _switched_on()
    back = html.escape(_safe_return(volta)) if volta else ""
    token = request.cookies.get(COOKIE) or ""
    locked = False
    if token:
        pool = await get_pool()
        async with pool.connection() as conn:
            touched = await _session(conn, token)
            if touched is not None and touched.rule == "app" and touched.locked:
                staff = await get_staff_by_id(conn, touched.staff_id)
                locked = staff is not None and _usable(staff) and web_app_open_to(staff)
    phone = request_looks_like_phone(request)
    refusal = refusal_attribute(phone)
    if locked:
        # Only an app session locks, but a phone asking for the desktop site
        # reads as a computer here, and the words follow the page.
        confirm, press, without = (
            ("o Face ID", "Desbloquear com Face ID", "Sem Face ID neste aparelho?")
            if phone else
            ("a chave de acesso", "Desbloquear com a chave de acesso",
             "Sem a chave de acesso aqui?"))
        body = (
            "<h1>Painel bloqueado</h1>"
            "<p class=sub>Sem uso por alguns minutos</p>"
            "<section class=faceid hidden>"
            f"<p>Confirme com {confirm} para continuar de onde parou.</p>"
            f"<button type=button data-faceid=desbloquear data-csrf='{csrf_for(token)}' "
            f"data-volta='{back}' {refusal}>{press}</button>"
            "<p class=bad role=alert hidden></p></section>"
            f"<p class=note>{without} Peça um novo link no Telegram "
            "com <strong>/web</strong>.</p>"
        )
        title = "Painel bloqueado"
    elif not phone:
        body = (
            "<h1>Entrar</h1>"
            "<p class=sub>Demo &amp; Associados</p>"
            "<section class=faceid hidden>"
            "<p>Use a chave de acesso deste computador (Windows Hello ou Touch ID) "
            "para abrir o painel.</p>"
            f"<button type=button data-faceid=entrar-computador data-volta='{back}' "
            f"{refusal}>"
            "Entrar com a chave de acesso</button>"
            f"<p class=note data-dock hidden>{_DOCK_NOTE}</p>"
            "<p class=bad role=alert hidden></p></section>"
            "<p class=note>ou peça um link com <strong>/web</strong> no Telegram</p>"
            f"{_FIRST_USE_COMPUTER}"
        )
        title = "Entrar"
    else:
        body = (
            "<h1>Entrar</h1>"
            "<p class=sub>Demo &amp; Associados</p>"
            "<section class=faceid hidden>"
            "<p>Use o Face ID deste aparelho para abrir o painel.</p>"
            f"<button type=button data-faceid=entrar data-volta='{back}' {refusal}>"
            "Entrar com Face ID</button>"
            "<p class=bad role=alert hidden></p></section>"
            f"{_FIRST_USE}"
        )
        title = "Entrar"
    return page(title, body + FACE_ID_SCRIPT, headers={"Content-Security-Policy": FACE_ID_POLICY},
                style=FACE_ID_STYLE)


@router.get("/passkey.js", include_in_schema=False)
async def script() -> Response:
    """The ceremonies' script. It holds nothing about anyone, so it needs no
    session; off, it does not exist, like the page."""
    _switched_on()
    return Response(_SCRIPT_BYTES, media_type="text/javascript; charset=utf-8",
                    headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})


# A resume check is a look, never activity. In particular it must report a
# lock as text even when a browser sends navigation fetch metadata.
@router.get("/sessao", include_in_schema=False)
async def session_state(request: Request) -> Response:
    _switched_on()
    pool = await get_pool()
    async with pool.connection() as conn:
        try:
            entered = await current_staff(conn, request, activity=False)
        except SessionLocked:
            return PlainTextResponse("locked", status_code=401, headers=HEADERS)
        if entered is None:
            return PlainTextResponse("ended", status_code=401, headers=HEADERS)
        staff, _token = entered
        if not web_app_open_to(staff):
            raise HTTPException(status_code=404)
    return Response(status_code=204, headers=HEADERS)


async def _devices_staff(conn, request: Request) -> tuple[StaffMember, str] | Response:
    entered = await current_staff(conn, request)
    if entered is None:
        return signed_out(request)
    staff, _token = entered
    if not web_app_open_to(staff):
        raise HTTPException(status_code=404)
    return entered


@router.get("/aparelhos/", response_class=HTMLResponse, include_in_schema=False)
async def devices(request: Request) -> Response:
    _switched_on()
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _devices_staff(conn, request)
        if isinstance(entered, Response):
            return entered
        staff, token = entered
        own = await web_access.passkeys_of(conn, staff.id)
        notices = await offers_notices(conn, staff, token)
    items = []
    for key in own:
        used = (f"Último uso: {key.last_used_at.strftime('%d/%m/%Y %H:%M')} UTC."
                if key.last_used_at else "Ainda não usada.")
        items.append(
            f"<li><p>Chave de acesso criada em {key.created_at.strftime('%d/%m/%Y')}</p>"
            f"<p class=sub>{html.escape(used)}</p>"
            f"<form method=post action='/web/aparelhos/remover'>"
            f"<input type=hidden name=csrf value='{csrf_for(token)}'>"
            f"<input type=hidden name=chave value='{key.id}'>"
            "<button type=submit class=quiet>Remover</button></form></li>"
        )
    listing = "<ul>" + "".join(items) + "</ul>" if items else "<p>Nenhuma chave de acesso.</p>"
    return page(
        "Aparelhos", "<h1>Aparelhos</h1>"
        "<p>Uma chave de acesso pode estar em vários aparelhos do mesmo iCloud. "
        "Removê-la encerra as sessões abertas com ela em todos eles.</p>"
        f"{listing}<p class=note>Sair encerra só a sessão deste navegador. "
        "<strong>/web encerrar</strong>, no Telegram, encerra todas. "
        "Perdeu o celular? Mande <strong>/web encerrar</strong> "
        "no Telegram: isso desliga todas as chaves e encerra todas as sessões.</p>"
        + ("<script type=module src='/web/notices.js'></script>" if notices else ""),
        # Only with the button: the page otherwise runs no script at all.
        headers={"Content-Security-Policy": FACE_ID_POLICY + "; worker-src 'self'"}
        if notices else None,
        style=NOTICES_STYLE if notices else "",
        navigation=nav(staff, token, notices=notices),
    )


@router.post("/aparelhos/remover", include_in_schema=False)
async def remove_device(request: Request) -> Response:
    _switched_on()
    if from_another_site(request):
        return _refused(request, status=403)
    form = await read_form(request, max_bytes=1024)
    token = request.cookies.get(COOKIE) or ""
    if not csrf_matches(token, form.get("csrf", "")):
        return _refused(request, status=403)
    raw = form.get("chave", "")
    if not (raw.isascii() and raw.isdigit() and len(raw) <= 9):
        return _refused(request)
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _devices_staff(conn, request)
        if isinstance(entered, Response):
            return entered
        staff, _token = entered
        await web_access.remove_passkey(conn, staff.id, int(raw))
    return RedirectResponse("/web/aparelhos/", status_code=303, headers=HEADERS)


async def _session(conn, token: str) -> web_access.Touched | None:
    """The session behind this cookie, locked or not, read as a look: a
    passkey session's lock clock does not move."""
    return await web_access.touch_session(
        conn, token,
        idle_seconds=settings.web_session_idle_seconds,
        absolute_seconds=settings.web_session_absolute_seconds,
        app_absolute_seconds=settings.web_app_session_seconds,
        lock_seconds=settings.web_app_lock_seconds,
        activity=False,
    )


# ── enrolling ────────────────────────────────────────────────────────────────


async def _enrolling(conn, request: Request) -> tuple[StaffMember, str] | Response:
    """The person who may enrol a passkey now, or the refusal."""
    try:
        signed_in = await current_staff(conn, request)
    except SessionLocked:
        # Only a passkey session locks, and it may not enrol anyway.
        return _refused(request, WINDOW_PASSED, 403)
    if signed_in is None:
        return _refused(request, WINDOW_PASSED, 401)
    staff, session = signed_in
    if not web_app_open_to(staff):
        raise HTTPException(status_code=404)
    window = await web_access.enrolment_window_open(
        conn, session, window_seconds=settings.web_passkey_enrol_seconds)
    if window != staff.id:
        return _refused(request, WINDOW_PASSED, 403)
    return staff, session


@router.post("/faceid/ativar/opcoes", include_in_schema=False)
async def enrol_options(request: Request) -> Response:
    _switched_on()
    body = await _posted(request, with_session=True)
    if isinstance(body, Response):
        return body
    _origin, rp_id = _relying_party()
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _enrolling(conn, request)
        if isinstance(entered, Response):
            return entered
        staff, session = entered
        existing = await web_access.passkeys_of(conn, staff.id)
        if len(existing) >= web_access.MAX_PASSKEYS:
            return _refused(request, TOO_MANY, 409)
        # Stored before the device is handed it: the completion stores the
        # same, whatever another enrolment does meanwhile.
        handle = await web_access.user_handle_for(conn, staff.id)
        challenge = await web_access.issue_challenge(
            conn, "register", staff_id=staff.id, session_token=session)
    if challenge is None:
        return _refused(request, status=503)
    return _options(generate_registration_options(
        rp_id=rp_id, rp_name=_RP_NAME,
        user_name=staff.email or staff.name, user_display_name=staff.name,
        user_id=handle, challenge=challenge,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=p.credential_id)
                             for p in existing],
    ))


@router.post("/faceid/ativar", include_in_schema=False)
async def enrol(request: Request) -> Response:
    _switched_on()
    body = await _posted(request, with_session=True)
    if isinstance(body, Response):
        return body
    origin, rp_id = _relying_party()
    credential = body.get("credencial")
    if not isinstance(credential, dict):
        return _refused(request)
    challenge = _challenge_of(credential)
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _enrolling(conn, request)
        if isinstance(entered, Response):
            return entered
        staff, session = entered
        if challenge is None:
            return _refused(request)
        spent = await web_access.spend_challenge(conn, challenge, "register",
                                                 session_token=session)
        if spent is None or spent.staff_id != staff.id:
            return _refused(request)
        try:
            verified = verify_registration_response(
                credential=credential, expected_challenge=challenge,
                expected_rp_id=rp_id, expected_origin=origin,
                require_user_verification=True,
            )
        except Exception as exc:
            logger.info("A Face ID enrolment for staff %s was refused: %s", staff.id, exc)
            return _refused(request)
        handle = await web_access.user_handle_for(conn, staff.id)
        try:
            added = await web_access.add_passkey(
                conn, staff.id, credential_id=verified.credential_id,
                public_key=verified.credential_public_key,
                sign_count=verified.sign_count, user_handle=handle)
        except pg_errors.UniqueViolation:
            # Already stored: excludeCredentials is there to prevent it.
            await conn.rollback()
            logger.info("A Face ID enrolment for staff %s repeated a stored credential",
                        staff.id)
            return _refused(request)
    if added is None:
        return _refused(request, TOO_MANY, 409)
    logger.info("Passkey %s enrolled for staff %s", added, staff.id)
    if staff.chat_id:
        # The kind named as the home page offered it: by the User-Agent.
        told = TOLD if request_looks_like_phone(request) else TOLD_COMPUTER
        try:
            await telegram.send_message(staff.chat_id, told, parse_mode=None)
        except Exception:
            logger.exception("Could not tell staff %s about a new passkey", staff.id)
    return _go("/web/?aviso=faceid")


# ── signing in ───────────────────────────────────────────────────────────────


@router.post("/faceid/entrar/opcoes", include_in_schema=False)
async def sign_in_options(request: Request) -> Response:
    """A discoverable ceremony: no credential is named, the device offers
    the passkeys it holds for this host."""
    _switched_on()
    refused = await _posted(request, with_session=False)
    if isinstance(refused, Response):
        return refused
    _origin, rp_id = _relying_party()
    pool = await get_pool()
    async with pool.connection() as conn:
        challenge = await web_access.issue_challenge(conn, "signin")
    if challenge is None:
        logger.warning("Face ID sign-in challenges are at their cap")
        return _refused(request, status=503)
    return _options(generate_authentication_options(
        rp_id=rp_id, challenge=challenge,
        user_verification=UserVerificationRequirement.REQUIRED,
    ))


@router.post("/faceid/entrar", include_in_schema=False)
async def sign_in(request: Request) -> Response:
    """The rule is chosen here, once, from the User-Agent, the mobile hint
    and the page's touch report, and stored on the session. Never left to
    `open_session`'s default, which is the looser rule."""
    _switched_on()
    body = await _posted(request, with_session=False)
    if isinstance(body, Response):
        return body
    origin, rp_id = _relying_party()
    credential = body.get("credencial")
    if not isinstance(credential, dict):
        return _refused(request)
    challenge = _challenge_of(credential)
    credential_id = _credential_id(credential)
    if challenge is None or credential_id is None:
        return _refused(request)
    pool = await get_pool()
    async with pool.connection() as conn:
        if await web_access.spend_challenge(conn, challenge, "signin") is None:
            return _refused(request)
        passkey = await web_access.passkey_by_credential(conn, credential_id)
        if passkey is None:
            return _refused(request)
        if await web_access.second_factor_locked(conn, passkey.staff_id):
            logger.warning("A Face ID sign-in for staff %s met the attempt lock",
                           passkey.staff_id)
            return _refused(request, status=429)
        staff = await get_staff_by_id(conn, passkey.staff_id)
        if staff is None or not _usable(staff) or not web_app_open_to(staff):
            return _refused(request, status=403)
        try:
            verified = verify_authentication_response(
                credential=credential, expected_challenge=challenge,
                expected_rp_id=rp_id, expected_origin=origin,
                credential_public_key=passkey.public_key,
                credential_current_sign_count=passkey.sign_count,
                require_user_verification=True,
            )
        except Exception as exc:
            # A sign count that did not move is one of these: a possible
            # cloned key, and py_webauthn's message says so.
            logger.warning("A Face ID sign-in for staff %s was refused: %s", staff.id, exc)
            await _count_failure(conn, staff.id)
            return _refused(request)
        if not await web_access.record_use(conn, passkey.id, verified.new_sign_count,
                                           previous=passkey.sign_count, commit=False):
            # Another answer of this credential was recorded since it was
            # read: two at once, or a cloned key. Neither opens a session.
            await conn.rollback()
            logger.warning("A Face ID sign-in for staff %s lost the race for passkey %s's "
                           "count", staff.id, passkey.id)
            return _refused(request)
        await web_access.clear_failures(conn, staff.id, commit=False)
        phone = is_phone(request.headers.get("user-agent", ""),
                         request.headers.get("sec-ch-ua-mobile"), _touch_points(body))
        rule: Literal["browser", "app"] = "app" if phone else "browser"
        session = await web_access.open_session(
            conn, staff.id, idle_seconds=settings.web_session_idle_seconds,
            method="passkey", passkey_id=passkey.id, rule=rule,
            absolute_seconds=settings.web_app_session_seconds if rule == "app" else None,
        )
    logger.info("Web session opened with a passkey for staff %s under the %s rule",
                staff.id, rule)
    volta = _text(body, "volta")
    response = _go(_safe_return(volta) if volta else "/web/?app=1")
    _set_cookie(response, session, max_age=cookie_seconds(rule))
    return response


# ── unlocking ────────────────────────────────────────────────────────────────


async def _unlocking(conn, request: Request) -> tuple[StaffMember, str] | Response:
    """The person whose app session may be unlocked here, or the refusal.
    The one place a locked session is let through. A browser session, link
    or a computer's passkey, never locks and has nothing to unlock."""
    token = request.cookies.get(COOKIE) or ""
    touched = await _session(conn, token)
    if touched is None:
        return _refused(request, status=401)
    if touched.rule != "app":
        return _refused(request, status=403)
    staff = await get_staff_by_id(conn, touched.staff_id)
    if staff is None or not _usable(staff):
        await web_access.close_session(conn, token)
        return _refused(request, status=401)
    if not web_app_open_to(staff):
        raise HTTPException(status_code=404)
    return staff, token


@router.post("/faceid/desbloquear/opcoes", include_in_schema=False)
async def unlock_options(request: Request) -> Response:
    _switched_on()
    body = await _posted(request, with_session=True)
    if isinstance(body, Response):
        return body
    _origin, rp_id = _relying_party()
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _unlocking(conn, request)
        if isinstance(entered, Response):
            return entered
        staff, session = entered
        own = await web_access.passkeys_of(conn, staff.id)
        await conn.commit()
        challenge = await web_access.issue_challenge(
            conn, "unlock", staff_id=staff.id, session_token=session)
    if challenge is None:
        return _refused(request, status=503)
    return _options(generate_authentication_options(
        rp_id=rp_id, challenge=challenge,
        allow_credentials=[PublicKeyCredentialDescriptor(id=p.credential_id) for p in own],
        user_verification=UserVerificationRequirement.REQUIRED,
    ))


@router.post("/faceid/desbloquear", include_in_schema=False)
async def unlock(request: Request) -> Response:
    _switched_on()
    body = await _posted(request, with_session=True)
    if isinstance(body, Response):
        return body
    origin, rp_id = _relying_party()
    credential = body.get("credencial")
    if not isinstance(credential, dict):
        return _refused(request)
    challenge = _challenge_of(credential)
    credential_id = _credential_id(credential)
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _unlocking(conn, request)
        if isinstance(entered, Response):
            return entered
        staff, session = entered
        if challenge is None or credential_id is None:
            return _refused(request)
        spent = await web_access.spend_challenge(conn, challenge, "unlock",
                                                 session_token=session)
        if spent is None or spent.staff_id != staff.id:
            return _refused(request)
        passkey = await web_access.passkey_by_credential(conn, credential_id)
        if passkey is None or passkey.staff_id != staff.id:
            logger.warning("A locked session of staff %s was answered by a passkey "
                           "not theirs", staff.id)
            return _refused(request, status=403)
        if await web_access.second_factor_locked(conn, staff.id):
            return _refused(request, status=429)
        try:
            verified = verify_authentication_response(
                credential=credential, expected_challenge=challenge,
                expected_rp_id=rp_id, expected_origin=origin,
                credential_public_key=passkey.public_key,
                credential_current_sign_count=passkey.sign_count,
                require_user_verification=True,
            )
        except Exception as exc:
            logger.warning("A Face ID unlock for staff %s was refused: %s", staff.id, exc)
            await _count_failure(conn, staff.id)
            return _refused(request)
        if not await web_access.record_use(conn, passkey.id, verified.new_sign_count,
                                           previous=passkey.sign_count, commit=False):
            await conn.rollback()
            logger.warning("A Face ID unlock for staff %s lost the race for passkey %s's "
                           "count", staff.id, passkey.id)
            return _refused(request)
        await web_access.clear_failures(conn, staff.id, commit=False)
        if not await web_access.unlock_session(conn, session, staff.id):
            return _refused(request, status=401)
    volta = _text(body, "volta")
    return _go(_safe_return(volta) if volta else "/web/")


# ── notices ──────────────────────────────────────────────────────────────────

# A subscription is an endpoint of at most 1024 characters and two short keys.
_MAX_PUSH_BODY = 4 * 1024
_BASE64URL = re.compile(r"[A-Za-z0-9_-]{1,128}={0,2}")
PUSH_REFUSED = ("Não consegui ativar os avisos neste aparelho. Abra o painel pelo ícone, "
                "entre com Face ID e tente de novo.")


def _push_switched_on() -> None:
    """Off, or without the VAPID keys, these addresses do not exist:
    answered before any connection."""
    _switched_on()
    if not push_ready():
        raise HTTPException(status_code=404)


def _push_refused(status: int) -> JSONResponse:
    return JSONResponse({"erro": PUSH_REFUSED}, status_code=status, headers=HEADERS)


async def _push_posted(request: Request) -> dict[str, Any] | Response:
    """The body of a notices POST, or the refusal: another site, a body over
    4 KiB or not JSON, a form token that is not this session's. All before
    any connection is taken."""
    if from_another_site(request):
        return _push_refused(403)
    body = await _read_json(request, max_bytes=_MAX_PUSH_BODY)
    if body is None:
        return _push_refused(400)
    if not csrf_matches(request.cookies.get(COOKIE) or "", _text(body, "csrf")):
        return _push_refused(403)
    return body


async def _push_session(conn, request: Request) -> tuple[StaffMember, int] | Response:
    """The person and the passkey of an unlocked app session, or the refusal.

    Decided on `rule`, not `method`: a passkey on a computer opens a
    `method='passkey'` session under the browser rule, and that one gets no
    push. Read as a look, like the unlock: subscribing does not move the lock
    clock. The staff row is read again, as `current_staff` does.
    """
    token = request.cookies.get(COOKIE) or ""
    touched = await _session(conn, token) if token else None
    if touched is None or touched.rule != "app" or touched.locked:
        return _push_refused(403)
    staff = await get_staff_by_id(conn, touched.staff_id)
    if staff is None or not _usable(staff):
        await web_access.close_session(conn, token)
        return _push_refused(403)
    if not web_app_open_to(staff):
        raise HTTPException(status_code=404)
    passkey_id = await web_access.session_passkey(conn, token, staff.id)
    if passkey_id is None:
        return _push_refused(403)
    return staff, passkey_id


def _key(body: dict[str, Any], name: str, size: int) -> bytes | None:
    """A subscription key as the browser hands it (base64url), decoded, when
    it is exactly `size` bytes."""
    value = body.get(name)
    if not isinstance(value, str) or not _BASE64URL.fullmatch(value):
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError:
        return None
    return raw if len(raw) == size else None


def _device_point(raw: bytes | None) -> bytes | None:
    """The device's public key: 65 bytes, an uncompressed point on P-256.
    A point off the curve could never be encrypted to."""
    if raw is None or raw[0] != 4:
        return None
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except ValueError:
        return None
    return raw


@router.get("/avisos/chave", include_in_schema=False)
async def notices_key(request: Request) -> Response:
    """The VAPID public key `PushManager.subscribe` takes, for the session
    that may subscribe."""
    _push_switched_on()
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _push_session(conn, request)
    if isinstance(entered, Response):
        return entered
    return JSONResponse({"chave": settings.web_push_vapid_public_key}, headers=HEADERS)


@router.post("/avisos/assinar", include_in_schema=False)
async def subscribe_notices(request: Request) -> Response:
    """Store this device's subscription, bound to the person and passkey of
    the unlocked app session that sent it. The endpoint must be a known push
    service: the server will POST to it."""
    _push_switched_on()
    body = await _push_posted(request)
    if isinstance(body, Response):
        return body
    endpoint = body.get("endpoint")
    p256dh = _device_point(_key(body, "p256dh", 65))
    auth = _key(body, "auth", 16)
    if not isinstance(endpoint, str) or not push_subscriptions.allowed_endpoint(endpoint) \
            or p256dh is None or auth is None:
        return _push_refused(400)
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _push_session(conn, request)
        if isinstance(entered, Response):
            return entered
        staff, passkey_id = entered
        try:
            await push_subscriptions.subscribe(conn, staff.id, passkey_id, endpoint=endpoint,
                                               p256dh=p256dh, auth=auth)
        except pg_errors.ForeignKeyViolation:
            # The passkey was removed since the session was read.
            await conn.rollback()
            return _push_refused(403)
    # The host only: the endpoint itself is a capability.
    logger.info("Push subscription stored for staff %s, passkey %s, at %s",
                staff.id, passkey_id, urlsplit(endpoint).hostname)
    return Response(status_code=204, headers=HEADERS)


@router.post("/avisos/cancelar", include_in_schema=False)
async def cancel_notices(request: Request) -> Response:
    """Remove this device's subscription. Only the person's own: another
    person's endpoint removes nothing."""
    _push_switched_on()
    body = await _push_posted(request)
    if isinstance(body, Response):
        return body
    endpoint = body.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > \
            push_subscriptions.MAX_ENDPOINT:
        return _push_refused(400)
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _push_session(conn, request)
        if isinstance(entered, Response):
            return entered
        staff, _passkey_id = entered
        await push_subscriptions.unsubscribe(conn, staff.id, endpoint)
    return Response(status_code=204, headers=HEADERS)


__all__ = ["FACE_ID_REFUSED", "KEY_REFUSED", "KEY_TOO_MANY", "KEY_WINDOW_PASSED",
           "PUSH_REFUSED", "TOLD", "TOLD_COMPUTER", "TOO_MANY", "WINDOW_PASSED", "router"]
