"""The shell every page this host serves to a person is built in.

The PJe enrolment page and the web pages both take something sensitive from a
browser, and both need the same guarantees: never cached, never framed, never
a referrer carrying a token to anybody, nothing loaded from anywhere else. That
is one decision, so it lives in one place. Two copies of a Content-Security-
Policy drift, and the one that drifts is the one nobody re-reads, which is how
a page ends up quietly allowing a script source the other forbids.

Nothing here is fetched from another host: no stylesheet, no font, no script.
Anything fetched from elsewhere is another party watching a password or a
client's document being typed.
"""
from __future__ import annotations

import html
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse

HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, private",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        # img-src is for the tab icon, served by this host under /web/;
        # manifest-src for the manifest every page links, without which the
        # browser refuses it and the panel cannot be installed.
        "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; manifest-src 'self'"
    ),
}

# The firm's own visual language (website/assets/css/style.css): a near-black
# ground at night, light grey text, a silver accent, serif headings, uppercase
# letter-spaced labels. The corners are rounder than the site's 2px on purpose
# (Paulo, 2026-10-27): --radius (8px) for controls, --radius-lg (12px) for
# containers; the site keeps its own. The day version keeps the same
# shapes on a warm off-white ground. Which one shows follows the device's own
# light or dark setting unless the person picked one with the "Tema" link,
# which the server remembers in a cookie and renders as data-theme on <html>:
# a toggle without a script. The site's typefaces (Playfair Display, Jost) are not loaded either:
# these pages fetch nothing from another host, so the same mood is drawn from
# system serif and sans stacks.
_DAY = """color-scheme:light;--bg:#f6f5f2;--panel:#fff;--line:#e2e0db;--line-strong:#cfccc5;
 --text:#151515;--strong:#000;--mid:#4e4e4e;--muted:#6b6b6b;--accent:#1a1a1a;
 --on-accent:#fff;--accent-hover:#3a3a3a;--quiet-line:rgba(0,0,0,.3);
 --quiet-hover:rgba(0,0,0,.05);--row-hover:#efede8;--bad:#a5321f"""
_NIGHT = """color-scheme:dark;--bg:#080808;--panel:#111;--line:#1f1f1f;--line-strong:#2a2a2a;
 --text:#e8e8e8;--strong:#fff;--mid:#a8a8a8;--muted:#8a8a8a;--accent:#bbb;
 --on-accent:#080808;--accent-hover:#e8e8e8;--quiet-line:rgba(255,255,255,.35);
 --quiet-hover:rgba(255,255,255,.08);--row-hover:#0f0f0f;--bad:#e08a7a"""

# A field in focus shows one edge, its own border thickened inward, instead of
# the border plus the general outline floating 2px outside it (a double ring
# that read as noise). Buttons and links keep that outline: they have no border
# of their own to thicken.
# The device decides unless data-theme says otherwise; the explicit choice is
# stated after the media query and is more specific, so it wins either way.
STYLE = """
:root{""" + _DAY + """;--radius:8px;--radius-lg:12px}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){""" + _NIGHT + """}}
:root[data-theme=dark]{""" + _NIGHT + """}
*,*::before,*::after{box-sizing:border-box}
body{font-family:system-ui,-apple-system,'Segoe UI',Roboto,'Helvetica Neue',sans-serif;
 max-width:30rem;margin:0 auto;padding-block:3.5rem 4.5rem;
 padding-inline:clamp(1.25rem,4vw,2.5rem);line-height:1.6;color:var(--text);
 background:var(--bg);-webkit-font-smoothing:antialiased}
h1{font-family:Georgia,'Times New Roman',serif;font-weight:600;font-size:2rem;
 line-height:1.15;letter-spacing:-.01em;color:var(--strong);margin:0 0 .5rem;
 text-wrap:balance}
p{color:var(--mid)}
p.sub{color:var(--muted);margin:0 0 2rem;font-size:.8rem;letter-spacing:.14em;
 text-transform:uppercase}
label{display:block;margin:1.3rem 0 .45rem;font-weight:600;font-size:.7rem;
 letter-spacing:.16em;text-transform:uppercase;color:var(--muted)}
input,select,textarea{width:100%;min-height:2.9rem;padding:.75rem .9rem;font-size:1rem;
 color:var(--text);background:var(--panel);border:1px solid var(--line-strong);
 border-radius:var(--radius);font-family:inherit;
 transition:border-color .15s ease,background-color .15s ease}
input:hover,select:hover,textarea:hover{border-color:var(--muted)}
input:focus,select:focus,textarea:focus{border-color:var(--accent)}
input:focus-visible,select:focus-visible,textarea:focus-visible{outline:none;
 box-shadow:inset 0 0 0 1px var(--accent)}
button,.actions a{display:inline-flex;align-items:center;justify-content:center;
 min-height:2.9rem;padding:.8rem 1.5rem;font-size:.76rem;font-weight:600;
 letter-spacing:.12em;text-transform:uppercase;line-height:1.2;text-decoration:none;
 color:var(--on-accent);background:var(--accent);border:1px solid var(--accent);
 border-radius:var(--radius);cursor:pointer;font-family:inherit;
 transition:background-color .15s ease,border-color .15s ease,color .15s ease}
button{margin-top:1.8rem;width:100%}
button:hover,.actions a:hover{background:var(--accent-hover);border-color:var(--accent-hover)}
button.quiet,.actions a.quiet{background:transparent;color:var(--text);
 border-color:var(--quiet-line)}
button.quiet:hover,.actions a.quiet:hover{background:var(--quiet-hover)}
:focus-visible{outline:1px solid var(--accent);outline-offset:2px}
.note{margin-top:1.8rem;padding:1rem 1.2rem;background:var(--panel);
 border-left:1px solid var(--accent);border-radius:var(--radius-lg);font-size:.88rem;color:var(--mid)}
.bad{color:var(--bad)}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--text)}
body.wide{max-width:68rem}
.nav-icon{width:1.25rem;height:1.25rem;flex-shrink:0;vertical-align:middle}
nav{display:flex;flex-wrap:wrap;gap:.6rem 1.6rem;align-items:center;margin-bottom:3rem;
 padding-bottom:1.2rem;border-bottom:1px solid var(--line);font-size:.72rem;
 letter-spacing:.16em;text-transform:uppercase}
nav a{color:var(--mid);text-decoration:none;transition:color .15s ease}
nav a:hover{color:var(--strong)}
nav .who{margin-left:auto;color:var(--muted);text-transform:none;letter-spacing:.02em;
 font-size:.88rem}
nav form{margin:0}nav button{margin:0;width:auto;min-height:0;padding:.45rem 1rem;
 font-size:.68rem}
a{color:var(--mid);transition:color .15s ease}a:hover{color:var(--strong)}
.filters{display:flex;flex-wrap:wrap;gap:1rem;align-items:flex-end;padding:1.2rem 1.3rem;
 background:var(--panel);border:1px solid var(--line);border-radius:var(--radius-lg)}
.filters div{flex:1 1 10rem}.filters label{margin-top:0}
.filters button{margin-top:0;width:auto}
.scroll{overflow-x:auto;margin-top:2rem}
table{border-collapse:collapse;width:100%;font-size:.92rem}
th,td{text-align:left;padding:.9rem 1rem;border-bottom:1px solid var(--line);
 vertical-align:middle}
th{font-size:.66rem;font-weight:600;text-transform:uppercase;letter-spacing:.16em;
 color:var(--muted);border-bottom-color:var(--line-strong);padding-block:.75rem}
tbody tr{transition:background-color .12s ease}
tbody tr:hover{background:var(--row-hover)}
td a{color:var(--strong);font-weight:500;text-decoration:none}
td a:hover{text-decoration:underline}
td.num{font-variant-numeric:tabular-nums;white-space:nowrap;color:var(--muted)}
.tag{display:inline-block;padding:.15rem .55rem;border-radius:var(--radius);
 font-size:.64rem;letter-spacing:.12em;text-transform:uppercase;
 border:1px solid var(--line-strong);color:var(--mid);white-space:nowrap}
.tag.private{border-color:var(--accent);color:var(--text)}
dl{display:grid;grid-template-columns:max-content 1fr;gap:.8rem 1.6rem}
dt{color:var(--muted);font-size:.7rem;letter-spacing:.14em;text-transform:uppercase;
 padding-top:.2rem}
dd{margin:0;color:var(--text)}
.actions{display:flex;flex-wrap:wrap;gap:.8rem;margin-top:2rem}
.empty{margin-top:2rem;padding:3rem 1.5rem;text-align:center;
 border:1px dashed var(--line-strong);border-radius:var(--radius-lg)}
.empty strong{display:block;font-family:Georgia,'Times New Roman',serif;font-weight:600;
 font-size:1.25rem;color:var(--strong);margin-bottom:.4rem}
.empty p{margin:0 auto;max-width:34rem;color:var(--muted)}
.empty .actions{justify-content:center;margin-top:1.4rem}
pre{white-space:pre-wrap;word-wrap:break-word;font-size:.9rem;line-height:1.7;
 background:var(--panel);color:var(--text);padding:1.4rem;border:1px solid var(--line);
 border-radius:var(--radius-lg);font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
@media (prefers-reduced-motion:reduce){
 *,*::before,*::after{transition:none !important;animation:none !important}}
"""


@dataclass(frozen=True)
class Navigation:
    markup: str
    sidebar: bool = False


SHELL_STYLE = """
body.app-page{--sidebar-width:15.5rem;--mobile-bar-height:3.75rem;
 max-width:none;margin:0;padding:0;min-height:100dvh}
body.app-page.wide{max-width:none}
.app-shell{display:grid;grid-template-columns:var(--sidebar-width) minmax(0,1fr);
 min-height:100dvh}
.app-content{grid-column:2;min-width:0;width:100%;max-width:48rem;margin-inline:auto;
 padding:3rem clamp(1.25rem,4vw,3rem) 4rem}
.wide .app-content{max-width:68rem}
.sidebar-slot{position:fixed;inset:0 auto 0 0;width:var(--sidebar-width);z-index:20}
nav.sidebar-nav{display:flex;flex-direction:column;align-items:stretch;gap:0;height:100%;margin:0;padding:1.1rem;
 overflow-y:auto;background:var(--panel);border:0;border-right:1px solid var(--line);
 font-size:.92rem;letter-spacing:normal;text-transform:none}
.sidebar-brand{display:flex;align-items:center;gap:.5rem;padding:.4rem .65rem 1.2rem}
.sidebar-brand a{text-decoration:none;min-width:0;flex:1}
.sidebar-brand strong{font-family:Georgia,'Times New Roman',serif;font-size:1.2rem;
 font-weight:500;color:var(--strong);letter-spacing:-.02em}
.sidebar-brand small{display:block;margin-top:.2rem;font-size:.59rem;letter-spacing:.16em;
 text-transform:uppercase;color:var(--muted)}
.sidebar-links{display:flex;flex-direction:column;gap:.35rem;margin:.7rem 0 1.5rem}
.sidebar-nav .nav-item{display:flex;align-items:center;justify-content:flex-start;gap:.8rem;
 width:100%;min-height:2.9rem;padding:.7rem .8rem;margin:0;border:1px solid transparent;
 border-radius:var(--radius);font-size:.9rem;font-weight:400;line-height:1.35;
 letter-spacing:normal;text-transform:none;text-decoration:none;color:var(--mid);
 background:transparent}
.sidebar-nav .nav-item:hover{background:var(--row-hover);color:var(--strong)}
.sidebar-nav .nav-item[aria-current=page]{background:var(--row-hover);color:var(--strong);
 border-color:var(--line);font-weight:600}
.sidebar-footer{display:flex;flex-direction:column;gap:.25rem;margin-top:auto;padding-top:1rem;
 border-top:1px solid var(--line)}
.sidebar-person{display:flex;align-items:center;gap:.75rem;padding:.5rem .8rem .9rem;min-width:0}
.staff-mark{display:grid;place-items:center;width:2.2rem;height:2.2rem;flex-shrink:0;
 border:1px solid var(--line-strong);border-radius:var(--radius);font-size:.72rem;
 font-weight:600;color:var(--text)}
.sidebar-person .who{margin:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
 font-size:.9rem;color:var(--text)}
.mobile-bar{display:none}
.menu-toggle,.menu-close{display:inline-flex;align-items:center;justify-content:center;
 width:2.75rem;min-height:2.75rem;margin:0;padding:.65rem;border:0;
 border-radius:var(--radius);color:var(--text);background:transparent}
.menu-toggle:hover,.menu-close:hover{background:var(--row-hover);border-color:transparent}
.menu-toggle[hidden],.menu-close[hidden]{display:none}
.app-menu{position:fixed;inset:0 auto 0 0;margin:0;padding:0;
 width:min(20rem,88vw);height:100dvh;max-width:none;max-height:none;
 border:0;border-radius:0 var(--radius-lg) var(--radius-lg) 0;background:var(--panel);
 color:var(--text)}
.app-menu::backdrop{background:rgba(0,0,0,.55)}
.app-menu .sidebar-nav{padding-top:max(1.1rem,env(safe-area-inset-top));
 padding-bottom:max(1.1rem,env(safe-area-inset-bottom))}
.menu-open{overflow:hidden}
.skip-content{position:fixed;top:.5rem;left:calc(var(--sidebar-width) + .5rem);
 z-index:80;padding:.6rem 1rem;background:var(--panel);color:var(--text);
 border-radius:var(--radius);transform:translateY(-200%)}
.skip-content:focus{transform:none}
@media(min-width:801px){.sidebar-nav .menu-close{display:none}}
@media(max-width:800px){
 .app-shell{display:block}
 .app-content{max-width:none;padding:calc(var(--mobile-bar-height) + env(safe-area-inset-top) + 1.25rem)
  clamp(1rem,4vw,1.4rem) calc(2rem + env(safe-area-inset-bottom))}
 .sidebar-slot{position:static;width:auto;margin-top:calc(var(--mobile-bar-height) + env(safe-area-inset-top))}
 .sidebar-slot .sidebar-nav{height:auto;border-right:0;border-bottom:1px solid var(--line)}
 .nav-ready .sidebar-slot{display:none}
 .app-page:not(.nav-ready) .app-content{padding-top:1.25rem}
 .mobile-bar{position:fixed;inset:0 0 auto;z-index:60;display:grid;
  grid-template-columns:2.75rem minmax(0,1fr) 2.75rem;align-items:center;gap:.6rem;
  height:calc(var(--mobile-bar-height) + env(safe-area-inset-top));
  padding:env(safe-area-inset-top) .65rem 0;background:var(--bg);border-bottom:1px solid var(--line)}
 .mobile-title{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:center;
  font-size:.9rem;font-weight:500;color:var(--text)}
 .mobile-brand{font-family:Georgia,'Times New Roman',serif;text-align:center;
  font-size:1rem;color:var(--text);text-decoration:none}
 .skip-content{left:.5rem}
 .app-page h1{font-size:1.7rem}
 .app-page p.sub{font-size:.72rem;letter-spacing:.08em;margin-bottom:1.4rem}
 .app-page .filters{gap:.8rem;padding:1rem}
 .app-page .drawer{top:calc(var(--mobile-bar-height) + env(safe-area-inset-top))}
 .app-page #toast{top:calc(var(--mobile-bar-height) + env(safe-area-inset-top) + .75rem)}
}
"""


# ── Theme ───────────────────────────────────────────────────────────────────
# The choice lives in a cookie and reaches page() through context variables set
# by a router dependency, so no route has to thread a request into every page
# it builds. A missing or unknown value is "follow the device".
THEME_COOKIE = "tema"
THEMES = ("claro", "escuro")
_theme: ContextVar[str | None] = ContextVar("web_theme", default=None)
_here: ContextVar[str] = ContextVar("web_here", default="/web/")


async def remember_theme(request: Request) -> None:
    """Router dependency: read the theme cookie and the current page."""
    chosen = request.cookies.get(THEME_COOKIE)
    _theme.set(chosen if chosen in THEMES else None)
    query = f"?{request.url.query}" if request.url.query else ""
    _here.set(f"{request.url.path}{query}")


def current_theme() -> str | None:
    return _theme.get()


def current_page() -> str:
    return _here.get()


def _theme_attribute() -> str:
    chosen = _theme.get()
    if chosen == "claro":
        return " data-theme=light"
    if chosen == "escuro":
        return " data-theme=dark"
    return ""


# The firm site's own icon files, served by staff_web under /web/: the proxy
# forwards nothing else to this host, so the browser's guess at /favicon.ico
# would never arrive.
_ICON_LINKS = (
    "<link rel=icon href=/web/favicon.ico sizes=32x32>"
    "<link rel=icon href=/web/favicon-32x32.png type=image/png sizes=32x32>"
    "<link rel=apple-touch-icon href=/web/apple-touch-icon.png>"
)
# What makes the panel installable. The theme colour paints the installed
# app's title bar on a computer and the status bar on a phone, so it follows
# the page: the ground (--bg) of the chosen theme, or both, each under the
# device setting that picks it. navigation.js repaints it when the theme
# changes without a reload. The manifest's own colour stays the night ground.
_MANIFEST_LINK = "<link rel=manifest href=/web/manifest.webmanifest>"
DAY_GROUND = "#f6f5f2"
NIGHT_GROUND = "#080808"


def _theme_color() -> str:
    chosen = _theme.get()
    if chosen == "claro":
        return f"<meta name=theme-color content={DAY_GROUND}>"
    if chosen == "escuro":
        return f"<meta name=theme-color content={NIGHT_GROUND}>"
    return (f"<meta name=theme-color media='(prefers-color-scheme: light)' content={DAY_GROUND}>"
            f"<meta name=theme-color media='(prefers-color-scheme: dark)' content={NIGHT_GROUND}>")


def page(title: str, body: str, status: int = 200,
         headers: dict[str, str] | None = None, *, wide: bool = False,
         style: str = "", head: str = "", navigation: Navigation | None = None) -> HTMLResponse:
    """One page, with every protective header, and anything extra on top.

    `wide` is for lists and documents; forms stay narrow, where a long line
    is harder to read and a code field is the only thing on the page.
    `style` is rules only one group of pages uses, after the shared ones.
    `head` is markup for the head that only one page needs; never built from
    input.
    `navigation` is the signed-in bar, or the pilot's responsive sidebar.
    """
    protected = dict(HEADERS)
    protected.update(headers or {})
    body_class = " class=wide" if wide else ""
    if navigation is not None:
        if navigation.sidebar:
            body_class = " class='app-page wide'" if wide else " class=app-page"
            style = SHELL_STYLE + style
            # Only signed-in pilot pages need the navigation controller.
            # Other pages keep their original policy, including no scripts.
            policy = protected["Content-Security-Policy"]
            # connect-src is for the theme switch, which saves the choice
            # without reloading the page.
            parts = [part.strip() for part in policy.split(";")]
            if not any(part.startswith("script-src ") for part in parts):
                policy += "; script-src 'self'"
            if not any(part.startswith("connect-src ") for part in parts):
                policy += "; connect-src 'self'"
            protected["Content-Security-Policy"] = policy
            body = (
                "<a class=skip-content href='#main-content'>Pular para o conteúdo</a>"
                "<header class=mobile-bar>"
                "<button type=button class=menu-toggle data-menu-open hidden "
                "aria-label='Abrir menu' aria-controls=app-menu aria-haspopup=dialog aria-expanded=false>"
                "<svg class=nav-icon viewBox='0 0 24 24' fill=none stroke=currentColor "
                "stroke-width=1.7 aria-hidden=true focusable=false>"
                "<path d='M4 6h16M4 12h16M4 18h16'/></svg></button>"
                f"<span class=mobile-title>{html.escape(title)}</span>"
                "<a class=mobile-brand href='/web/' aria-label='Demo e Associados: início'>N&amp;S</a>"
                "</header><div class=app-shell>"
                f"<div class=sidebar-slot>{navigation.markup}</div>"
                f"<main class=app-content id=main-content>{body}</main></div>"
                "<dialog class=app-menu id=app-menu aria-label='Menu principal'></dialog>"
                "<script type=module src='/web/navigation.js'></script>"
            )
        else:
            body = navigation.markup + body
    response = HTMLResponse(
        f"<!doctype html><html lang=pt-BR{_theme_attribute()}><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<meta name=robots content='noindex,nofollow'>{_ICON_LINKS}{_MANIFEST_LINK}{_theme_color()}"
        f"<title>{html.escape(title)}</title><style>{STYLE}{style}</style>{head}</head>"
        f"<body{body_class}>{body}</body></html>",
        status_code=status,
        headers=protected,
    )
    return response


def from_another_site(request: Request, *, without_session: bool = False) -> bool:
    """Whether a POST was sent by a page on some other site.

    The login POST carries no session yet, so it has no session-bound form
    token to check, and SameSite says nothing about a request that sets a
    cookie rather than sends one. Without this, a page elsewhere could post
    its author's own valid link and code from a colleague's browser and sign
    that browser into the author's account.

    `Sec-Fetch-Site` is the check that holds: the browser sets it and no page
    can. `Origin` cannot be the main check here, because these pages send no
    referrer and a browser then sends `Origin: null` even for this site's own
    forms, which is exactly what a hostile page can arrange too. A real
    origin that names another host is still refused when one arrives.

    `without_session` is for the posts that have no form token to fall back
    on: there, a browser that sends no `Sec-Fetch-Site` (Safari before 16.4,
    Firefox before 90) is refused, since `Origin: null` is all it would leave
    to check. Telling someone to update their browser costs less than a
    colleague signed into somebody else's account.
    """
    site = request.headers.get("sec-fetch-site")
    if site is None:
        if without_session:
            return True
    elif site != "same-origin":
        return True
    origin = request.headers.get("origin")
    if origin and origin != "null":
        host = (request.headers.get("host") or "").lower()
        return urlsplit(origin).netloc.lower() != host
    return False


def refused_from_another_site() -> HTMLResponse:
    return page("Pedido recusado",
                "<h1 class=bad>Pedido recusado</h1>"
                "<p>Este formulário só pode ser enviado a partir desta página.</p>"
                "<p class=note>Se você abriu esta página aqui mesmo, o navegador pode "
                "estar desatualizado. Atualize-o ou abra o link no Chrome ou no "
                "Safari e peça um novo link com <strong>/web</strong>.</p>",
                status=403)


async def read_form(request: Request, *, max_bytes: int) -> dict[str, str]:
    """The posted form, read without adding a dependency for it.

    An HTML form posts `application/x-www-form-urlencoded`, which the standard
    library parses. FastAPI's `Form(...)` would pull in `python-multipart`, and
    a law firm's production image does not need another package to read a few
    strings. The body is capped here rather than trusted, and capped as it
    arrives: reading it whole and measuring after would hold whatever a public
    endpoint was sent before refusing it.
    """
    # Refused before reading when the body says it is too long, and the
    # reading stops at the cap when it does not say: either way no more than
    # the cap is ever held.
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > max_bytes:
        return {}
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            return {}
        chunks.append(chunk)
    body = b"".join(chunks)
    parsed = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items() if values}


__all__ = ["HEADERS", "STYLE", "THEMES", "THEME_COOKIE", "current_page", "current_theme",
           "from_another_site", "page", "read_form", "refused_from_another_site",
           "remember_theme"]
