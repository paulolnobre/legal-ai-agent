"""The web pages for people who already have an account with the bot.

Nobody gets in here who is not already on the staff list, and nobody gets in
on one factor. The login has two, and neither is a password:

1. **A one-time link the bot sends.** The bot hands it only to the Telegram
   account bound to the staff row, so holding a live one proves control of
   that account.
2. **A code from an authenticator app**, or one of the recovery codes printed
   at enrolment, for the day the phone is lost.

No password is stored anywhere, which is the point: a password database is
the most dangerous thing a small system can hold, and this one has none.

**The person is read again on every request.** A session proves who signed in,
not what they may do now. Deactivating somebody, or demoting them, takes
effect on their next click rather than when their session ages out, because
the session carries an id and the rank comes from the staff row each time.
It is the same rule the callback buttons follow: store who, re-read what.

The cookie is `__Host-` prefixed. That prefix is not decoration here: the
firm's domain also serves a different product on another subdomain, and a
`__Host-` cookie cannot be set or overwritten by a sibling subdomain. Without
it, that other application could plant a session cookie for this one.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging

from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from core.config import push_ready, settings, web_app_on_for
from core.models import StaffMember
from core.repositories import web_access
from core.repositories.connection import get_pool
from core.repositories.staff import ROLE_INTERN, get_staff_by_id, has_rank
from core.services import authorization, second_factor
from core import totp
from core.web.devices import request_looks_like_phone
from core.web.pages import (
    HEADERS,
    Navigation,
    THEME_COOKIE,
    current_page,
    current_theme,
    remember_theme,
    from_another_site,
    page,
    read_form,
    refused_from_another_site,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/web", tags=["web"], dependencies=[Depends(remember_theme)])

COOKIE = "__Host-session"
# A code, a recovery code, a CSRF token and the form overhead. Small on
# purpose: this endpoint is public and Starlette buffers the body first.
_MAX_BODY = 2048


def csrf_for(session_token: str) -> str:
    """The form token that proves a POST came from a page this host rendered.

    Derived from the session rather than stored: the session token lives only
    in an HttpOnly cookie no script can read, so a page on another site cannot
    compute this, and there is no second value to keep in step. SameSite
    already refuses the cross-site POST in current browsers; this is the layer
    that does not depend on the browser getting that right.
    """
    return hashlib.sha256(f"csrf:{session_token}".encode()).hexdigest()[:40]


def csrf_matches(session_token: str, submitted: str) -> bool:
    """Whether a posted form token is this session's. Compared as bytes:
    `hmac.compare_digest` raises on a str with anything but ASCII, and a
    form token is whatever the request says it is."""
    if not session_token:
        return False
    return hmac.compare_digest(submitted.encode("utf-8", "replace"),
                               csrf_for(session_token).encode())


def cookie_seconds(rule: str) -> int:
    """How long the cookie lives: the absolute limit of the session it
    carries, by the rule it lives by, so it never outlives it."""
    if rule == "app":
        return int(settings.web_app_session_seconds)
    return int(settings.web_session_absolute_seconds)


def _set_cookie(response, token: str, *, max_age: int) -> None:
    response.set_cookie(
        COOKIE, token, max_age=max_age,
        httponly=True, secure=True, samesite="strict", path="/",
    )


def _clear_cookie(response) -> None:
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True,
                           samesite="strict")


class SessionLocked(Exception):
    """A passkey session past its lock. Answered by the handlers `install`
    registers with the Face ID page, never with the page asked for."""


class AppEntryRefused(Exception):
    """The installed app on a phone opened without an app session. Answered
    like a lock, and unlike `signed_out()` the cookie stays: the same
    person's browser tab may hold the same session and keeps working."""


async def current_staff(conn, request: Request, *,
                        activity: bool = True) -> tuple[StaffMember, str] | None:
    """Who is signed in, re-read from the staff list, or None.

    Renews the session as a side effect, because a request is activity;
    `activity=False` is for a look that is not (the assistant's poll). The
    staff row is read after that, every time, so a deactivation or demotion
    since sign-in is honoured now.

    Raises `SessionLocked` for a locked session, and `AppEntryRefused` when
    the app's start address (`?app=1`) arrives from a phone without an app
    session while Face ID is switched on. On a computer the installed app is
    the browser's own profile, so a browser session is served there as in a
    tab. The marker only ever refuses: forged, it costs its sender a sign-in
    and grants nothing.
    """
    app_entry = (request.query_params.get(APP_ENTRY) == "1"
                 and settings.web_app != "off")
    token = request.cookies.get(COOKIE) or ""
    touched = await web_access.touch_session(
        conn, token,
        idle_seconds=settings.web_session_idle_seconds,
        absolute_seconds=settings.web_session_absolute_seconds,
        app_absolute_seconds=settings.web_app_session_seconds,
        lock_seconds=settings.web_app_lock_seconds,
        activity=activity,
    ) if token else None
    if touched is None:
        if app_entry:
            raise AppEntryRefused
        return None
    staff = await get_staff_by_id(conn, touched.staff_id)
    if staff is None or not has_rank(staff.role, ROLE_INTERN):
        # Deactivated, or no longer holding any rank: the session is worthless,
        # so it goes, rather than sitting there until it expires.
        await web_access.close_session(conn, token)
        if app_entry:
            raise AppEntryRefused
        return None
    if touched.locked:
        raise SessionLocked
    # Only on a phone, read from this navigation's User-Agent, and only for
    # someone Face ID is offered to: outside the pilot, the app keeps opening
    # on a link session, as it did before.
    if (app_entry and touched.rule == "browser" and request_looks_like_phone(request)
            and web_app_open_to(staff)):
        raise AppEntryRefused
    return staff, token


def _to_face_id(request: Request, marker: str) -> Response:
    """A page load goes to the Face ID page, back here afterwards; a script
    gets the marker. Neither touches the cookie or the session.

    A form that met the lock is not returned to: its address answers only a
    POST, and the way back is a GET.
    """
    if request.headers.get("sec-fetch-mode") == "navigate":
        here = (request.url.path + (f"?{request.url.query}" if request.url.query else "")
                if request.method in ("GET", "HEAD") else "/web/")
        back = quote(_safe_return(here), safe="")
        return RedirectResponse(f"/web/faceid/?volta={back}", status_code=303,
                                headers=HEADERS)
    return PlainTextResponse(marker, status_code=401, headers=HEADERS)


async def _locked(request: Request, _exc: Exception) -> Response:
    return _to_face_id(request, "locked")


async def _app_entry_refused(request: Request, _exc: Exception) -> Response:
    return _to_face_id(request, "app")


def install(app: FastAPI) -> None:
    """Answer the two refusals `current_staff` raises. Without this they
    reach the server's error handler: a 500, never a page."""
    app.add_exception_handler(SessionLocked, _locked)
    app.add_exception_handler(AppEntryRefused, _app_entry_refused)


def way_back_in(request: Request) -> str:
    """The sign-in page's address, named for this device, wherever a session
    has just ended. Safari's Dock app cannot open a Telegram link, so for it
    this is the only way back to the key. Off, nothing: the pages stay as
    they were before Face ID."""
    if not settings.web_enabled or settings.web_app == "off":
        return ""
    label = ("Entrar com Face ID" if request_looks_like_phone(request)
             else "Entrar com a chave de acesso")
    return f"<div class=actions><a href='/web/faceid/'>{label}</a></div>"


def signed_out(request: Request) -> HTMLResponse:
    """For a page reached without a live session. The cookie goes too."""
    response = page(
        "Sessão encerrada",
        "<h1>Você não está conectado</h1>"
        "<p>Peça um link de acesso no Telegram com <strong>/web</strong>.</p>"
        f"{way_back_in(request)}",
        status=401,
    )
    _clear_cookie(response)
    return response


# The theme cycles through "follow the device", day and night. Changing it is
# a GET because it changes nothing but how the next page is painted: another
# site flipping somebody's colours costs them a click to flip back.
_NEXT_THEME: dict[str | None, str | None] = {None: "claro", "claro": "escuro", "escuro": None}
_THEME_LABEL = {None: "automático", "claro": "claro", "escuro": "escuro"}
_THEME_COOKIE_SECONDS = 365 * 24 * 3600


def theme_link(icon: str = "") -> str:
    """The "Tema" link, sending the person back to the page they were on."""
    back = html.escape(quote(current_page(), safe=""))
    label = f"Tema: {_THEME_LABEL[current_theme()]}"
    classes = "'theme nav-item'" if icon else "theme"
    content = f"{icon}<span>{label}</span>" if icon else label
    return f"<a class={classes} href='/web/tema?volta={back}'>{content}</a>"


def _safe_return(target: str) -> str:
    """Only back to one of these pages: never a scheme, another host, "//"
    (which a browser reads as another host), a header-breaking character, or
    a dot segment ("/web/../admin" is normalised by the browser to "/admin";
    "%2e" is the same dot, encoded)."""
    path = target.split("?", 1)[0]
    segments = path.split("/")
    if (target.startswith(("/web/", "/pje/")) and "//" not in target
            and "\\" not in target and target.isprintable() and len(target) <= 500
            and "%2e" not in path.casefold()
            and not any(segment in (".", "..") for segment in segments)):
        return target
    return "/web/"


# What navigation.js asks for: the theme it is about to paint, so the saved
# choice is the one on screen even when another tab changed it meanwhile.
_THEME_TARGET: dict[str, str | None] = {"claro": "claro", "escuro": "escuro", "auto": None}


@router.get("/tema", include_in_schema=False)
async def switch_theme(volta: str = "/web/", para: str | None = None):
    """Move to the next theme and go back where the person was.

    With `para`, the script's request: save that theme and answer with
    nothing, so the page is repainted in place instead of reloaded (which
    closed the menu and lost the place in the conversation)."""
    if not settings.web_enabled:
        return unusable()
    response: Response
    if para is not None:
        if para not in _THEME_TARGET:
            return Response(status_code=400)
        chosen = _THEME_TARGET[para]
        response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    else:
        chosen = _NEXT_THEME[current_theme()]
        response = RedirectResponse(_safe_return(volta), status_code=303)
    if chosen is None:
        response.delete_cookie(THEME_COOKIE, path="/", secure=True, httponly=True,
                               samesite="lax")
    else:
        response.set_cookie(THEME_COOKIE, chosen, max_age=_THEME_COOKIE_SECONDS, path="/",
                            secure=True, httponly=True, samesite="lax")
    return response


# The tab icon: the firm site's own files (website/assets/img), read once.
# Served whether or not the pages are on, since the pages-off answer is still
# a page with a tab, and cached for a week, since they change with the brand.
_STATIC = Path(__file__).parent / "static"
_ICONS = {
    "favicon.ico": "image/x-icon",
    "favicon-32x32.png": "image/png",
    "apple-touch-icon.png": "image/png",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "icon-maskable-512.png": "image/png",
}
_ICON_BYTES = {name: (_STATIC / name).read_bytes() for name in _ICONS}


def _icon_route(name: str):
    async def icon() -> Response:
        return Response(
            _ICON_BYTES[name], media_type=_ICONS[name],
            headers={"Cache-Control": "public, max-age=604800",
                     "X-Content-Type-Options": "nosniff"},
        )
    return icon


for _name in _ICONS:
    router.add_api_route(f"/{_name}", _icon_route(_name), methods=["GET"],
                         include_in_schema=False)

# The query name the installed app's start_url carries. The home page sends a
# person who has the assistant on straight to it; everyone else gets home.
APP_ENTRY = "app"

# Fixed text with nothing about anyone in it, so it needs no session and, like
# the icons, answers with the pages off. The colour is the night ground in
# pages._NIGHT; the icons are those above, the last one with room around the
# mark for the round and rounded crops a phone applies.
_MANIFEST = json.dumps({
    "id": "/web/",
    "scope": "/web/",
    "start_url": f"/web/?{APP_ENTRY}=1",
    "name": "Demo & Associados",
    "short_name": "Demo & Associados",
    "lang": "pt-BR",
    "display": "standalone",
    "background_color": "#080808",
    "theme_color": "#080808",
    "icons": [
        {"src": "/web/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/web/icon-512.png", "sizes": "512x512", "type": "image/png"},
        {"src": "/web/icon-maskable-512.png", "sizes": "512x512", "type": "image/png",
         "purpose": "maskable"},
    ],
}).encode()


@router.get("/manifest.webmanifest", include_in_schema=False)
async def manifest() -> Response:
    return Response(
        _MANIFEST, media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=3600",
                 "X-Content-Type-Options": "nosniff"},
    )


# The installed app's worker (static/sw.js). Like the manifest it holds
# nothing about anyone, so it needs no session and answers with the pages off.
# no-cache: the browser asks for a new version on every navigation, so a fixed
# bug reaches the phone at the next open. Its policy is its own, not the
# pages': connect-src is what lets it fetch the page it is asked for.
_WORKER_BYTES = (_STATIC / "sw.js").read_bytes()
WORKER_POLICY = "default-src 'none'; connect-src 'self'"


@router.get("/sw.js", include_in_schema=False)
async def worker() -> Response:
    return Response(
        _WORKER_BYTES, media_type="text/javascript; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff",
                 "Content-Security-Policy": WORKER_POLICY},
    )


def explorer_open_to(staff: StaffMember) -> bool:
    """Whether the document explorer is switched on for this person."""
    if not settings.web_enabled or settings.web_explorer == "off":
        return False
    if settings.web_explorer == "on":
        return True
    listed = {part.strip() for part in settings.web_explorer_staff.split(",")}
    return str(staff.id) in listed


def web_app_open_to(staff: StaffMember) -> bool:
    """Whether Face ID and the app session are switched on for this person.
    One decision, kept in config, so the notices' sender reads the same one."""
    return web_app_on_for(staff.id)


# The Face ID page, and the home page while it offers enrolment, run
# static/passkey.js and let it ask this host for a ceremony's options. Built
# from the shared policy, never changing it; every other page stays without.
FACE_ID_POLICY = HEADERS["Content-Security-Policy"] + "; script-src 'self'; connect-src 'self'"
FACE_ID_SCRIPT = "<script type=module src='/web/passkey.js'></script>"
# A ceremony's section is rendered hidden and shown by the script once it
# finds the browser can do it: without either, the button would do nothing.
FACE_ID_STYLE = """
.faceid button{width:auto}
.faceid .bad{margin-top:1rem}
h2{font-family:Georgia,'Times New Roman',serif;font-weight:600;font-size:1.25rem;
 color:var(--strong);margin:2.8rem 0 .4rem}
ol.steps{margin:0;padding-left:1.3rem;color:var(--mid)}
ol.steps li{margin:.55rem 0;padding-left:.3rem}
ol.steps li::marker{color:var(--muted);font-variant-numeric:tabular-nums}
"""


# What the script says when a ceremony is cancelled or refused before the
# server answers. The page hands it the words, by the device it was shown
# on, so the script never guesses: a computer has a key and no Face ID.
FACE_ID_REFUSED = ("O Face ID não confirmou. Tente de novo, ou entre pelo link do "
                   "/web no Telegram.")
KEY_REFUSED = ("A chave de acesso não confirmou. Tente de novo, ou entre pelo link do "
               "/web no Telegram.")


def refusal_attribute(phone: bool) -> str:
    text = FACE_ID_REFUSED if phone else KEY_REFUSED
    return f"data-recusa='{html.escape(text, quote=True)}'"


def assistant_open_to(staff: StaffMember) -> bool:
    """Whether the assistant's page is switched on for this person."""
    if not settings.web_enabled or settings.web_assistant == "off":
        return False
    if settings.web_assistant == "on":
        return True
    listed = {part.strip() for part in settings.web_assistant_staff.split(",")}
    return str(staff.id) in listed


def files_href(staff: StaffMember) -> str:
    """Where "Arquivos" leads: the explorer for whoever has it, else the list."""
    return "/web/explorar/" if explorer_open_to(staff) else "/web/arquivos/"


def nav(staff: StaffMember, session: str, *, notices: bool = False) -> Navigation:
    """The bar on every signed-in page. Leaving is a POST with the form token,
    so a link on another site cannot sign somebody out. `notices` adds
    "Ativar avisos" to the pilot's sidebar: only what `offers_notices` said."""
    assistant = ("<a href='/web/assistente/#escrever'>Assistente</a>"
                 if assistant_open_to(staff) else "")
    cases = ("<a href='/web/processos/'>Processos</a>"
             if authorization.decide(staff, "process.track").allowed else "")
    monitoring = (f"<a href='{GRAFANA_HREF}'>Monitoramento</a>"
                  if authorization.decide(staff, "observability.view").allowed else "")
    if web_app_open_to(staff):
        return _sidebar(staff, session, notices=notices)
    return Navigation(
        f"<nav><a href='{files_href(staff)}'>Arquivos</a>"
        f"{assistant}{cases}<a href='/web/integracoes/'>Integrações</a>{monitoring}"
        f"<span class=who>{html.escape(staff.name)}</span>"
        f"{theme_link()}"
        f"<form method=post action='/web/logout'>"
        f"<input type=hidden name=csrf value='{csrf_for(session)}'>"
        f"<button type=submit class=quiet>Sair</button></form></nav>"
    )


# Grafana, behind the same sign-in (core/web/observability.py), owner only.
GRAFANA_HREF = "/grafana/"

_NAV_ICONS = {
    "assistant": "<path d='M21 11a8 8 0 0 1-8 8H7l-4 3V11a9 9 0 0 1 18 0Z'/>"
                 "<path d='M7 9h10M7 13h6'/>",
    "files": "<path d='M3 6h7l2 3h9v11H3zM3 6V4h7l2 2h9v3'/>",
    "processes": "<rect x='4' y='4' width='16' height='17' rx='2'/>"
                 "<path d='M8 2v4M16 2v4M4 10h16M8 14h3M8 17h6'/>",
    "integrations": "<path d='M9 2v5M15 2v5M6 7h12v4a6 6 0 0 1-12 0zM12 17v5'/>",
    "devices": "<rect x='6' y='2' width='12' height='20' rx='2'/>"
               "<path d='M10 18h4'/>",
    "theme": "<circle cx='12' cy='12' r='4'/>"
             "<path d='M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19"
             "M5 19l1.5-1.5m11-11L19 5'/>",
    "observability": "<path d='M3 3v18h18'/><path d='m7 15 4-4 3 3 6-6'/>",
    "logout": "<path d='M9 4H4v16h5M9 12h12m-4-4 4 4-4 4'/>",
    "close": "<path d='m6 6 12 12M6 18 18 6'/>",
    "notices": "<path d='M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9'/>"
               "<path d='M10.3 21a1.94 1.94 0 0 0 3.4 0'/>",
}


def _nav_icon(name: str) -> str:
    """Trusted local vector shapes, with a text label beside every icon."""
    return ("<svg class=nav-icon viewBox='0 0 24 24' fill=none stroke=currentColor "
            "stroke-width=1.65 stroke-linecap=round stroke-linejoin=round "
            f"aria-hidden=true focusable=false>{_NAV_ICONS[name]}</svg>")


# What the person reads about notices. The denial is the design's copy.
NOTICES_DENIED = ("Os avisos estão desligados nos Ajustes do iPhone para este app. "
                  "Ative em Ajustes > Notificações > Demo & Associados.")
NOTICES_ON = "Avisos ativados neste aparelho."
NOTICES_FAILED = "Não consegui ativar os avisos. Tente de novo."
# For every page that offers the button. A nav item is display:flex, which
# would beat the bare `hidden` attribute and show the button everywhere.
NOTICES_STYLE = """
.notices-note{margin:.25rem 0 .5rem;padding:.6rem .8rem;font-size:.8rem}
.sidebar-nav [data-avisos][hidden]{display:none}
"""


async def offers_notices(conn, staff: StaffMember, session: str) -> bool:
    """Whether this page offers "Ativar avisos": push is set up, the person
    is in the app's switch, and this is an unlocked app session (a passkey on
    a phone). Never a link session, never a computer's. Read as a look, so
    asking does not move the lock clock. The routes decide again."""
    if not push_ready() or not web_app_open_to(staff):
        return False
    touched = await web_access.touch_session(
        conn, session,
        idle_seconds=settings.web_session_idle_seconds,
        absolute_seconds=settings.web_session_absolute_seconds,
        app_absolute_seconds=settings.web_app_session_seconds,
        lock_seconds=settings.web_app_lock_seconds,
        activity=False,
    )
    return (touched is not None and touched.staff_id == staff.id
            and touched.rule == "app" and not touched.locked)


def _notices_button(session: str) -> str:
    """Rendered hidden: the notices script shows it only inside the
    installed app and only while this device has no subscription."""
    said = "".join(f" data-{name}='{html.escape(text, quote=True)}'" for name, text in
                   (("negado", NOTICES_DENIED), ("ativado", NOTICES_ON),
                    ("falha", NOTICES_FAILED)))
    return (f"<button type=button class='quiet nav-item' data-avisos "
            f"data-csrf='{csrf_for(session)}'{said} hidden>{_nav_icon('notices')}"
            "<span>Ativar avisos</span></button>"
            "<p class='note notices-note' data-avisos-nota role=status hidden></p>")


def _sidebar(staff: StaffMember, session: str, *, notices: bool = False) -> Navigation:
    here = urlsplit(current_page()).path
    entries = []
    if assistant_open_to(staff):
        entries.append(("assistant", "Assistente", "/web/assistente/#escrever",
                        here.startswith("/web/assistente/")))
    if authorization.decide(staff, "process.track").allowed:
        entries.append(("processes", "Processos", "/web/processos/",
                        here.startswith("/web/processos/")))
    entries.extend([
        ("files", "Arquivos", files_href(staff),
         here.startswith(("/web/explorar/", "/web/arquivos/"))),
        ("integrations", "Integrações", "/web/integracoes/",
         here.startswith("/web/integracoes/")),
        ("devices", "Aparelhos", "/web/aparelhos/", here.startswith("/web/aparelhos/")),
    ])
    if authorization.decide(staff, "observability.view").allowed:
        entries.append(("observability", "Monitoramento", GRAFANA_HREF, False))
    links = "".join(
        f"<a class=nav-item href='{html.escape(href, quote=True)}'"
        f"{' aria-current=page' if active else ''}>{_nav_icon(icon)}"
        f"<span>{html.escape(label)}</span></a>"
        for icon, label, href, active in entries
    )
    names = staff.name.split()
    initials = (names[0][0] + (names[-1][0] if len(names) > 1 else "")) if names else "NS"
    return Navigation(
        "<nav class=sidebar-nav aria-label='Navegação principal'>"
        "<div class=sidebar-brand><a href='/web/'>"
        "<strong>Demo &amp; Associados</strong><small>Assistente legal</small></a>"
        "<button type=button class=menu-close data-menu-close hidden autofocus "
        f"aria-label='Fechar menu'>{_nav_icon('close')}</button></div>"
        f"<div class=sidebar-links>{links}</div><div class=sidebar-footer>"
        f"<div class=sidebar-person><span class=staff-mark aria-hidden=true>"
        f"{html.escape(initials.upper())}</span><span class=who>{html.escape(staff.name)}</span></div>"
        f"{_notices_button(session) if notices else ''}"
        f"{theme_link(_nav_icon('theme'))}"
        "<form method=post action='/web/logout'>"
        f"<input type=hidden name=csrf value='{csrf_for(session)}'>"
        f"<button type=submit class='quiet nav-item'>{_nav_icon('logout')}"
        "<span>Sair</span></button></form></div></nav>",
        sidebar=True,
    )


_NAVIGATION_BYTES = (Path(__file__).parent / "static" / "navigation.js").read_bytes()


@router.get("/navigation.js", include_in_schema=False)
async def navigation_script() -> Response:
    """Presentation only, with no staff data; its one request saves the theme."""
    if not settings.web_enabled or settings.web_app == "off":
        raise HTTPException(status_code=404)
    return Response(_NAVIGATION_BYTES, media_type="text/javascript; charset=utf-8",
                    headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})


def app_lock_meta(staff: StaffMember) -> str:
    """Only pilot pages install the resume check in their scripts."""
    return "<meta name=app-lock content=1>" if web_app_open_to(staff) else ""


def unusable() -> HTMLResponse:
    """One message for every reason a link does not work.

    Expired, already used, never existed and belongs to somebody else read
    the same, because telling them apart tells a stranger which are real.
    """
    return page(
        "Link inválido",
        "<h1 class=bad>Este link não vale mais</h1>"
        "<p>Ele pode ter expirado, já ter sido usado, ou não ser válido.</p>"
        "<p>Peça um novo no Telegram com <strong>/web</strong>.</p>",
        status=404,
    )


def _offer_enrolment(token: str) -> HTMLResponse:
    """The first visit: set up the authenticator, here and not in the chat.

    The secret is not on this page. It appears only after the button is
    pressed, which is a POST: link-preview crawlers and browser prefetchers
    make GET requests, so a secret rendered on GET would be read by Telegram's
    servers before the person ever opened the link.
    """
    return page(
        "Configurar o autenticador",
        f"<h1>Primeiro acesso</h1>"
        f"<p>Para entrar é preciso um código de um aplicativo autenticador "
        f"(Google Authenticator, Microsoft Authenticator, Authy, 1Password, "
        f"Bitwarden). Você configura uma vez, aqui.</p>"
        f"<p class=note>O código é mostrado nesta página e nunca no Telegram. "
        f"O Telegram é o primeiro fator do seu acesso: se o segundo também "
        f"passasse por ele, quem entrasse na sua conta do Telegram teria os "
        f"dois.</p>"
        f"<form method=post action='/web/login/{html.escape(token)}/enrol'>"
        f"<button type=submit>Configurar agora</button></form>",
    )


def _grouped(secret: str) -> str:
    """The secret in groups of four, which is how people copy it correctly."""
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


def _login_form(token: str, *, error: str = "") -> HTMLResponse:
    warn = f"<p class=bad>{html.escape(error)}</p>" if error else ""
    return page(
        "Entrar",
        f"<h1>Entrar</h1>"
        f"<p class=sub>Digite o código de 6 dígitos do seu aplicativo "
        f"autenticador.</p>{warn}"
        f"<form method=post action='/web/login/{html.escape(token)}'>"
        f"<label for=code>Código</label>"
        f"<input id=code name=code inputmode=numeric autocomplete=one-time-code "
        f"maxlength=20 required autofocus>"
        f"<button type=submit>Entrar</button></form>"
        f"<p class=note>Perdeu o celular? Use um dos códigos de recuperação que "
        f"você guardou no cadastro, no mesmo campo. Cada um funciona uma vez.</p>",
    )


@router.get("/login/{token}", response_class=HTMLResponse, include_in_schema=False)
async def login_form(token: str) -> HTMLResponse:
    """Show the second-factor form. Looking does not spend the link."""
    if not settings.web_enabled:
        return unusable()
    pool = await get_pool()
    async with pool.connection() as conn:
        staff_id = await web_access.peek_login_link(conn, token)
        if staff_id is None:
            return unusable()
        enrolment = await web_access.enrolment_of(conn, staff_id)
        if enrolment is None or not enrolment.confirmed:
            return _offer_enrolment(token)
    return _login_form(token)


@router.post("/login/{token}/enrol", response_class=HTMLResponse,
             include_in_schema=False)
async def enrol(request: Request, token: str) -> HTMLResponse:
    """Generate the secret and the recovery codes, and show them once.

    The link is not spent here: it is spent by the first code, which is what
    proves the secret reached the app. An enrolment made here stays
    unconfirmed until then, so closing this tab before saving leaves nothing
    half-working behind, only a fresh start next time.

    A confirmed enrolment is not replaced from this page. Starting over is
    `/mfa refazer` in the bot, a deliberate act, rather than something a
    second visit to a login link can do.
    """
    if not settings.web_enabled:
        return unusable()
    if from_another_site(request, without_session=True):
        return refused_from_another_site()
    pool = await get_pool()
    async with pool.connection() as conn:
        staff_id = await web_access.peek_login_link(conn, token)
        if staff_id is None:
            return unusable()
        staff = await get_staff_by_id(conn, staff_id)
        if staff is None:
            return unusable()
        # Link and enrolment are checked again inside the write, under a
        # lock: the looks above only decide what to render.
        secret = totp.new_secret()
        enrolled = await web_access.enrol_through_link(conn, token, secret=secret)
        if enrolled is None or enrolled.staff_id != staff_id:
            return unusable()
        if enrolled.codes is None:
            return _login_form(token)
        codes = enrolled.codes
    uri = totp.provisioning_uri(
        secret, account=staff.email or staff.name, issuer="Demo & Associados",
    )
    listed = "".join(f"<li><code>{html.escape(code)}</code></li>" for code in codes)
    logger.info("Authenticator enrolment started for staff %s", staff_id)
    return page(
        "Configurar o autenticador",
        f"<h1>Configure o seu autenticador</h1>"
        f"<p>No aplicativo, escolha <strong>adicionar conta</strong> e "
        f"<strong>inserir chave manualmente</strong>. Nome: Demo &amp; Associados. "
        f"Chave:</p>"
        # Wrapped only between groups of four, the way it is typed; and the
        # address kept out of the uppercase caption style, which made it read
        # as "OTPAUTH://..." while the app needs it as written.
        f"<p><code style='font-size:1.15rem;word-break:normal'>"
        f"{html.escape(_grouped(secret))}</code></p>"
        f"<p style='font-size:.85rem;color:var(--muted)'>Se o aplicativo aceitar um "
        f"endereço, use: <code style='word-break:break-all'>{html.escape(uri)}</code></p>"
        f"<h1 style='margin-top:2rem'>Guarde estes códigos de recuperação</h1>"
        f"<p>Se perder o celular, cada um destes entra uma vez no lugar do "
        f"código do aplicativo. <strong>Esta é a única vez que eles aparecem.</strong> "
        f"Imprima ou guarde no gerenciador de senhas do escritório.</p>"
        f"<ul>{listed}</ul>"
        f"<h1 style='margin-top:2rem'>Confirme</h1>"
        f"<p>Digite o código de 6 dígitos que o aplicativo está mostrando agora. "
        f"Só depois disso o cadastro vale.</p>"
        f"<form method=post action='/web/login/{html.escape(token)}'>"
        f"<label for=code>Código do aplicativo</label>"
        f"<input id=code name=code inputmode=numeric autocomplete=one-time-code "
        f"maxlength=20 required>"
        f"<button type=submit>Confirmar e entrar</button></form>",
    )


@router.post("/login/{token}", response_class=HTMLResponse, include_in_schema=False)
async def login(request: Request, token: str):
    """Spend the link, check the second factor, open a session."""
    if not settings.web_enabled:
        return unusable()
    # Before the link is spent: a hostile post must not burn it either.
    if from_another_site(request, without_session=True):
        return refused_from_another_site()
    form = await read_form(request, max_bytes=_MAX_BODY)
    code = (form.get("code") or "").strip()
    pool = await get_pool()
    async with pool.connection() as conn:
        staff_id = await web_access.spend_login_link(conn, token)
        if staff_id is None:
            return unusable()
        verdict = (await second_factor.check(conn, staff_id, code)
                   if code else second_factor.Check.REFUSED)
        if verdict is second_factor.Check.LOCKED:
            logger.warning("A web login for staff %s met the attempt lock", staff_id)
            minutes = second_factor.LOCK_SECONDS // 60
            return page(
                "Acesso travado",
                "<h1 class=bad>Acesso travado por alguns minutos</h1>"
                f"<p>Foram {second_factor.MAX_FAILURES} códigos errados seguidos, "
                f"então o acesso fica fechado por {minutes} minutos. O "
                "responsável pelo escritório foi avisado.</p>"
                "<p>Se não foi você, avise-o agora.</p>",
                status=429,
            )
        if verdict is not second_factor.Check.ACCEPTED:
            logger.info("A web login for staff %s failed its second factor", staff_id)
            return page(
                "Código não aceito",
                "<h1 class=bad>Código não aceito</h1>"
                "<p>O código estava errado, já tinha sido usado, ou expirou "
                "enquanto era digitado.</p>"
                "<p>Por segurança este link não vale mais. Peça outro no "
                "Telegram com <strong>/web</strong>.</p>",
                status=401,
            )
        session = await web_access.open_session(
            conn, staff_id, idle_seconds=settings.web_session_idle_seconds,
        )
    logger.info("Web session opened for staff %s", staff_id)
    response = RedirectResponse("/web/", status_code=303)
    _set_cookie(response, session, max_age=cookie_seconds("browser"))
    return response


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home(request: Request):
    if not settings.web_enabled:
        return unusable()
    pool = await get_pool()
    enrolling = False
    async with pool.connection() as conn:
        signed_in = await current_staff(conn, request)
        if signed_in is not None and web_app_open_to(signed_in[0]):
            # Offered only while the full sign-in is minutes old. The
            # enrolment routes decide again; this decides what to show.
            enrolling = await web_access.enrolment_window_open(
                conn, signed_in[1], window_seconds=settings.web_passkey_enrol_seconds,
            ) == signed_in[0].id
    if signed_in is None:
        return signed_out(request)
    staff, session = signed_in
    if request.query_params.get(APP_ENTRY) == "1" and assistant_open_to(staff):
        return RedirectResponse("/web/assistente/#escrever", status_code=303)
    face_id, headers = "", None
    # The offer and its note follow this request's User-Agent: the same
    # ceremony, named as the device names it.
    phone = request_looks_like_phone(request)
    if request.query_params.get("aviso") == "faceid" and web_app_open_to(staff):
        face_id = ("<p class=note role=status>Face ID ativado neste aparelho. No iPhone, "
                   "toque em Compartilhar e em <strong>Adicionar à Tela de Início</strong>, "
                   "e abra o painel pelo ícone com <strong>Entrar com Face ID</strong>.</p>"
                   if phone else
                   "<p class=note role=status>Chave de acesso ativada neste computador. "
                   "Para instalar o painel: no Chrome ou no Edge, clique no ícone de "
                   "instalar na barra de endereço; no Safari, use <strong>Arquivo &gt; "
                   "Adicionar ao Dock</strong>. Depois abra o painel pelo ícone com "
                   "<strong>Entrar com a chave de acesso</strong>.</p>")
    elif enrolling:
        offer = (("Neste aparelho o painel pode abrir com o Face ID",
                  "Ativar Face ID neste aparelho") if phone else
                 ("Neste computador o painel pode abrir com a chave de acesso (Windows "
                  "Hello ou Touch ID)", "Ativar a chave de acesso deste computador"))
        face_id = (f"<section class=faceid hidden>"
                   f"<p class=note>{offer[0]}, sem "
                   f"pedir um link no Telegram a cada vez. Ative agora: a opção fica "
                   f"disponível só nos primeiros minutos depois de entrar.</p>"
                   f"<button type=button data-faceid=ativar data-csrf='{csrf_for(session)}' "
                   f"{refusal_attribute(phone)}>"
                   f"{offer[1]}</button>"
                   f"<p class=bad role=alert hidden></p></section>{FACE_ID_SCRIPT}")
        headers = {"Content-Security-Policy": FACE_ID_POLICY}
    if not face_id:
        # Nothing to say here: the person goes where the work is. The address
        # stays, because sign-in, the app's start and the brand lead to it.
        return RedirectResponse(("/web/assistente/#escrever" if assistant_open_to(staff)
                                 else files_href(staff)), status_code=303)
    return page(
        "Demo & Associados",
        f"<h1>Olá, {html.escape(staff.name)}</h1>"
        f"<p class=sub>Sessão ativa. Ela se renova enquanto você usa e "
        f"termina sozinha depois de algumas horas.</p>"
        f"<div class=actions><a href='{files_href(staff)}'>Abrir os arquivos</a></div>"
        f"<p class=note>Aqui aparece exatamente o que você já vê pelo bot: a sua "
        f"pasta pessoal, as pastas das suas equipes e a do escritório.</p>{face_id}",
        headers=headers, wide=True, style=FACE_ID_STYLE if face_id else "",
        navigation=nav(staff, session),
    )


@router.post("/logout", include_in_schema=False)
async def logout(request: Request):
    """End this session. Requires the form token, like every write will."""
    if not settings.web_enabled:
        return unusable()
    if from_another_site(request):
        return refused_from_another_site()
    form = await read_form(request, max_bytes=_MAX_BODY)
    token = request.cookies.get(COOKIE) or ""
    if not csrf_matches(token, form.get("csrf", "")):
        return page("Pedido recusado",
                    "<h1 class=bad>Pedido recusado</h1>"
                    "<p>Recarregue a página e tente de novo.</p>", status=403)
    pool = await get_pool()
    async with pool.connection() as conn:
        await web_access.close_session(conn, token)
    response = page("Até logo", "<h1>Você saiu</h1>"
                    "<p>Para voltar, peça um link com <strong>/web</strong>.</p>"
                    f"{way_back_in(request)}")
    _clear_cookie(response)
    return response


__all__ = ["AppEntryRefused", "COOKIE", "FACE_ID_POLICY", "FACE_ID_REFUSED", "FACE_ID_SCRIPT",
           "FACE_ID_STYLE", "KEY_REFUSED", "NOTICES_STYLE", "SessionLocked", "cookie_seconds", "csrf_for",
           "csrf_matches", "current_staff", "install", "nav", "offers_notices", "router",
           "signed_out", "unusable", "way_back_in", "web_app_open_to"]
