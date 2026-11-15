"""The archive as folders: tiles for folders and clients, cards for documents.

The same archive as `/web/arquivos/`, arranged the way people picture it:
first the folders this person has (their own, each of their teams, the
firm's), then the clients or cases inside one folder, then that client's
documents. Search crosses every folder they see.

**Who sees what is not decided here.** Every read takes the scope from
`scope_for`, rebuilt on every request, and the repository applies
`visibility_clause` before any narrowing, exactly as the bot and the list do.
A team folder in the address is honoured only if the person is in that team
now; one they left answers like one that does not exist.

Behind `WEB_EXPLORER` (off, pilot, on). Off, or not in the pilot, these
addresses answer like addresses that do not exist.

Every page works without a script: tiles and cards are links, search is a GET
form, pages are links. `static/explorer.js` only improves them: a document
opens in a panel beside the cards, a card can be dragged onto a folder, and
search answers as you type. These pages, and only these, allow a script from
this host; every other page keeps the policy with no script at all.
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from core.config import settings
from core.legislation import GROUP_NAMES, GROUPS, PREFIXES, SPHERES, TYPE_SUGGESTIONS, group_of
from core.models import StaffMember
from core.repositories.connection import get_pool
from core.repositories.documents import (
    DocumentPreview,
    ExplorerCard,
    FolderKind,
    client_counts,
    document_preview,
    documents_in,
    folder_counts,
    legislation_counts,
    list_doc_types,
    log_access,
    search_everywhere,
)
from core.repositories.teams import Scope, list_teams, scope_for, teams_of
from core.schedule import local_now
from core.services import folders as moving
from core.services.authorization import decide
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
    csrf_for,
    csrf_matches,
    current_staff,
    explorer_open_to,
    nav,
    signed_out,
)

router = APIRouter(prefix="/web/explorar", tags=["web"],
                   dependencies=[Depends(remember_theme)])

PER_PAGE = 48
_SEARCH_MAX = 100
# The column's width: a client name is matched whole, never cut.
_CLIENT_MAX = 255
_HOME = "/web/explorar/"
_DOCUMENT = "/web/explorar/documento/"
# How much of the text the document page shows. Cut by the database.
PREVIEW_CHARS = 600
# A CSRF token and one destination.
_MAX_BODY = 1024
# A CSRF token and a document's name, type and client at their longest, twice
# (as typed, and as the page opened): each character up to four UTF-8 bytes,
# each byte "%XX" in the form. The sphere is a short ASCII word.
_EDIT_BODY = 1024 + 2 * (500 + 50 + 255 + 9) * 4 * 3

STYLE = """
.crumbs{display:flex;flex-wrap:wrap;gap:.2rem .6rem;margin:0 0 .8rem;font-size:.72rem;
 letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
.crumbs a{color:var(--muted);text-decoration:none}.crumbs a:hover{color:var(--strong)}
.crumbs [aria-current]{color:var(--text);overflow-wrap:anywhere}
.tiles,.cards{display:grid;gap:1rem;margin-top:2rem}
.tiles{grid-template-columns:repeat(auto-fill,minmax(13rem,1fr));align-items:start}
.cards{grid-template-columns:repeat(auto-fill,minmax(15rem,1fr))}
.tile{display:flex;flex-direction:column;gap:.35rem;padding:1.2rem 1.3rem;
 background:var(--panel);border:1px solid var(--line-strong);border-radius:var(--radius-lg);
 color:var(--text);text-decoration:none;transition:border-color .15s ease}
.tile:hover{border-color:var(--accent);color:var(--text)}
.tile .kind{font-size:.64rem;letter-spacing:.16em;text-transform:uppercase;color:var(--muted)}
.tile strong{font-family:Georgia,'Times New Roman',serif;font-weight:600;font-size:1.15rem;
 line-height:1.25;color:var(--strong);overflow-wrap:anywhere}
.tile .count{font-size:.85rem;color:var(--mid);font-variant-numeric:tabular-nums}
.tile.none strong{font-style:italic}
.card{display:flex;flex-direction:column;gap:.4rem;padding:1.1rem 1.2rem;
 background:var(--panel);border:1px solid var(--line);border-radius:var(--radius-lg)}
.card .title{color:var(--strong);font-weight:500;line-height:1.35;text-decoration:none;
 overflow-wrap:anywhere}
.card .title:hover{text-decoration:underline}
.card .meta{font-size:.8rem;color:var(--muted);font-variant-numeric:tabular-nums}
.card .where{margin-top:auto;padding-top:.4rem;font-size:.64rem;letter-spacing:.14em;
 text-transform:uppercase;color:var(--mid);text-decoration:none}
.search{display:flex;flex-wrap:wrap;gap:.8rem;align-items:flex-end;margin-top:1.5rem}
.search div{flex:1 1 16rem}.search label{margin-top:0}
.search button{margin-top:0;width:auto}
.pager{justify-content:space-between}
pre.preview{margin-top:2rem;max-height:22rem;overflow:auto}
.move fieldset{border:0;padding:0;margin:2rem 0 0;display:grid;gap:.6rem}
.move legend{margin-bottom:.6rem;font-weight:600;font-size:.7rem;letter-spacing:.16em;
 text-transform:uppercase;color:var(--muted)}
.choice{display:flex;gap:.8rem;align-items:flex-start;margin:0;padding:.9rem 1rem;
 background:var(--panel);border:1px solid var(--line-strong);border-radius:var(--radius);
 cursor:pointer;font-size:1rem;font-weight:400;letter-spacing:0;text-transform:none;
 color:var(--text)}
.choice:has(input:checked){border-color:var(--accent)}
.choice input{width:auto;min-height:0;margin:.3rem 0 0}
.choice small{display:block;margin-top:.2rem;font-size:.82rem}
.move button{width:auto}
.edit button{width:auto}
.edit .hint{margin:.5rem 0 0;font-size:.85rem;color:var(--muted)}
/* What the script adds: none of it shows without it. */
.card[draggable=true]{cursor:grab}
.card.dragging{opacity:.45}
.drop{position:fixed;left:50%;bottom:1.5rem;transform:translateX(-50%);z-index:30;display:flex;
 flex-wrap:wrap;gap:.6rem;align-items:center;max-width:calc(100% - 2rem);padding:.8rem 1rem;
 background:var(--panel);border:1px solid var(--accent);border-radius:var(--radius-lg);
 box-shadow:0 18px 40px rgba(0,0,0,.35)}
.drop[hidden]{display:none}
.drop span{font-size:.66rem;letter-spacing:.16em;text-transform:uppercase;color:var(--muted)}
.drop .target{padding:.7rem 1rem;border:1px dashed var(--line-strong);border-radius:var(--radius);
 color:var(--text);font-size:.92rem}
.drop .target.over{border-style:solid;border-color:var(--accent);background:var(--quiet-hover)}
.drawer{position:fixed;top:0;right:0;bottom:0;z-index:40;width:min(44rem,100%);overflow-y:auto;
 padding:1.6rem 2rem 3rem;background:var(--bg);border-left:1px solid var(--line-strong);
 box-shadow:-24px 0 48px rgba(0,0,0,.35)}
.drawer .close{margin:0 0 1.4rem;width:auto}
.drawer h1{font-size:1.7rem}
#toast{position:fixed;left:50%;top:1.2rem;transform:translateX(-50%);z-index:50;max-width:calc(100% - 2rem);
 padding:.8rem 1.1rem;background:var(--panel);border:1px solid var(--accent);border-radius:var(--radius-lg);
 color:var(--text);font-size:.92rem}
#toast[hidden]{display:none}
#toast.bad{border-color:var(--bad);color:var(--bad)}
"""


# ── Folders in the address ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Folder:
    slug: str
    kind: FolderKind
    team_id: int | None
    name: str
    label: str  # the small word above the name on its tile

    @property
    def href(self) -> str:
        return f"{_HOME}{self.slug}/"


def _folders(teams: list[tuple[int, str]]) -> list[Folder]:
    """This person's folders, in the order the tiles show them."""
    return [
        Folder("pessoal", "private", None, "Minha pasta", "Pessoal"),
        *(Folder(f"equipe-{team_id}", "team", team_id, name, "Equipe")
          for team_id, name in teams),
        Folder("escritorio", "firm", None, "Escritório", "Todos"),
    ]


def _folder_named(slug: str, teams: list[tuple[int, str]]) -> Folder | None:
    """The folder an address names, if it is one of this person's now."""
    if slug != "pessoal" and slug != "escritorio" and not re.fullmatch(
            r"equipe-[0-9]{1,9}", slug):
        return None
    return next((folder for folder in _folders(teams) if folder.slug == slug), None)


def _slug_of(card: ExplorerCard) -> str:
    """The address word of the folder a card's document is in."""
    if card.visibility == "team":
        return f"equipe-{card.team_id}"
    return "pessoal" if card.visibility == "private" else "escritorio"


def _drop_bar(teams: list[tuple[int, str]]) -> str:
    """Where a dragged card can land: this person's own folders. The script
    shows it while a card is dragged. None of them takes a document out of
    the person's sight (each is theirs), so a drag never needs the warning
    the "Mover para..." page gives an admin moving into a team they are not
    in; the server decides every drop again all the same."""
    targets = "".join(
        f"<div class=target data-slug={f.slug}>{html.escape(f.name)}</div>"
        for f in _folders(teams)
    )
    return f"<div id=drop class=drop hidden><span>Soltar em</span>{targets}</div>"


def _folder_of(card: ExplorerCard, folders: list[Folder]) -> Folder | None:
    kind = card.visibility
    return next((f for f in folders if f.kind == kind
                 and (kind != "team" or f.team_id == card.team_id)), None)


# ── Pieces ──────────────────────────────────────────────────────────────────


def _documents(n: int) -> str:
    return "1 documento" if n == 1 else f"{n:,} documentos".replace(",", ".")


def _url(path: str, **params) -> str:
    """An address with its query, every value percent-encoded."""
    query = urlencode({k: v for k, v in params.items() if v not in (None, "")},
                      quote_via=quote)
    return f"{path}?{query}" if query else path


def _href(path: str, **params) -> str:
    """`_url`, ready for an HTML attribute."""
    return html.escape(_url(path, **params))


def _shown(client: str | None) -> str:
    """A client as the pages show it: capital first letter, like "Sem cliente"
    beside it. Only shown so: the stored name, which links and suggestions
    carry, is untouched, so nothing typed is lost and every link still
    matches its documents exactly."""
    if client is None or not client.strip():
        return "Sem cliente"
    return client[:1].upper() + client[1:]


def _legislation_href(folder: Folder, group: str | None = None) -> str:
    """The folder's "Legislação", or one kind of legislation inside it."""
    return _href(folder.href, legislacao=group or "1")


def _client_href(folder: Folder, client: str | None) -> str:
    if client is None:
        return _href(folder.href, sem_cliente="1")
    return _href(folder.href, cliente=client)


def _crumbs(*steps: tuple[str, str | None]) -> str:
    """Arquivos › Escritório › Acme. The last step is where the person is."""
    parts = []
    for label, href in steps:
        if href is None:
            parts.append(f"<span aria-current=page>{html.escape(label)}</span>")
        else:
            parts.append(f"<a href='{href}'>{html.escape(label)}</a>")
    return f"<div class=crumbs>{'<span aria-hidden=true>›</span>'.join(parts)}</div>"


def _tile(href: str, label: str, name: str, count: int, *, none: bool = False) -> str:
    return (f"<a class='tile{' none' if none else ''}' href='{href}'>"
            f"<span class=kind>{html.escape(label)}</span>"
            f"<strong>{html.escape(name)}</strong>"
            f"<span class=count>{_documents(count)}</span></a>")


def _when(value) -> str:
    return f"{local_now(value):%d/%m/%Y}" if value else ""


def _card(card: ExplorerCard, where: Folder | None = None) -> str:
    """A document's card. Outside a client's page (`where` given, in search)
    it also says whose it is: two "Procuração" cards are otherwise one."""
    if group_of(card.doc_type):
        # Legislation has no client: what tells two laws apart is the sphere.
        parts = (card.doc_type or "", card.sphere or "", _when(card.created_at))
    else:
        client = (_shown(card.matter) if where is not None and (card.matter or "").strip()
                  else "")
        parts = (client, card.doc_type or "", _when(card.created_at))
    meta = " · ".join(html.escape(part) for part in parts if part)
    place = ""
    if where is not None:
        place = (f"<a class=where href='{html.escape(where.href)}'>"
                 f"{html.escape(where.label + ' ' + where.name if where.kind == 'team' else where.name)}"
                 f"</a>")
    return (f"<article class=card data-id={card.id} data-folder={_slug_of(card)}>"
            f"<a class=title href='{_DOCUMENT}{card.id}'>{html.escape(card.title)}</a>"
            f"<span class=meta>{meta or '&nbsp;'}</span>{place}</article>")


def _pager(path: str, page_number: int, has_more: bool, **params) -> str:
    links = []
    if page_number > 1:
        links.append(f"<a class=quiet href='{_href(path, **params, pagina=page_number - 1)}'>"
                     f"Mais recentes</a>")
    if has_more:
        links.append(f"<a class=quiet href='{_href(path, **params, pagina=page_number + 1)}'>"
                     f"Mais antigos</a>")
    return f"<div class='actions pager'>{''.join(links)}</div>" if links else ""


def _empty(title: str, text: str, back: tuple[str, str] | None = None) -> str:
    action = (f"<div class=actions><a class=quiet href='{back[1]}'>{html.escape(back[0])}</a>"
              f"</div>" if back else "")
    return (f"<div class=empty><strong>{html.escape(title)}</strong>"
            f"<p>{html.escape(text)}</p>{action}</div>")


def _search_form(text: str = "") -> str:
    return (f"<form method=get action='{_HOME}' class=search role=search>"
            f"<div><label for=q>Buscar em todas as pastas</label>"
            f"<input id=q name=q value='{html.escape(text)}' maxlength={_SEARCH_MAX} "
            f"autocomplete=off placeholder='Título, cliente ou tipo'></div>"
            f"<button type=submit>Buscar</button></form>")


def _page_number(raw: str) -> int:
    return int(raw) if re.fullmatch(r"[1-9][0-9]{0,4}", raw or "") else 1


# The shared policy, plus a script from this host and the requests it makes
# back to it. Built from the shared one rather than written out, so a change
# there reaches these pages too; never written into it, so no other page
# gains a script source.
POLICY = (HEADERS["Content-Security-Policy"]
          + "; script-src 'self'; connect-src 'self'; worker-src 'self'")
SCRIPT = f"{_HOME}explorer.js"
_SCRIPT_BYTES = (Path(__file__).parent / "static" / "explorer.js").read_bytes()


def _explorer_page(staff: StaffMember, session: str, title: str, body: str,
                   status: int = 200) -> HTMLResponse:
    return page(title, f"{body}"
                f"<script type=module src='{SCRIPT}'></script>",
                status=status, headers={"Content-Security-Policy": POLICY},
                wide=True, style=STYLE, head=app_lock_meta(staff),
                navigation=nav(staff, session))


# ── Every request ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Visit:
    staff: StaffMember
    session: str
    scope: Scope
    teams: list[tuple[int, str]]


def _switched_on() -> None:
    """Off, the explorer does not exist: the same 404 as any unknown address,
    answered before a database connection is taken, so a switched-off page
    costs nothing and cannot fail any other way."""
    if not settings.web_enabled or settings.web_explorer == "off":
        raise HTTPException(status_code=404)


async def _enter(conn, request: Request) -> _Visit | HTMLResponse:
    """Who is asking, re-read now, and what they may see now. Not in the
    pilot, the explorer does not exist for them either."""
    _switched_on()
    signed_in = await current_staff(conn, request)
    if signed_in is None:
        return signed_out(request)
    staff, session = signed_in
    if not explorer_open_to(staff):
        raise HTTPException(status_code=404)
    decision = decide(staff, "document.browse")
    if not decision.allowed:
        return page("Sem permissão",
                    f"<h1 class=bad>Sem permissão</h1>"
                    f"<p>{html.escape(decision.reason)}</p>", status=403, wide=True,
                    navigation=nav(staff, session))
    scope = await scope_for(conn, staff.id)
    # Tiles for the teams the scope was built from, so a tile never leads to
    # a folder the reads below would refuse.
    teams = [(team_id, name) for team_id, name in await teams_of(conn, staff.id)
             if team_id in scope.team_ids]
    return _Visit(staff, session, scope, teams)


@router.get("/explorer.js", include_in_schema=False)
async def script() -> Response:
    """The explorer's script. It holds nothing about anyone, so it needs no
    session; off, it does not exist, like the pages."""
    _switched_on()
    return Response(_SCRIPT_BYTES, media_type="text/javascript; charset=utf-8",
                    headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def folders_page(request: Request, q: str = "", pagina: str = "") -> HTMLResponse:
    _switched_on()
    text = (q or "").strip()[:_SEARCH_MAX]
    pool = await get_pool()
    async with pool.connection() as conn:
        visit = await _enter(conn, request)
        if isinstance(visit, HTMLResponse):
            return visit
        if text:
            page_number = _page_number(pagina)
            cards, has_more = await search_everywhere(
                conn, visit.scope, text, page=page_number, per_page=PER_PAGE,
            )
            return _search_results(visit, text, cards, page_number, has_more)
        counts = await folder_counts(conn, visit.scope)

    folders = _folders(visit.teams)
    tiles = "".join(
        _tile(html.escape(f.href), f.label, f.name, counts.get((f.kind, f.team_id), 0))
        for f in folders
    )
    total = sum(counts.values())
    return _explorer_page(
        visit.staff, visit.session, "Arquivos",
        f"<h1>Arquivos</h1>"
        f"<p class=sub>{_documents(total)} nas suas pastas</p>"
        f"{_search_form()}<div id=results><div class=tiles>{tiles}</div></div>"
        f"<div class=actions><a class=quiet href='/web/arquivos/'>Ver tudo em lista</a></div>"
        f"{_drop_bar(visit.teams)}",
    )


def _search_results(visit: _Visit, text: str, cards: list[ExplorerCard],
                    page_number: int, has_more: bool) -> HTMLResponse:
    folders = _folders(visit.teams)
    if cards:
        listing = ("<div class=cards>"
                   + "".join(_card(card, _folder_of(card, folders)) for card in cards)
                   + "</div>" + _pager(_HOME, page_number, has_more, q=text))
    elif page_number > 1:
        listing = _empty("Não há mais resultados", "Os resultados desta busca acabaram.",
                         ("Voltar ao início da busca", _href(_HOME, q=text)))
    else:
        listing = _empty(f"Nada encontrado para “{text}”",
                         "A busca olha o título, o cliente ou caso e o tipo do documento. "
                         "Tente uma palavra só, ou parte dela.",
                         ("Voltar às pastas", _HOME))
    return _explorer_page(
        visit.staff, visit.session, "Busca",
        f"{_crumbs(('Arquivos', _HOME), (f'Busca: {text}', None))}<h1>Busca</h1>"
        f"{_search_form(text)}<div id=results>{listing}</div>{_drop_bar(visit.teams)}",
    )


@router.get("/{slug}/", response_class=HTMLResponse, include_in_schema=False)
async def folder_page(request: Request, slug: str, cliente: str = "",
                      sem_cliente: str = "", legislacao: str = "", pagina: str = "",
                      movido: str = "") -> HTMLResponse:
    _switched_on()
    pool = await get_pool()
    async with pool.connection() as conn:
        visit = await _enter(conn, request)
        if isinstance(visit, HTMLResponse):
            return visit
        folder = _folder_named(slug, visit.teams)
        if folder is None:
            return _explorer_page(
                visit.staff, visit.session, "Pasta indisponível",
                f"{_crumbs(('Arquivos', _HOME), ('Pasta indisponível', None))}"
                f"<h1>Pasta indisponível</h1>"
                + _empty("Esta pasta não está entre as suas",
                         "Ela não existe, ou você não faz mais parte desta equipe.",
                         ("Voltar às pastas", _HOME)),
                status=404,
            )

        if legislacao:
            group = legislacao if legislacao in GROUP_NAMES else None
            if group is None:
                counts = await legislation_counts(conn, visit.scope, folder.kind,
                                                  folder.team_id)
                return _legislation_page(visit, folder, counts)
            page_number = _page_number(pagina)
            cards, has_more = await documents_in(
                conn, visit.scope, folder.kind, folder.team_id, group=group,
                page=page_number, per_page=PER_PAGE,
            )
            return _group_page(visit, folder, group, cards, page_number, has_more, movido)

        no_client = sem_cliente == "1"
        client = cliente if cliente and len(cliente) <= _CLIENT_MAX else None
        if not no_client and not cliente:
            clients = await client_counts(conn, visit.scope, folder.kind, folder.team_id)
            laws = await legislation_counts(conn, visit.scope, folder.kind, folder.team_id)
            return _clients_page(visit, folder, clients, sum(laws.values()))

        page_number = _page_number(pagina)
        if no_client or client is not None:
            cards, has_more = await documents_in(
                conn, visit.scope, folder.kind, folder.team_id, client=client,
                no_client=no_client, page=page_number, per_page=PER_PAGE,
            )
        else:
            cards, has_more = [], False  # longer than any stored name

    shown = "Sem cliente" if no_client else _shown(cliente)
    if cards:
        listing = ("<div class=cards>" + "".join(_card(card) for card in cards) + "</div>"
                   + _pager(folder.href, page_number, has_more,
                            sem_cliente="1" if no_client else None,
                            cliente=None if no_client else cliente))
    else:
        listing = _empty(f"Nenhum documento de “{shown}” aqui",
                         "Eles podem ter sido movidos para outra pasta, ou esta página "
                         "já passou do último.",
                         (f"Voltar para {folder.name}", html.escape(folder.href)))
    # Where a move that took the document out of this person's view lands.
    moved = ("<p class=note>Documento movido. Ele foi para uma pasta que você não "
             "acessa, por isso não aparece mais aqui.</p>" if movido == "1" else "")
    return _explorer_page(
        visit.staff, visit.session, shown,
        f"{_crumbs(('Arquivos', _HOME), (folder.name, html.escape(folder.href)), (shown, None))}"
        f"<h1>{html.escape(shown)}</h1>{moved}{listing}{_drop_bar(visit.teams)}",
    )


def _clients_page(visit: _Visit, folder: Folder, clients: list[tuple[str | None, int]],
                  laws: int) -> HTMLResponse:
    """A folder's tiles: "Legislação" first, always there so everyone learns
    where the laws are, then a tile per client or case."""
    tiles = _tile(_legislation_href(folder), "Uso comum", "Legislação", laws) + "".join(
        _tile(_client_href(folder, client), "Cliente ou caso",
              _shown(client), count, none=client is None)
        for client, count in clients
    )
    listing = f"<div class=tiles>{tiles}</div>"
    total = sum(count for _client, count in clients) + laws
    if not total:
        listing += _empty("Esta pasta está vazia",
                          "Envie um arquivo ao bot no Telegram, ou mova um documento "
                          "para cá com /compartilhar.",
                          ("Voltar às pastas", _HOME))
    return _explorer_page(
        visit.staff, visit.session, folder.name,
        f"{_crumbs(('Arquivos', _HOME), (folder.name, None))}"
        f"<h1>{html.escape(folder.name)}</h1>"
        f"<p class=sub>{_documents(total)}</p>{listing}",
    )


def _legislation_page(visit: _Visit, folder: Folder, counts: dict[str, int]) -> HTMLResponse:
    """One tile per kind of legislation, each always there, empty or not."""
    tiles = "".join(_tile(_legislation_href(folder, key), "Legislação", name,
                          counts.get(key, 0))
                    for key, name, _starts in GROUPS)
    return _explorer_page(
        visit.staff, visit.session, f"Legislação · {folder.name}",
        f"{_crumbs(('Arquivos', _HOME), (folder.name, html.escape(folder.href)), ('Legislação', None))}"
        f"<h1>Legislação</h1>"
        f"<p class=sub>{_documents(sum(counts.values()))} de uso comum, sem cliente</p>"
        f"<div class=tiles>{tiles}</div>",
    )


def _group_page(visit: _Visit, folder: Folder, group: str, cards: list[ExplorerCard],
                page_number: int, has_more: bool, movido: str) -> HTMLResponse:
    name = GROUP_NAMES[group]
    if cards:
        listing = ("<div class=cards>" + "".join(_card(card) for card in cards) + "</div>"
                   + _pager(folder.href, page_number, has_more, legislacao=group))
    else:
        listing = _empty(f"Nenhum documento em “{name}” aqui",
                         "Um documento entra aqui pelo tipo: edite-o e escolha, por "
                         "exemplo, lei, código, decreto ou súmula.",
                         ("Voltar para Legislação", _legislation_href(folder)))
    moved = ("<p class=note>Documento movido. Ele foi para uma pasta que você não "
             "acessa, por isso não aparece mais aqui.</p>" if movido == "1" else "")
    return _explorer_page(
        visit.staff, visit.session, name,
        f"{_crumbs(('Arquivos', _HOME), (folder.name, html.escape(folder.href)), ('Legislação', _legislation_href(folder)), (name, None))}"
        f"<h1>{html.escape(name)}</h1>{moved}{listing}{_drop_bar(visit.teams)}",
    )


# ── One document ────────────────────────────────────────────────────────────


def _document_id(raw: str) -> int | None:
    """ASCII digits only, nine at most, so it always fits the INTEGER column."""
    return int(raw) if re.fullmatch(r"[0-9]{1,9}", raw or "") else None


def _home_of(document: DocumentPreview, teams: list[tuple[int, str]]) -> Folder:
    """The folder a document this person sees sits in."""
    kind = document.card.visibility
    found = next((f for f in _folders(teams) if f.kind == kind
                  and (kind != "team" or f.team_id == document.team_id)), None)
    # A team document read through the scope is in one of the scope's teams;
    # should the two ever disagree, the way back is the firm's shelf.
    return found or _folders([])[-1]


def _client_of(document: DocumentPreview) -> str | None:
    return (document.card.matter or "") if (document.card.matter or "").strip() else None


def _document_crumbs(document: DocumentPreview, folder: Folder, *tail) -> str:
    group = group_of(document.card.doc_type)
    if group:
        return _crumbs(("Arquivos", _HOME), (folder.name, html.escape(folder.href)),
                       ("Legislação", _legislation_href(folder)),
                       (GROUP_NAMES[group], _legislation_href(folder, group)), *tail)
    client = _client_of(document)
    return _crumbs(("Arquivos", _HOME), (folder.name, html.escape(folder.href)),
                   (_shown(client), _client_href(folder, client)), *tail)


def _not_found(visit: _Visit) -> HTMLResponse:
    """One answer for a document that does not exist and one that is not this
    person's to see: telling them apart would say which ids are real."""
    return _explorer_page(
        visit.staff, visit.session, "Documento não encontrado",
        f"{_crumbs(('Arquivos', _HOME), ('Documento não encontrado', None))}"
        f"<h1>Documento não encontrado</h1>"
        + _empty("Ele não está em nenhuma das suas pastas",
                 "Ele não existe, foi movido para uma pasta que você não acessa, ou "
                 "foi apagado.", ("Voltar às pastas", _HOME)),
        status=404,
    )


def _refused(visit: _Visit, reason: str, back: tuple[str, str],
             status: int = 403, heading: str = "Sem permissão") -> HTMLResponse:
    return _explorer_page(
        visit.staff, visit.session, "Sem permissão",
        f"<h1 class=bad>{html.escape(heading)}</h1><p>{html.escape(reason)}</p>"
        f"<div class=actions><a class=quiet href='{back[1]}'>{html.escape(back[0])}</a></div>",
        status=status,
    )


@router.get("/documento/{raw_id}", response_class=HTMLResponse, include_in_schema=False)
async def document_page(request: Request, raw_id: str, movido: str = "",
                        atualizado: str = "") -> HTMLResponse:
    """A document's details and the start of its text, with what can be done.

    Opening it is written to the access log, as reading the text is: the
    start of a contract is already the contract.
    """
    _switched_on()
    pool = await get_pool()
    async with pool.connection() as conn:
        visit = await _enter(conn, request)
        if isinstance(visit, HTMLResponse):
            return visit
        allowed = decide(visit.staff, "document.view_details")
        if not allowed.allowed:
            return _refused(visit, allowed.reason, ("Voltar às pastas", _HOME))
        document_id = _document_id(raw_id)
        document = (await document_preview(conn, document_id, visit.scope,
                                           chars=PREVIEW_CHARS)
                    if document_id is not None else None)
        if document is None:
            return _not_found(visit)
        await log_access(conn, action="view_text", staff_id=visit.staff.id,
                         query="web explorador", document_ids=[document.card.id])
        may_move = (await moving.check_source(conn, visit.staff, visit.scope,
                                              document.card.id)).allowed

    card = document.card
    folder = _home_of(document, visit.teams)
    folder_name = f"Equipe {folder.name}" if folder.kind == "team" else folder.name
    details = [
        ("Tipo", html.escape(card.doc_type or "")),
        (("Esfera", html.escape((card.sphere or "não informada").capitalize()))
         if group_of(card.doc_type) else
         ("Cliente ou caso", html.escape(_shown(_client_of(document))))),
        ("Pasta", f"<a href='{html.escape(folder.href)}'>{html.escape(folder_name)}</a>"),
        ("Enviado por", html.escape(card.uploader_name or "")),
        ("Data", _when(card.created_at)),
    ]
    if card.has_original:
        size = f"{(card.source_size_bytes or 0) / 1024:,.0f} KB".replace(",", ".")
        details.append(("Arquivo original",
                        f"{html.escape(card.source_filename or '')} ({size})"))
    listed = "".join(f"<dt>{label}</dt><dd>{value or '&nbsp;'}</dd>"
                     for label, value in details)

    if document.preview.strip():
        text = (f"<pre class=preview>{html.escape(document.preview.strip())}"
                f"{'…' if document.more else ''}</pre>")
    else:
        text = "<p class=note>Este documento não tem texto extraído para mostrar.</p>"

    actions = [f"<a href='/web/arquivos/{card.id}/texto'>Ler o texto</a>"]
    if may_move:
        actions.append(f"<a class=quiet href='{_DOCUMENT}{card.id}/mover'>Mover para…</a>")
    if decide(visit.staff, "document.edit").allowed:
        actions.append(f"<a class=quiet href='{_DOCUMENT}{card.id}/editar'>Editar</a>")
    if card.has_original and decide(visit.staff, "document.read_original").allowed:
        actions.append(f"<a class=quiet href='/web/arquivos/{card.id}/original'>"
                       f"Baixar o original</a>")
    moved = (f"<p class=note>Documento movido para a pasta {html.escape(folder_name)}.</p>"
             if movido == "1" else "")
    if atualizado == "1":
        moved += "<p class=note>Documento atualizado.</p>"

    return _explorer_page(
        visit.staff, visit.session, card.title,
        f"{_document_crumbs(document, folder, (card.title, None))}"
        f"<article id=document><h1>{html.escape(card.title)}</h1>"
        f"<p class=sub>Documento {card.id}</p>{moved}"
        f"<dl>{listed}</dl><div class=actions>{''.join(actions)}</div>{text}"
        f"<p class=note>Abrir esta página, ler o texto e baixar o original ficam "
        f"registrados no histórico de acessos, como no bot.</p></article>",
    )


# ── Moving ──────────────────────────────────────────────────────────────────


def _destination_of(choice: str, all_teams: list[tuple[int, str, int]]
                    ) -> moving.Destination | None:
    """The folder a form names, by the address word the explorer uses."""
    if choice == "pessoal":
        return moving.PRIVATE
    if choice == "escritorio":
        return moving.FIRM
    found = re.fullmatch(r"equipe-([0-9]{1,9})", choice or "")
    if found:
        team_id = int(found.group(1))
        name = next((name for tid, name, _count in all_teams if tid == team_id), None)
        if name is not None:
            return moving.team(team_id, name)
    return None


def _choice_of(destination: moving.Destination) -> str:
    if destination.kind == "team":
        return f"equipe-{destination.team_id}"
    return "pessoal" if destination.kind == "private" else "escritorio"


def _same_folder(document: DocumentPreview, destination: moving.Destination) -> bool:
    return (document.card.visibility == destination.kind
            and (destination.kind != "team" or document.team_id == destination.team_id))


async def _choices(conn, visit: _Visit, document: DocumentPreview
                   ) -> list[tuple[moving.Destination, bool]]:
    """Every folder this person may move this document into, and whether
    each one would take it out of their sight. Decided by the same service
    the move itself asks, so the list never offers what the move refuses."""
    all_teams = await list_teams(conn)
    candidates = [moving.PRIVATE, *(moving.team(tid, name) for tid, name, _ in all_teams),
                  moving.FIRM]
    offered = []
    for destination in candidates:
        if _same_folder(document, destination):
            continue
        decision = await moving.decide_move(conn, visit.staff, visit.scope,
                                            document.card.id, destination)
        if decision.allowed:
            offered.append((destination, decision.hides_from_mover))
    return offered


@router.get("/documento/{raw_id}/mover", response_class=HTMLResponse,
            include_in_schema=False)
async def move_page(request: Request, raw_id: str) -> HTMLResponse:
    _switched_on()
    pool = await get_pool()
    async with pool.connection() as conn:
        visit = await _enter(conn, request)
        if isinstance(visit, HTMLResponse):
            return visit
        document_id = _document_id(raw_id)
        document = (await document_preview(conn, document_id, visit.scope, chars=1)
                    if document_id is not None else None)
        if document is None:
            return _not_found(visit)
        back = ("Voltar ao documento", f"{_DOCUMENT}{document.card.id}")
        source = await moving.check_source(conn, visit.staff, visit.scope, document.card.id)
        if not source.allowed:
            return _refused(visit, source.reason, back)
        choices = await _choices(conn, visit, document)

    card = document.card
    folder = _home_of(document, visit.teams)
    if choices:
        options = "".join(
            f"<label class=choice><input type=radio name=destino "
            f"value='{_choice_of(destination)}' required>"
            f"<span>{html.escape(destination.label[:1].upper() + destination.label[1:])}"
            + ("<small class=bad>Você deixará de ver este documento.</small>" if hides else "")
            + "</span></label>"
            for destination, hides in choices
        )
        form = (f"<form method=post action='{_DOCUMENT}{card.id}/mover' class=move>"
                f"<input type=hidden name=csrf value='{csrf_for(visit.session)}'>"
                f"<fieldset><legend>Para qual pasta?</legend>{options}</fieldset>"
                f"<button type=submit>Mover</button></form>")
    else:
        form = _empty("Não há outra pasta para onde você possa movê-lo",
                      "Você só move para as suas equipes, e só quem enviou o documento "
                      "o torna pessoal de novo.", back)
    return _explorer_page(
        visit.staff, visit.session, f"Mover {card.title}",
        f"{_document_crumbs(document, folder, (card.title, f'{_DOCUMENT}{card.id}'), ('Mover', None))}"
        f"<h1>Mover “{html.escape(card.title)}”</h1>"
        f"<p class=sub>Hoje na pasta {html.escape(folder.name)}</p>{form}"
        f"<div class=actions><a class=quiet href='{back[1]}'>{back[0]}</a></div>",
    )


@router.post("/documento/{raw_id}/mover", include_in_schema=False)
async def move_document_now(request: Request, raw_id: str):
    """Move it, deciding again now: the page that offered the destination may
    be minutes old, and a form token proves where the request came from, not
    that it is still allowed."""
    _switched_on()
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
        visit = await _enter(conn, request)
        if isinstance(visit, HTMLResponse):
            return visit
        document_id = _document_id(raw_id)
        document = (await document_preview(conn, document_id, visit.scope, chars=1)
                    if document_id is not None else None)
        if document is None:
            return _not_found(visit)
        back = ("Voltar ao documento", f"{_DOCUMENT}{document.card.id}")
        destination = _destination_of(form.get("destino", ""), await list_teams(conn))
        if destination is None:
            return _refused(visit, "Escolha uma das pastas da lista.", back)
        origin = _home_of(document, visit.teams)
        client = _client_of(document)
        hides = destination.kind == "team" and destination.team_id not in visit.scope.team_ids
        if hides and form.get("visivel") == "1":
            # A drag, which never warns: it only ever offers the person's own
            # folders. A team they left since the page loaded would take the
            # document out of their sight unannounced, so the drag stops here
            # and the page that does warn is where that move is made.
            return _refused(visit, "Você não está mais nesta equipe. Para mover para ela, "
                            "use “Mover para…” no documento.", back)
        result = await moving.move(conn, visit.staff, visit.scope, document.card.id,
                                   destination)
        if not result.moved:
            return _refused(visit, result.reason, back)

    # A team the mover is not in takes the document out of their sight: they
    # land where it was, told why it is gone, not on a page that now says
    # "not found".
    if hides:
        where = (_url(origin.href, sem_cliente="1", movido="1") if client is None
                 else _url(origin.href, cliente=client, movido="1"))
    else:
        where = _url(f"{_DOCUMENT}{document.card.id}", movido="1")
    return RedirectResponse(where, status_code=303)


# ── Editing how a document is described ─────────────────────────────────────


def _suggestions(list_id: str, names: list[str]) -> str:
    options = "".join(f"<option value='{html.escape(name)}'></option>" for name in names)
    return f"<datalist id={list_id}>{options}</datalist>"


async def _described(conn, visit: _Visit, raw_id: str):
    """The document, the clients of its folder and the types in use: what the
    edit page shows and what its POST keeps exactly when picked."""
    document_id = _document_id(raw_id)
    document = (await document_preview(conn, document_id, visit.scope, chars=1)
                if document_id is not None else None)
    if document is None:
        return None, [], []
    folder = _home_of(document, visit.teams)
    clients = [client for client, _count in
               await client_counts(conn, visit.scope, folder.kind, folder.team_id) if client]
    in_use = [kind for kind, _count in await list_doc_types(conn, scope=visit.scope) if kind]
    # The usual kinds of legislation first, then whatever the archive uses.
    kinds = list(dict.fromkeys([*TYPE_SUGGESTIONS, *in_use]))
    return document, clients, kinds


@router.get("/documento/{raw_id}/editar", response_class=HTMLResponse,
            include_in_schema=False)
async def edit_page(request: Request, raw_id: str) -> HTMLResponse:
    """The document's name, type and client or case, to change in one form.
    The types in use and the clients of its folder are offered as
    suggestions, so choosing an existing one does not depend on typing it
    exactly. An empty client is "Sem cliente"."""
    _switched_on()
    pool = await get_pool()
    async with pool.connection() as conn:
        visit = await _enter(conn, request)
        if isinstance(visit, HTMLResponse):
            return visit
        document, clients, kinds = await _described(conn, visit, raw_id)
        if document is None:
            return _not_found(visit)
        back = ("Voltar ao documento", f"{_DOCUMENT}{document.card.id}")
        allowed = decide(visit.staff, "document.edit")
        if not allowed.allowed:
            return _refused(visit, allowed.reason, back)

    card = document.card
    folder = _home_of(document, visit.teams)
    opened = {"titulo": card.title, "tipo": card.doc_type or "",
              "cliente": _client_of(document) or "", "esfera": card.sphere or ""}
    # What each field held as the page opened travels with the form: the POST
    # writes only what the person changed, and only if nobody changed it since.
    snapshot = "".join(f"<input type=hidden name=antes_{name} value='{html.escape(value)}'>"
                       for name, value in opened.items())
    # No maxlength on these fields: a browser counts it in UTF-16 units and
    # would cut a name the column holds. The limits are the server's, and it
    # says which one was passed.
    spheres = "".join(
        f"<option value='{value}'{' selected' if value == (card.sphere or '') else ''}>"
        f"{label}</option>"
        for value, label in (("", "Não informada"), *((s, s.capitalize()) for s in SPHERES))
    )
    # The script hides the client and shows the sphere when the type is a
    # kind of legislation, and the other way round; it reads which types
    # those are from here, the same list the server files them by. Without
    # the script both show, and the server applies the same rule.
    starts = html.escape(json.dumps(list(PREFIXES)))
    form = (
        f"<form method=post action='{_DOCUMENT}{card.id}/editar' class=edit "
        f"data-legislacao='{starts}'>"
        f"<input type=hidden name=csrf value='{csrf_for(visit.session)}'>{snapshot}"
        f"<label for=titulo>Nome</label>"
        f"<input id=titulo name=titulo required autocomplete=off "
        f"value='{html.escape(card.title)}'>"
        f"<label for=tipo>Tipo</label>"
        f"<input id=tipo name=tipo list=tipos autocomplete=off placeholder='Sem tipo' "
        f"value='{html.escape(card.doc_type or '')}'>{_suggestions('tipos', kinds)}"
        f"<div data-campo=cliente><label for=cliente>Cliente ou caso</label>"
        f"<input id=cliente name=cliente list=clientes autocomplete=off "
        f"placeholder='Sem cliente' value='{html.escape(_client_of(document) or '')}'>"
        f"{_suggestions('clientes', clients)}"
        f"<p class=hint>Em branco, o documento fica em “Sem cliente”. Leis, códigos, "
        f"decretos e súmulas não têm cliente: ficam em “Legislação”.</p></div>"
        f"<div data-campo=esfera><label for=esfera>Esfera</label>"
        f"<select id=esfera name=esfera>{spheres}</select>"
        f"<p class=hint>Só para legislação: aparece no cartão do documento.</p></div>"
        f"<button type=submit>Salvar</button></form>"
    )
    return _explorer_page(
        visit.staff, visit.session, f"Editar {card.title}",
        f"{_document_crumbs(document, folder, (card.title, f'{_DOCUMENT}{card.id}'), ('Editar', None))}"
        f"<h1>Editar “{html.escape(card.title)}”</h1>{form}"
        f"<p class=note>O documento continua na mesma pasta: muda só como ele se chama e "
        f"onde aparece dentro dela. Fica registrado no histórico, como o /editar do bot.</p>"
        f"<div class=actions><a class=quiet href='{back[1]}'>{back[0]}</a></div>",
    )


@router.post("/documento/{raw_id}/editar", include_in_schema=False)
async def edit_now(request: Request, raw_id: str):
    """Write what the person changed, and only that.

    A field sent back exactly as the page showed it was not touched: it is
    neither written nor tidied, so fixing the client never rewrites the name
    or the type, nor undoes a colleague's edit made since the page opened.
    A field the person did change is written only if it still holds what the
    page showed; otherwise the colleague's edit stands and the person is told.
    """
    _switched_on()
    if from_another_site(request):
        return refused_from_another_site()
    form = await read_form(request, max_bytes=_EDIT_BODY)
    token = request.cookies.get(COOKIE) or ""
    if not csrf_matches(token, form.get("csrf", "")):
        return page("Pedido recusado",
                    "<h1 class=bad>Pedido recusado</h1>"
                    "<p>Recarregue a página e tente de novo.</p>", status=403)
    pool = await get_pool()
    async with pool.connection() as conn:
        visit = await _enter(conn, request)
        if isinstance(visit, HTMLResponse):
            return visit
        document, clients, kinds = await _described(conn, visit, raw_id)
        if document is None:
            return _not_found(visit)
        card = document.card
        back = ("Voltar", f"{_DOCUMENT}{card.id}/editar")

        changes: dict[str, str | None] = {}
        expected: dict[str, str | None] = {}
        for field, name, known in (("title", "titulo", []), ("doc_type", "tipo", kinds),
                                   ("matter", "cliente", clients),
                                   ("sphere", "esfera", list(SPHERES))):
            typed, before = form.get(name, ""), form.get(f"antes_{name}")
            if before is None:
                # A form from before this page carried what it showed: it
                # cannot say what was touched, so it writes nothing.
                return _refused(visit, "Esta página está desatualizada. Abra a edição de "
                                "novo.", back, status=409, heading="Não foi salvo")
            if typed == before:
                continue
            # A suggestion picked from the list exists already: kept exactly
            # as stored, odd spacing and all, or it would become a second tile
            # beside the one it was meant to join. Anything typed is tidied.
            value = typed if typed in known else moving.tidy(typed)
            if value == (before if field == "title" else before or None):
                continue
            changes[field] = value
            expected[field] = before if field == "title" else (before or None)
        # Legislation has no client and anything else has no sphere. What
        # the type becomes decides which of the two applies; the other is
        # ignored as sent, and emptied only when the type crosses that line
        # (a contract becoming a law, a law becoming a contract). Renaming a
        # law, or "lei" becoming "lei complementar", touches nothing else.
        opened_type = form.get("antes_tipo", "")
        final_type = changes["doc_type"] if "doc_type" in changes else opened_type
        was_law, is_law = bool(group_of(opened_type)), bool(group_of(final_type))
        drop, emptied = ("matter", "cliente") if is_law else ("sphere", "esfera")
        changes.pop(drop, None)
        expected.pop(drop, None)
        before_dropped = form.get(f"antes_{emptied}", "")
        if is_law != was_law:
            # Crossing the line: the field that no longer applies is emptied
            # if it held something, and in any case checked by the write, so
            # a client a colleague set meanwhile cannot stay on a law.
            if before_dropped:
                changes[drop] = None
            expected[drop] = before_dropped or None
        # Which of the two fields applies was decided from the type as the
        # page opened; the write checks that type is still there, so a
        # colleague retyping the document meanwhile cannot leave a client on
        # a law or a sphere on a contract.
        if ("matter" in changes or "sphere" in changes) and "doc_type" not in expected:
            expected["doc_type"] = opened_type or None
        if not changes:
            return RedirectResponse(f"{_DOCUMENT}{card.id}", status_code=303)
        result = await moving.edit_document(conn, visit.staff, visit.scope, card.id,
                                            changes, expected=expected)
        if not result.changed:
            if result.conflict:
                return _refused(visit, result.reason, back, status=409,
                                heading="Não foi salvo")
            return _refused(visit, result.reason, back)
    return RedirectResponse(_url(f"{_DOCUMENT}{card.id}", atualizado="1"), status_code=303)


__all__ = ["PER_PAGE", "POLICY", "PREVIEW_CHARS", "SCRIPT", "router"]
