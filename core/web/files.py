"""The archive in a browser: the same documents the bot shows, and no others.

**What a person sees here is decided by the same scope the bot uses**, built by
`scope_for` and applied by `visibility_clause` inside each query. This page
adds no rule of its own about who sees what. A second rule would be a second
door, and the one written later is the one that drifts: the folders exist so a
client's papers stay between a lawyer and that client, and that promise has to
hold identically on every way in.

What each action needs is the bot's rule too, asked of `decide` by action
name: browsing is open to every rank, the original file to lawyers and above,
exactly as `/arquivo` is.

Two things are written down, as in the bot: reading a document's text, and
taking its original. The original is recorded twice, because they are two
facts: that it was decrypted on this host for this person, and that it was
handed to their browser.

The original is always sent as an attachment, never shown inline. Anything
can arrive in the archive, an HTML page or a PDF with scripts in it among
them, and a file rendered by the browser on this host would run with this
host's session in reach.
"""
from __future__ import annotations

import html
import logging
import re
import unicodedata
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse

from core.config import settings
from core.models import StaffMember
from core.repositories.connection import get_pool
from core.repositories.documents import (
    count_documents,
    document_card,
    get_document_source,
    list_doc_types,
    log_access,
    read_document_text,
    search_documents,
)
from core.repositories.teams import scope_for
from core.schedule import local_now
from core.services import document_storage
from core.services.authorization import decide
from core.web.pages import HEADERS, page, remember_theme
from core.web.staff_web import current_staff, nav, signed_out, unusable

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/web/arquivos", tags=["web"], dependencies=[Depends(remember_theme)])

# The words in the address bar, mapped to the folders the database knows.
FOLDERS = {"minha": "private", "equipe": "team", "escritorio": "firm"}
_FOLDER_NAMES = {"private": "Pessoal", "team": "Equipe", "firm": "Escritório"}

# A page is a list to scan, not an export. Past this, the filters are the way
# in, and the page says so rather than quietly cutting.
LIST_LIMIT = 100
# A text past this is shown in part. A whole statute can be megabytes, and a
# browser tab is not where it should be read end to end.
TEXT_LIMIT = 400_000
_FILTER_MAX = 100


def _clean(value: str) -> str:
    return (value or "").strip()[:_FILTER_MAX]


def _document_id(raw: str) -> int | None:
    """An id from the address, or None. ASCII digits only: `str.isdigit` also
    accepts characters like ² that `int` then refuses, which was a 500.
    Nine digits at most, so it always fits the INTEGER column."""
    return int(raw) if re.fullmatch(r"[0-9]{1,9}", raw) else None


def _when(value) -> str:
    return f"{local_now(value):%d/%m/%Y}" if value else ""


def _folder_tag(visibility: str | None) -> str:
    name = _FOLDER_NAMES.get(visibility or "", "")
    if not name:
        return ""
    return f"<span class='tag {html.escape(visibility or '')}'>{name}</span>"


def _not_found(staff: StaffMember, session: str) -> HTMLResponse:
    """One answer for a document that does not exist and one that is not
    this person's to see. Telling them apart would say which ids are real."""
    return page(
        "Documento não encontrado",
        "<h1>Documento não encontrado</h1>"
        "<p>Ele não existe ou não está em uma pasta que você acessa.</p>"
        "<div class=actions><a class=quiet href='/web/arquivos/'>Voltar aos arquivos</a></div>",
        status=404, wide=True, navigation=nav(staff, session),
    )


def _refused(staff: StaffMember, session: str, reason: str) -> HTMLResponse:
    return page(
        "Sem permissão",
        f"<h1 class=bad>Sem permissão</h1>"
        f"<p>{html.escape(reason)}</p>",
        status=403, wide=True, navigation=nav(staff, session),
    )


def _option(value: str, label: str, chosen: str) -> str:
    selected = " selected" if value == chosen else ""
    return f"<option value='{html.escape(value)}'{selected}>{html.escape(label)}</option>"


def _filters(q: str, client: str, kind: str, folder: str,
             kinds: list[tuple[str, int]]) -> str:
    kind_options = _option("", "Todos", kind) + "".join(
        _option(name, f"{name} ({count})", kind) for name, count in kinds if name
    )
    folder_options = _option("", "Todas", folder) + "".join(
        _option(key, _FOLDER_NAMES[value], folder) for key, value in FOLDERS.items()
    )
    return (
        "<form method=get action='/web/arquivos/' class=filters>"
        f"<div><label for=q>Título</label>"
        f"<input id=q name=q value='{html.escape(q)}' maxlength={_FILTER_MAX} autocomplete=off></div>"
        f"<div><label for=cliente>Cliente ou caso</label>"
        f"<input id=cliente name=cliente value='{html.escape(client)}' "
        f"maxlength={_FILTER_MAX} autocomplete=off></div>"
        f"<div><label for=tipo>Tipo</label><select id=tipo name=tipo>{kind_options}</select></div>"
        f"<div><label for=pasta>Pasta</label><select id=pasta name=pasta>{folder_options}</select></div>"
        "<button type=submit>Filtrar</button></form>"
    )


def _listing(documents, *, filtered: bool) -> str:
    """The table, or an empty state that says why it is empty and what next.

    An empty page with no word on it reads as broken. With filters it is the
    filters, and the way back is one click; without, nothing has reached this
    person's folders yet, and the bot is how something does.
    """
    if documents:
        rows = "".join(
            f"<tr><td class=num>{doc.id}</td>"
            f"<td><a href='/web/arquivos/{doc.id}'>{html.escape(doc.title)}</a></td>"
            f"<td>{html.escape(doc.doc_type or '')}</td>"
            f"<td>{html.escape(doc.matter or '')}</td>"
            f"<td>{_folder_tag(doc.visibility)}</td>"
            f"<td class=num>{_when(doc.created_at)}</td></tr>"
            for doc in documents
        )
        return (
            "<div class=scroll><table><thead><tr><th>Id</th><th>Título</th>"
            "<th>Tipo</th><th>Cliente ou caso</th><th>Pasta</th><th>Data</th>"
            f"</tr></thead><tbody>{rows}</tbody></table></div>"
        )
    if filtered:
        return ("<div class=empty><strong>Nada com esses filtros</strong>"
                "<p>Tente um trecho menor do título, outro cliente ou outra pasta.</p>"
                "<div class=actions><a class=quiet href='/web/arquivos/'>Limpar filtros</a>"
                "</div></div>")
    return ("<div class=empty><strong>Nenhum documento ainda</strong>"
            "<p>Os arquivos da sua pasta, das suas equipes e do escritório aparecem "
            "aqui. Envie um arquivo ao bot no Telegram e ele surge nesta lista.</p></div>")


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def list_files(request: Request, q: str = "", cliente: str = "",
                     tipo: str = "", pasta: str = "") -> HTMLResponse:
    if not settings.web_enabled:
        return unusable()
    q, client, kind = _clean(q), _clean(cliente), _clean(tipo)
    folder = pasta if pasta in FOLDERS else ""
    pool = await get_pool()
    async with pool.connection() as conn:
        signed_in = await current_staff(conn, request)
        if signed_in is None:
            return signed_out(request)
        staff, session = signed_in
        decision = decide(staff, "document.browse")
        if not decision.allowed:
            return _refused(staff, session, decision.reason)
        scope = await scope_for(conn, staff.id)
        documents = await search_documents(
            conn, title=q or None, matter=client or None, doc_type=kind or None,
            visibility=FOLDERS.get(folder), limit=LIST_LIMIT, scope=scope,
        )
        total = await count_documents(conn, scope)
        kinds = await list_doc_types(conn, scope=scope)

    filtered = bool(q or client or kind or folder)
    listing = _listing(documents, filtered=filtered)

    if len(documents) == LIST_LIMIT:
        count_line = (f"Mostrando os {LIST_LIMIT} mais recentes de {total}. "
                      f"Use os filtros para chegar aos outros.")
    elif filtered:
        count_line = f"{len(documents)} de {total} documentos."
    else:
        count_line = f"{total} documentos."

    return page(
        "Arquivos",
        f"<h1>Arquivos</h1>"
        f"<p class=sub>{count_line}</p>"
        f"{_filters(q, client, kind, folder, kinds)}{listing}",
        wide=True, navigation=nav(staff, session),
    )


@router.get("/{raw_id}", response_class=HTMLResponse, include_in_schema=False)
async def show_file(request: Request, raw_id: str) -> HTMLResponse:
    if not settings.web_enabled:
        return unusable()
    pool = await get_pool()
    async with pool.connection() as conn:
        signed_in = await current_staff(conn, request)
        if signed_in is None:
            return signed_out(request)
        staff, session = signed_in
        decision = decide(staff, "document.view_details")
        if not decision.allowed:
            return _refused(staff, session, decision.reason)
        document_id = _document_id(raw_id)
        scope = await scope_for(conn, staff.id)
        card = (await document_card(conn, document_id, scope)
                if document_id is not None else None)
        if card is None:
            return _not_found(staff, session)

    folder_text = _folder_tag(card.visibility)
    if card.visibility == "team" and card.team_name:
        folder_text += f" {html.escape(card.team_name)}"
    details = [
        ("Tipo", html.escape(card.doc_type or "")),
        ("Cliente ou caso", html.escape(card.matter or "")),
        ("Pasta", folder_text),
        ("Enviado por", html.escape(card.uploader_name or "")),
        ("Data", _when(card.created_at)),
    ]
    if card.has_original:
        size = f"{(card.source_size_bytes or 0) / 1024:,.0f} KB".replace(",", ".")
        details.append(("Arquivo original",
                        f"{html.escape(card.source_filename or '')} ({size})"))
    listed = "".join(f"<dt>{label}</dt><dd>{value or '&nbsp;'}</dd>"
                     for label, value in details)

    actions = [f"<a href='/web/arquivos/{card.id}/texto'>Ler o texto</a>"]
    if card.has_original and decide(staff, "document.read_original").allowed:
        actions.append(f"<a class=quiet href='/web/arquivos/{card.id}/original'>"
                       f"Baixar o original</a>")
    back = "<a class=quiet href='/web/arquivos/'>Voltar aos arquivos</a>"

    return page(
        card.title,
        f"<p class=sub>Documento {card.id}</p>"
        f"<h1>{html.escape(card.title)}</h1><dl>{listed}</dl>"
        f"<div class=actions>{''.join(actions)}{back}</div>"
        f"<p class=note>Ler o texto e baixar o original ficam registrados no "
        f"histórico de acessos, como no bot.</p>",
        wide=True, navigation=nav(staff, session),
    )


@router.get("/{raw_id}/texto", response_class=HTMLResponse, include_in_schema=False)
async def read_text(request: Request, raw_id: str) -> HTMLResponse:
    if not settings.web_enabled:
        return unusable()
    pool = await get_pool()
    async with pool.connection() as conn:
        signed_in = await current_staff(conn, request)
        if signed_in is None:
            return signed_out(request)
        staff, session = signed_in
        decision = decide(staff, "document.view_details")
        if not decision.allowed:
            return _refused(staff, session, decision.reason)
        document_id = _document_id(raw_id)
        scope = await scope_for(conn, staff.id)
        document = (await read_document_text(conn, document_id, limit=TEXT_LIMIT,
                                             scope=scope)
                    if document_id is not None else None)
        if document is None:
            return _not_found(staff, session)
        await log_access(conn, action="view_text", staff_id=staff.id,
                         query="web", document_ids=[document.id])

    cut = document.length > len(document.text)
    shown = document.text
    notice = (f"<p class=note>Mostrando o começo do texto ({len(shown):,} de "
              f"{document.length:,} caracteres).</p>".replace(",", ".") if cut else "")
    return page(
        document.title,
        f"<p class=sub><a href='/web/arquivos/{document.id}'>"
        f"Documento {document.id}</a></p><h1>{html.escape(document.title)}</h1>"
        f"{notice}<pre>{html.escape(shown) or 'Sem texto extraído.'}</pre>",
        wide=True, navigation=nav(staff, session),
    )


def attachment_disposition(filename: str) -> str:
    """Content-Disposition that keeps the name and cannot be bent into a header.

    The plain `filename` is ASCII with anything unusual replaced, so no quote,
    semicolon or line break from an uploaded name reaches the header. The real
    name, accents included, travels percent-encoded in `filename*`.
    """
    name = filename or "documento"
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    plain = re.sub(r"[^A-Za-z0-9._ -]", "_", folded).strip() or "documento"
    return f"attachment; filename=\"{plain}\"; filename*=UTF-8''{quote(name, safe='')}"


@router.get("/{raw_id}/original", include_in_schema=False)
async def download_original(request: Request, raw_id: str):
    if not settings.web_enabled:
        return unusable()
    pool = await get_pool()
    async with pool.connection() as conn:
        signed_in = await current_staff(conn, request)
        if signed_in is None:
            return signed_out(request)
        staff, session = signed_in
        decision = decide(staff, "document.read_original")
        if not decision.allowed:
            return _refused(staff, session, decision.reason)
        document_id = _document_id(raw_id)
        scope = await scope_for(conn, staff.id)
        source = (await get_document_source(conn, document_id, scope)
                  if document_id is not None else None)
        if source is None:
            return _not_found(staff, session)
        try:
            data = await document_storage.read(
                source.source_storage_key, source.source_wrapped_key,
                raw_sha256=source.source_sha256 or "",
            )
        except Exception:
            logger.exception("Could not read the original of document %s", source.id)
            data = None
        if not data:
            return page(
                "Original indisponível",
                "<h1 class=bad>Não localizei o original</h1>"
                "<p>Avise o administrador para verificar o armazenamento.</p>",
                status=404, wide=True, navigation=nav(staff, session),
            )
        await log_access(conn, action="decrypt_original", staff_id=staff.id,
                         query="web", document_ids=[source.id])
        await log_access(conn, action="download_original", staff_id=staff.id,
                         query="web", document_ids=[source.id])

    headers = dict(HEADERS)
    headers["Content-Disposition"] = attachment_disposition(source.source_filename or "")
    # The file is never a page: no script runs if a browser renders it anyway.
    headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return Response(content=data, media_type="application/octet-stream", headers=headers)


__all__ = ["FOLDERS", "LIST_LIMIT", "TEXT_LIMIT", "attachment_disposition", "router"]
