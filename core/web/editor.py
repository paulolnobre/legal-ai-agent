"""The editor: a sandbox to adjust what the assistant generated.

A .docx or .xlsx the assistant offered (sealed in `panel_files` for its
owner, for a day) opens here as text or as a grid. The person types, sees how
the text will look and downloads the result. Nothing is kept: no draft, no
copy, no autosave, and nothing is sent to a model. The text lives in the
page, which warns before it is left.

Every route decides again, in the query, whether the token is this person's
and alive, so a page left open past the file's day stops working. Anyone
else's token, an expired one and one that does not exist are the same 404.

Every part works without a script: the buttons are real form buttons. With
`static/editor.js` (this page, and only this page here, runs a script from
this host) there are the formatting buttons, rows and columns to add, and the
warning on leaving.
"""
from __future__ import annotations

import asyncio
import html
from pathlib import Path, PurePath

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from core.config import settings
from core.models import StaffMember
from core.repositories import panel
from core.repositories.connection import get_pool
from core.repositories.documents import log_access
from core.services import editing, exporting
from core.web.files import attachment_disposition
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

router = APIRouter(prefix="/web/editor", tags=["web"], dependencies=[Depends(remember_theme)])

HOME = "/web/editor/"
SCRIPT = f"{HOME}editor.js"
_SCRIPT_BYTES = (Path(__file__).parent / "static" / "editor.js").read_bytes()
POLICY = (HEADERS["Content-Security-Policy"]
          + "; script-src 'self'; connect-src 'self'")
# The longest text, each character up to four UTF-8 bytes and each byte "%XX"
# in the form, and the small fields. A body over it is not read.
_BODY = editing.MAX_TEXT * 4 * 3 + 4096
_TOKEN_MAX = 100
# What a person's own browser and iOS may correct, in Portuguese. Never a model.
_SPELL = "lang=pt-BR spellcheck=true autocorrect=on autocapitalize=sentences"

NOTES = {
    "not_editable": "Este arquivo não pode ser editado.",
    "too_big": ("O documento passa do limite do editor: 200 mil caracteres de texto, "
                "200 linhas por 20 colunas e 2 mil caracteres por célula."),
    "fields": "Confira os campos e tente de novo.",
    "refused": "Recarregue a página e tente de novo.",
}

STYLE = """
.editor-form{display:grid;gap:1rem;margin-top:1rem}
.editor-form label{display:grid;gap:.3rem;font-size:.85rem}
.editor-form input,.editor-form textarea,.editor-form select{margin:0}
.editor-form textarea{min-height:22rem;line-height:1.55;resize:vertical;font:inherit}
.editor-bar{display:flex;flex-wrap:wrap;gap:.5rem}
.editor-bar[hidden]{display:none}
.editor-bar button,.editor-actions button{width:auto;margin:0;min-height:2.4rem;padding:.4rem .9rem}
.editor-actions{display:flex;flex-wrap:wrap;gap:.7rem;align-items:end}
.editor-actions label{min-width:9rem}
.grid-scroll{overflow-x:auto;border:1px solid var(--line);border-radius:var(--radius-lg)}
.grid{border-collapse:collapse}
.grid td{padding:0;border:1px solid var(--line)}
.grid input{width:11rem;min-height:2.6rem;border:0;border-radius:0;background:transparent;
 padding:.4rem .6rem}
.preview{margin-top:2rem;padding:1.2rem 1.4rem;border:1px solid var(--line);
 border-radius:var(--radius-lg);background:var(--panel);line-height:1.6;overflow-wrap:anywhere}
.preview .doc-title{font-size:1.4rem;font-weight:600;margin:0 0 1rem}
.preview .disclaimer{margin-top:1.5rem;font-style:italic;color:var(--muted);font-size:.85rem}
"""


def _switched_on() -> None:
    if not settings.web_enabled or settings.web_assistant == "off":
        raise HTTPException(status_code=404)


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _shell(staff: StaffMember, session: str, title: str, body: str, *,
           status: int = 200) -> HTMLResponse:
    return page(title, body, wide=True, style=STYLE, status=status,
                headers={"Content-Security-Policy": POLICY},
                head=app_lock_meta(staff), navigation=nav(staff, session))


def _note(staff: StaffMember, session: str, key: str, status: int) -> HTMLResponse:
    return _shell(staff, session, "Editor",
                  f"<h1>Editor</h1><p class=note role=alert>{_e(NOTES[key])}</p>"
                  "<p><a href='/web/assistente/'>Voltar ao assistente</a></p>", status=status)


async def _enter(conn, request: Request) -> tuple[StaffMember, str] | Response:
    signed_in = await current_staff(conn, request)
    if signed_in is None:
        return signed_out(request)
    staff, session = signed_in
    if not assistant_open_to(staff):
        raise HTTPException(status_code=404)
    return staff, session


def _kind(filename: str) -> str | None:
    suffix = PurePath(filename).suffix.lower()
    return {".docx": "text", ".xlsx": "sheet"}.get(suffix)


# ── Rendering ───────────────────────────────────────────────────────────────


def _intro() -> str:
    return ("<p class=sub>Um rascunho: nada é guardado. O arquivo só existe se você tocar em "
            "Baixar, e o que você digitar aqui se perde ao sair da página.</p>")


def _title_field(title: str) -> str:
    return (f"<label>Título<input name=titulo value='{_e(title)}' "
            f"maxlength={editing.MAX_TITLE} {_SPELL}></label>")


def _text_form(token: str, csrf: str, title: str, text: str, *, dirty: bool) -> str:
    base = f"{HOME}{_e(token)}"
    return (
        f"<form class=editor-form id=editor method=post action='{base}/ver'"
        f"{' data-dirty=1' if dirty else ''}>"
        f"<input type=hidden name=csrf value='{_e(csrf)}'>"
        f"{_title_field(title)}"
        "<div class=editor-bar data-toolbar hidden>"
        "<button type=button class=quiet data-mark='**' aria-label='Negrito'><b>N</b></button>"
        "<button type=button class=quiet data-line='# '>Título</button>"
        "<button type=button class=quiet data-line='- '>Lista</button>"
        "<button type=button class=quiet data-line='1. '>Lista numerada</button></div>"
        f"<label>Texto<textarea name=texto rows=22 {_SPELL}>{html.escape(text)}</textarea></label>"
        "<div class=editor-actions>"
        f"<button type=submit class=quiet formaction='{base}/ver'>Ver como ficou</button>"
        "<label>Baixar como<select name=formato>"
        "<option value=docx>Word (.docx)</option><option value=pdf>PDF</option></select></label>"
        f"<button type=submit class=primary formaction='{base}/baixar'>Baixar</button>"
        "</div></form>")


def _grid_form(token: str, csrf: str, title: str, rows: list[list[str]]) -> str:
    height = max(len(rows), 1)
    width = max((len(row) for row in rows), default=0) or 3
    body = []
    for r in range(height):
        row = rows[r] if r < len(rows) else []
        cells = "".join(
            f"<td><input name=c{r}_{c} value='{_e(row[c] if c < len(row) else '')}' "
            f"maxlength={editing.MAX_CELL} {_SPELL} "
            f"aria-label='Linha {r + 1}, coluna {chr(65 + c)}'></td>"
            for c in range(width))
        body.append(f"<tr>{cells}</tr>")
    return (
        f"<form class=editor-form id=editor method=post action='{HOME}{_e(token)}/baixar'>"
        f"<input type=hidden name=csrf value='{_e(csrf)}'>"
        "<input type=hidden name=formato value=xlsx>"
        f"<input type=hidden name=linhas value={height}>"
        f"<input type=hidden name=colunas value={width}>"
        f"{_title_field(title)}"
        "<div class=editor-bar data-grid-bar hidden>"
        "<button type=button class=quiet data-add-row>Adicionar linha</button>"
        "<button type=button class=quiet data-remove-row>Remover linha</button>"
        "<button type=button class=quiet data-add-column>Adicionar coluna</button>"
        "<button type=button class=quiet data-remove-column>Remover coluna</button></div>"
        f"<div class=grid-scroll><table class=grid data-grid data-max-rows={editing.MAX_ROWS} "
        f"data-max-cols={editing.MAX_COLS} data-max-cell={editing.MAX_CELL}>"
        f"{''.join(body)}</table></div>"
        "<div class=editor-actions><button type=submit class=primary>Baixar (.xlsx)</button>"
        "</div></form>")


def _preview(title: str, text: str) -> str:
    """The document as it will download, from the same blocks the file is made
    from. Every value is escaped; bold is the only markup written."""
    def inline(raw: str) -> str:
        return "".join(f"<strong>{_e(part)}</strong>" if bold else _e(part)
                       for part, bold in exporting.bold_runs(raw))

    out = [f"<p class=doc-title>{_e(title or 'Resposta')}</p>"]
    open_list = ""
    for block in exporting.parse_blocks(text):
        wanted = {"bullet": "ul", "numbered": "ol"}.get(block.kind, "")
        if open_list and open_list != wanted:
            out.append(f"</{open_list}>")
            open_list = ""
        if wanted and not open_list:
            out.append(f"<{wanted}>")
            open_list = wanted
        if block.kind == "heading":
            level = min(block.level + 1, 4)
            out.append(f"<h{level}>{_e(exporting.strip_markdown(block.text))}</h{level}>")
        elif wanted:
            out.append(f"<li>{inline(block.text)}</li>")
        else:
            out.append(f"<p>{inline(block.text)}</p>")
    if open_list:
        out.append(f"</{open_list}>")
    out.append(f"<p class=disclaimer>{_e(exporting.DISCLAIMER)}</p>")
    return f"<section class=preview aria-label='Como vai ficar'>{''.join(out)}</section>"


def _page(staff: StaffMember, session: str, form: str, preview: str = "") -> HTMLResponse:
    return _shell(staff, session, "Editor",
                  f"<h1>Editar</h1>{_intro()}{form}{preview}"
                  f"<script type=module src='{SCRIPT}'></script>")


# ── Routes ──────────────────────────────────────────────────────────────────


@router.get("/editor.js", include_in_schema=False)
async def script() -> Response:
    """The page's script: it holds nothing about anyone, so it needs no
    session; off, it does not exist, like the page."""
    _switched_on()
    return Response(_SCRIPT_BYTES, media_type="text/javascript; charset=utf-8",
                    headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})


@router.get("/{token}", response_class=HTMLResponse, include_in_schema=False)
async def edit(request: Request, token: str):
    _switched_on()
    if len(token) > _TOKEN_MAX:
        raise HTTPException(status_code=404)
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _enter(conn, request)
        if isinstance(entered, Response):
            return entered
        staff, session = entered
        name = await panel.file_name(conn, token, staff.id)
        if name is None:
            raise HTTPException(status_code=404)
        kind = _kind(name)
        if kind is None:
            return _note(staff, session, "not_editable", 422)
        found = await panel.read_file(conn, token, staff_id=staff.id)
        if found is None:
            raise HTTPException(status_code=404)
        await log_access(conn, action="edit_export", staff_id=staff.id, query="editor")
    csrf = csrf_for(session)
    try:
        if kind == "text":
            read = editing.read_docx(found[2])
            return _page(staff, session,
                         _text_form(token, csrf, read.title, read.text, dirty=False))
        sheet = editing.read_xlsx(found[2])
    except editing.NotEditable:
        return _note(staff, session, "not_editable", 422)
    return _page(staff, session, _grid_form(token, csrf, sheet.title, sheet.rows))


async def _posted(request: Request) -> dict[str, str] | Response:
    """The form of a POST: another site, an oversized body or a missing token
    end here, before any connection."""
    if from_another_site(request):
        return refused_from_another_site()
    form = await read_form(request, max_bytes=_BODY)
    if not csrf_matches(request.cookies.get(COOKIE) or "", form.get("csrf", "")):
        return page("Pedido recusado", "<h1 class=bad>Pedido recusado</h1>"
                    f"<p>{NOTES['refused']}</p>", status=403)
    return form


@router.post("/{token}/ver", response_class=HTMLResponse, include_in_schema=False)
async def preview(request: Request, token: str):
    """The text as sent, shown as it will download. Never stored."""
    _switched_on()
    if len(token) > _TOKEN_MAX:
        raise HTTPException(status_code=404)
    form = await _posted(request)
    if isinstance(form, Response):
        return form
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _enter(conn, request)
        if isinstance(entered, Response):
            return entered
        staff, session = entered
        name = await panel.file_name(conn, token, staff.id)
        if name is None or _kind(name) != "text":
            raise HTTPException(status_code=404)
    title, text = form.get("titulo", ""), form.get("texto", "")
    if not editing.validate_text(title, text):
        return _note(staff, session, "too_big", 413)
    return _page(staff, session,
                 _text_form(token, csrf_for(session), title, text, dirty=True),
                 _preview(title, text))


def _count(raw: str, limit: int) -> int | None:
    """A row or column count as the page writes it, or None; over the limit is
    the caller's to refuse."""
    if not (raw.isascii() and raw.isdigit() and len(raw) <= 4):
        return None
    return int(raw)


@router.post("/{token}/baixar", include_in_schema=False)
async def download(request: Request, token: str):
    """What was typed, built in the chosen format and sent as an attachment.
    Never stored: the file is made for this answer and gone with it."""
    _switched_on()
    if len(token) > _TOKEN_MAX:
        raise HTTPException(status_code=404)
    form = await _posted(request)
    if isinstance(form, Response):
        return form
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _enter(conn, request)
        if isinstance(entered, Response):
            return entered
        staff, session = entered
        name = await panel.file_name(conn, token, staff.id)
        kind = _kind(name) if name is not None else None
        fmt = form.get("formato", "")
        if name is None or kind is None or fmt not in (
                ("docx", "pdf") if kind == "text" else ("xlsx",)):
            raise HTTPException(status_code=404)
        # A file whose seal does not open is as gone as an expired one.
        if await panel.read_file(conn, token, staff_id=staff.id) is None:
            raise HTTPException(status_code=404)
        title = form.get("titulo", "")
        if kind == "text":
            text = form.get("texto", "")
            if not editing.validate_text(title, text):
                return _note(staff, session, "too_big", 413)
            data = (await exporting.build_async(fmt, title, text))[0]
        else:
            height = _count(form.get("linhas", ""), editing.MAX_ROWS)
            width = _count(form.get("colunas", ""), editing.MAX_COLS)
            if height is None or width is None:
                return _note(staff, session, "fields", 422)
            if height > editing.MAX_ROWS or width > editing.MAX_COLS:
                return _note(staff, session, "too_big", 413)
            rows = [[form.get(f"c{r}_{c}", "") for c in range(width)] for r in range(height)]
            if not editing.validate_grid(title, rows):
                return _note(staff, session, "too_big", 413)
            data = await asyncio.to_thread(exporting.build_xlsx_rows, title, rows)
        await log_access(conn, action="download_export", staff_id=staff.id, query="editor")
    headers = dict(HEADERS)
    headers["Content-Disposition"] = attachment_disposition(f"{PurePath(name).stem}.{fmt}")
    # Never a page: no script runs if a browser renders it anyway.
    headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return Response(content=data, media_type="application/octet-stream", headers=headers)


__all__ = ["HOME", "NOTES", "POLICY", "SCRIPT", "router"]
