"""The assistant in the panel: the same conversation as on Telegram, here.

A message typed here goes through `panel_intake` (refused, or written and
queued) and is answered by the same worker, through the panel's channel, as
rows in the conversation. This page shows those rows: the person's whole
conversation, from both windows, read by their staff id and nobody else's.

Behind `WEB_ASSISTANT` (off, pilot, on). Off, or not in the pilot, these
addresses answer like addresses that do not exist, before any connection.

Every part works without a script. Sending posts and comes back here; while
the answer is on its way the page refreshes itself every few seconds, and
stops once it arrives or the request waits for the morning. With
`static/assistant.js` (this page, and only this page here, allows a script
from this host), sending stays on the page and the script asks
`/conversa` what changed every second and a half until the answer is in.

What the bot wrote is escaped before anything is linked in it: a document
cited as "[12]" becomes a link to the explorer, which checks the scope
again. A file is linked only to the two download addresses this system
serves, never to an address carried in a row.
"""
from __future__ import annotations

import asyncio
import html
import math
import re
from datetime import datetime, timedelta
from pathlib import Path, PurePath
from urllib.parse import unquote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.requests import ClientDisconnect

from core.config import settings
from core.models import StaffMember
from core.repositories import conversation, panel, panel_uploads, publications
from core.repositories.connection import get_pool
from core.repositories.documents import log_access
from core.schedule import local_now
from core.integrations import cache
from core.security import RateLimiter
from core.services import authorization, budget, ingestion, media, panel_intake, pje
from core.services.audio import is_silence_artifact
from core.services.legal_assistant.graph import budget_reached
from core.web import actions
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
    NOTICES_STYLE,
    app_lock_meta,
    assistant_open_to,
    csrf_for,
    csrf_matches,
    current_staff,
    explorer_open_to,
    nav,
    offers_notices,
    signed_out,
)

router = APIRouter(prefix="/web/assistente", tags=["web"],
                   dependencies=[Depends(remember_theme)])

_HOME = "/web/assistente/"
# Where the page opens: at the composer, below the newest turn. Every way
# back to the page carries it, so a long conversation opens at its end.
_ANCHOR = "escrever"
PER_PAGE = 50
# How often the page looks again while the answer is on its way.
REFRESH_SECONDS = 4
# A failed request is said for this long after it failed; later, the page is
# simply the conversation again.
_FAILED_SHOWN = timedelta(minutes=10)
# A CSRF token and a button's turn and token; a withdrawal's request id.
_SMALL_BODY = 1024

# What the page says for each reason the intake gives, in its own words: what
# the person typed is never carried back into the page.
NOTES = {
    "busy": "Aguarde a resposta anterior.",
    "telegram": "Isso é feito pelo Telegram: envie o comando ao bot.",
    "empty": "Escreva a mensagem antes de enviar.",
    "too_long": "A mensagem é muito longa. Pode resumir a pergunta?",
    "no_button": "Esse botão não está mais disponível.",
    "no_chat": ("O assistente do painel usa o seu acesso pelo bot do Telegram. "
                "Peça o acesso pelo bot e volte aqui."),
    "not_sent": "Não consegui enviar a mensagem. Tente de novo em instantes.",
    "withdrawn": "Mensagem retirada. Ela não será respondida.",
    "not_withdrawn": "Essa mensagem já começou a ser respondida ou não existe mais.",
    "refused": "Recarregue a página e tente de novo.",
    "no_upload": "O anexo não está mais disponível. Anexe de novo e envie.",
    "action_fields": "Confira os campos e tente de novo.",
    "microsoft_page": "Conecte a Microsoft na aba Integrações.",
    "pje_words": ("No painel, envie só /pje, /pje cadastrar ou /pje remover. A senha do "
                  "PJe é digitada na página de cadastro, nunca no chat."),
}

# This page, and only this page here, runs a script from this host and asks
# this host for what changed. Built from the shared policy, never changing it.
POLICY = (HEADERS["Content-Security-Policy"]
          + "; script-src 'self'; connect-src 'self'; worker-src 'self'")
SCRIPT = f"{_HOME}assistant.js"
_SCRIPT_BYTES = (Path(__file__).parent / "static" / "assistant.js").read_bytes()

# The only addresses a file in a reply may lead to.
_FILE_PREFIXES = ("/web/assistente/arquivo/", "/web/arquivos/")
# A file the editor can open: one the assistant generated (never an archived
# original), as a Word document or a spreadsheet.
_GENERATED = re.compile(r"/web/assistente/arquivo/([A-Za-z0-9_-]{1,100})")
_EDITABLE = (".docx", ".xlsx")
_SOURCE = re.compile(r"\[(\d{1,9})\]")
# The panel's PJe page as the bot names it (`/pje` here), with or without the
# host, read in the escaped text. It is linked to the fixed address and never
# to what the text says.
_PJE_PAGE = re.compile(r"(?<!\S)(?:https?://[^\s/]+)?" + re.escape(pje.PANEL_PAGE) + r"(?=\s|$)")

STYLE = """
.talk{list-style:none;margin:2rem 0 0;padding:0;display:flex;flex-direction:column;gap:1rem}
.turn{max-width:46rem;padding:1rem 1.2rem;border:1px solid var(--line);border-radius:var(--radius-lg);
 background:var(--panel)}
.turn.person{align-self:flex-end;border-color:var(--line-strong);background:transparent}
.turn .meta{display:flex;gap:.8rem;margin:0 0 .5rem;font-size:.66rem;letter-spacing:.14em;
 text-transform:uppercase;color:var(--muted);font-variant-numeric:tabular-nums}
.turn .text{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;color:var(--text);
 line-height:1.55}
.turn .file{display:inline-block;margin-top:.7rem;overflow-wrap:anywhere}
.turn .buttons{display:flex;flex-wrap:wrap;gap:.6rem;margin-top:.8rem}
.turn .buttons form{margin:0}
.turn .buttons button{margin:0;width:auto;min-height:2.4rem;padding:.4rem .9rem}
.state{margin-top:1.4rem}
.state{display:flex;flex-wrap:wrap;gap:.8rem;align-items:center;justify-content:space-between}
.state form{margin:0}
.state button{margin:0;width:auto;min-height:2.4rem;padding:.4rem .9rem}
.older{display:inline-block;margin-top:1rem}
.composer{margin-top:2rem;scroll-margin-bottom:2rem;scroll-margin-top:5rem}
.composer .note{margin:0 0 1rem}
.composer .box{display:flex;flex-direction:column;gap:.35rem;padding:.9rem .9rem .7rem 1.15rem;
 border:1px solid var(--line-strong);border-radius:16px;background:var(--panel);
 transition:border-color .15s ease}
.composer .box:focus-within{border-color:var(--accent)}
.composer textarea{display:block;width:100%;min-height:0;max-height:12.4rem;padding:.2rem 0;
 border:0;border-radius:0;background:transparent;box-shadow:none;outline:none;resize:none;
 overflow-y:auto;line-height:1.55}
.composer .hint{margin:0;font-size:.75rem;color:var(--muted)}
.composer .row{display:flex;align-items:center;justify-content:space-between;gap:.6rem;
 min-height:2.25rem}
.composer-tools{display:flex;align-items:center;gap:.4rem;min-width:0}
.composer .send{flex:none;width:2.25rem;height:2.25rem;min-height:0;margin:0;padding:0;
 border-radius:50%}
.composer .send svg{width:1.15rem;height:1.15rem}
form.composer [hidden]{display:none}
.composer .tool{width:2.75rem;min-height:2.75rem;margin:0;padding:.6rem;background:transparent;
 border-color:transparent;color:var(--text);border-radius:50%}
.composer .tool:hover{background:var(--row-hover)}
.attach-choices{display:flex;flex-wrap:wrap;gap:.5rem}
.composer .attach-choices button{width:auto;margin:0;padding:.5rem .8rem;letter-spacing:normal;
 text-transform:none;font-weight:400}
.attachment-chip{display:flex;align-items:center;gap:.6rem;width:fit-content;max-width:100%;
 padding:.35rem .7rem;border:1px solid var(--line);border-radius:var(--radius-lg);font-size:.85rem;
 overflow-wrap:anywhere}
.composer .attachment-chip button{margin:0;padding:.25rem;min-height:2rem;width:2rem}
.composer .archive-choice{display:flex;align-items:center;gap:.5rem;font-size:.8rem;
 text-transform:none;letter-spacing:normal;margin:.3rem 0}
.composer .archive-choice input{width:auto;margin:0}
.actions{display:block;margin-top:2rem}
.actions>summary{display:inline-flex;align-items:center;gap:.5rem;min-height:2.75rem;
 padding:.45rem 1rem;border:1px solid var(--line-strong);border-radius:999px;cursor:pointer;
 list-style:none;color:var(--text);background:var(--panel);font-size:.9rem;width:fit-content}
.actions>summary::-webkit-details-marker{display:none}
.actions>summary:hover{background:var(--row-hover)}
.actions[open]>summary{border-color:var(--accent)}
.action-list{margin-top:.8rem;padding:1rem 1.15rem;border:1px solid var(--line);
 border-radius:var(--radius-lg);background:var(--panel);display:grid;gap:1.2rem}
.action-list [data-actions-close]{display:none}
.action-list section{display:grid;gap:.35rem}
.action-list h2{margin:0 0 .25rem;font-size:.7rem;letter-spacing:.12em;text-transform:uppercase;
 color:var(--muted);font-weight:500}
.action-list .action>summary{cursor:pointer;padding:.55rem .2rem;border-radius:var(--radius)}
.action-list .action>summary:hover{background:var(--row-hover)}
.action-list div.action form button,.action-list .follows button{width:auto;margin:0;
 min-height:2.4rem;padding:.4rem .9rem}
.action-list div.action button{width:100%;text-align:left;text-transform:none;letter-spacing:normal;
 font-weight:400;font-size:1rem;color:var(--text);border-color:transparent;
 padding:.55rem .2rem .55rem 1.2rem}
.action-list details form{display:grid;gap:.7rem;padding:.6rem .2rem .9rem}
.action-list details form>button{justify-self:start;width:auto;margin:0}
.action-list label{display:grid;gap:.3rem;font-size:.85rem}
.action-list fieldset{display:flex;flex-wrap:wrap;gap:1rem;margin:0;padding:0;border:0}
.action-list legend{font-size:.85rem;margin-bottom:.3rem}
.action-list fieldset label,.action-list label:has(input[type=checkbox]){display:flex;
 align-items:center;gap:.45rem;text-transform:none;letter-spacing:normal;font-weight:400}
.action-list input[type=radio],.action-list input[type=checkbox]{width:auto;margin:0}
.action-list input,.action-list select{margin:0}
.action-list .follows{list-style:none;margin:0;padding:.4rem .2rem .6rem;display:grid;gap:.5rem}
.action-list .follows li{display:flex;align-items:center;justify-content:space-between;gap:.8rem;
 overflow-wrap:anywhere}
.action-list .follows button{flex:none;white-space:nowrap}
.action-list .follows form{margin:0;padding:0}
.action-list .hint{margin:.3rem .2rem .6rem;font-size:.85rem;color:var(--muted)}
.action-list button:disabled{opacity:.45;cursor:default}
.composer .recording{display:flex;align-items:center;gap:.5rem;min-height:2.75rem}
.composer .recording[hidden]{display:none}
.composer .box.is-recording textarea,.composer .box.is-recording .row,
.composer .box.is-recording .hint{display:none}
.composer .wave{flex:1;min-width:0;height:2.25rem;color:var(--text)}
.composer .record-clock{font-variant-numeric:tabular-nums;color:var(--mid);font-size:.9rem;
 min-width:2.6rem;text-align:right}
.composer .record-state{flex:1;color:var(--mid);font-size:.95rem}
.composer .recording .done{background:var(--accent);color:var(--on-accent);border-color:var(--accent)}
.composer .recording .done:hover{background:var(--accent-hover)}
.composer .recording button:disabled{opacity:.4}
.actions.in-composer>summary{display:none}
.actions.in-composer:not([open]){margin:0}
.composer .tool[aria-expanded=true]{background:var(--row-hover)}
.composer .send:disabled{opacity:.35;cursor:default}
.composer .send:disabled:hover{background:var(--accent);border-color:var(--accent)}
@media(max-width:800px){
 .app-page .app-content>h1{position:absolute;width:1px;height:1px;margin:-1px;padding:0;
  overflow:hidden;clip-path:inset(50%);white-space:nowrap}
 .app-page .talk{gap:.9rem;margin-top:.25rem}
 .app-page .turn{max-width:100%;padding:.9rem 1rem}
 .app-page .turn.person{max-width:94%}
 .app-page .turn .meta{font-size:.62rem;letter-spacing:.07em;gap:.5rem}
 .app-page .composer{margin-top:1.25rem}
 .app-page .composer .box{padding:.8rem .8rem .65rem 1rem}
 .app-page .actions{margin-top:1.25rem}
 .actions.sheet[open] .action-list{position:fixed;left:0;right:0;bottom:0;z-index:30;margin:0;
  max-height:75vh;overflow-y:auto;overscroll-behavior:contain;border-radius:16px 16px 0 0;
  border-bottom:0;padding:1rem 1rem max(1rem,env(safe-area-inset-bottom));
  box-shadow:0 -12px 40px rgba(0,0,0,.25)}
 .actions.sheet [data-actions-close]{display:block;justify-self:end;width:auto;min-height:2.25rem;
  margin:0;padding:.3rem .8rem}
}
""" + NOTICES_STYLE


def _switched_on() -> None:
    """Off, the assistant's addresses do not exist: answered before any
    connection is taken."""
    if not settings.web_enabled or settings.web_assistant == "off":
        raise HTTPException(status_code=404)


async def _enter(conn, request: Request, *,
                 poll: bool = False) -> tuple[StaffMember, str] | Response:
    """The person on this page, or the answer for nobody. The script's poll
    is a look, not use: it does not move the lock clock, and with no session
    it gets the marker `ended` instead of the signed-out page."""
    signed_in = await current_staff(conn, request, activity=not poll)
    if signed_in is None:
        if poll:
            return PlainTextResponse("ended", status_code=401, headers=HEADERS)
        return signed_out(request)
    staff, session = signed_in
    if not assistant_open_to(staff):
        raise HTTPException(status_code=404)
    return staff, session


async def _posted(request: Request, max_bytes: int) -> dict[str, str] | Response:
    """The form of a POST from this page, or the refusal: another site, an
    oversized body or a missing token all end here, before any connection."""
    if from_another_site(request):
        return refused_from_another_site()
    form = await read_form(request, max_bytes=max_bytes)
    if not csrf_matches(request.cookies.get(COOKIE) or "", form.get("csrf", "")):
        return page("Pedido recusado",
                    "<h1 class=bad>Pedido recusado</h1>"
                    f"<p>{NOTES['refused']}</p>", status=403)
    return form


def _number(raw: str) -> bool:
    """A row id as the page writes it: ASCII digits, short enough for a
    bigint. `isdigit` alone also takes "²", which int() then refuses."""
    return raw.isascii() and raw.isdigit() and len(raw) <= 18


# Notes that report something done, not refused.
_DONE_NOTES = frozenset({"", "withdrawn"})


def _back(form: dict[str, str], note: str = "") -> Response:
    """Back to the page, with its note. The script asks for the note alone
    (`via=script`): the text, 200 when done and 409 when refused."""
    if form.get("via") == "script":
        return PlainTextResponse(NOTES.get(note, ""),
                                 status_code=200 if note in _DONE_NOTES else 409,
                                 headers=HEADERS)
    where = f"{_HOME}?aviso={note}" if note else _HOME
    return RedirectResponse(f"{where}#{_ANCHOR}", status_code=303)


# ── The conversation ────────────────────────────────────────────────────────


def _bot_text(text: str, *, link_sources: bool) -> str:
    """Escaped first; then the panel's PJe page made a link, and each "[N]"
    a link to that document's page, which decides again whether this person
    may see it."""
    escaped = _PJE_PAGE.sub(lambda m: f"<a href='{pje.PANEL_PAGE}'>{m.group(0)}</a>",
                            html.escape(text))
    if not link_sources:
        return escaped
    return _SOURCE.sub(
        lambda m: f"<a href='/web/explorar/documento/{int(m.group(1))}'>[{m.group(1)}]</a>",
        escaped)


def _file(extras: dict | None) -> str:
    found = (extras or {}).get("file")
    if not isinstance(found, dict):
        return ""
    href, name = str(found.get("href") or ""), str(found.get("name") or "arquivo")
    if not href.startswith(_FILE_PREFIXES) or "'" in href or '"' in href:
        return ""
    # How long it lasts is in the reply's own text, said once.
    link = f"<a class=file href='{html.escape(href)}'>Baixar {html.escape(name)}</a>"
    generated = _GENERATED.fullmatch(href)
    if generated and name.lower().endswith(_EDITABLE):
        link += f" <a class=file href='/web/editor/{generated.group(1)}'>Editar</a>"
    return link


def _buttons(turn: conversation.Turn, csrf: str) -> str:
    rows = (turn.extras or {}).get("buttons")
    if turn.channel != "panel" or turn.speaker != "bot" or not isinstance(rows, list):
        return ""
    forms = []
    for row in rows:
        for button in row if isinstance(row, list) else []:
            if not isinstance(button, dict):
                continue
            forms.append(
                f"<form method=post action='{_HOME}botao'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                f"<input type=hidden name=turno value='{turn.id}'>"
                f"<input type=hidden name=token value='{html.escape(str(button.get('token', '')))}'>"
                f"<button type=submit class=quiet>{html.escape(str(button.get('label', '')))}"
                f"</button></form>")
    return f"<div class=buttons>{''.join(forms)}</div>" if forms else ""


def _attachment_chip(extras: dict | None) -> str:
    item = (extras or {}).get("attachment")
    if not isinstance(item, dict):
        return ""
    kind = str(item.get("kind"))
    label = {"photo": "Foto", "voice": "Nota de voz", "audio": "Áudio",
             "document": "Documento"}.get(kind)
    if label is None:
        return ""
    name = item.get("name")
    if kind == "document" and isinstance(name, str) and name:
        label += f" · {name}"
    seconds = item.get("seconds")
    if isinstance(seconds, (int, float)) and math.isfinite(seconds) and 0 <= seconds <= 3601:
        total = int(seconds)
        label += f" {total // 60}:{total % 60:02d}"
    return f"<p class=attachment-chip>{html.escape(label)}</p>"


# The longest label an action writes; anything else on a turn is not one.
_LABEL_MAX = 120


def _turn(turn: conversation.Turn, csrf: str, *, link_sources: bool) -> str:
    when = local_now(turn.at).strftime("%d/%m %H:%M")
    who = "Você" if turn.speaker == "person" else "Assistente"
    via = "<span>pelo Telegram</span>" if turn.channel == "telegram" else ""
    label = (turn.extras or {}).get("action") if turn.speaker == "person" else None
    if not (isinstance(label, str) and 0 < len(label) <= _LABEL_MAX):
        label = None
    text = (_bot_text(turn.text, link_sources=link_sources) if turn.speaker == "bot"
            else html.escape(label if label is not None else turn.text))
    chip = _attachment_chip(turn.extras)
    # An attachment sent alone has no words of its own: the chip is the turn.
    words = f"<p class=text>{text}</p>" if text or not chip else ""
    return (f"<li class='turn {'person' if turn.speaker == 'person' else 'bot'}' "
            f"id=t{turn.id}><p class=meta><span>{who}</span><span>{when}</span>{via}</p>"
            f"{chip}{words}{_file(turn.extras)}{_buttons(turn, csrf)}</li>")


def _state(request: panel.RequestRow | None, csrf: str) -> tuple[str, bool, bool]:
    """What the page says about the newest request: (html, busy, refresh)."""
    if request is None:
        return "", False, False
    if request.status in ("sending", "queued", "running"):
        return ("<div class='note state' role=status>"
                + ("Transcrevendo…" if request.attachment_kind in ("audio", "voice")
                   else "Lendo a foto…" if request.attachment_kind == "photo"
                   else "Processando o documento…" if request.attachment_kind == "document"
                   else "Respondendo…")
                + "</div>"), True, True
    if request.status == "deferred":
        # A div, not a p: a paragraph cannot hold the form, and the browser
        # would close it early and leave the button outside the notice.
        return (f"<div class='note state' role=status>Fora do horário: a mensagem será "
                f"respondida a partir das 08:00."
                f"<form method=post action='{_HOME}retirar'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                f"<input type=hidden name=pedido value='{request.id}'>"
                f"<button type=submit class=quiet>Retirar</button></form></div>", True, False)
    if request.status == "failed" and local_now() - request.updated_at < _FAILED_SHOWN:
        return ("<div class='note state bad' role=status>Não consegui responder. "
                "Tente de novo.</div>"), False, False
    return "", False, False


def _composer(csrf: str, busy: bool, note: str, *, can_archive: bool = False,
              actions_toggle: bool = False) -> str:
    """The form to write in, with the page's note above it: the note is said
    where the person is looking, at the bottom, by what they just sent."""
    disabled = " disabled" if busy else ""
    hint = "<p class=hint>Aguarde a resposta anterior.</p>" if busy else ""
    # The actions first, then the clip and the microphone. The pill above the
    # box is only for a page without the script.
    tools = (
        ("<button type=button class=tool data-actions-toggle aria-label='Ações' "
         f"aria-expanded=false aria-controls=acoes hidden>{actions.ICON}</button>"
         if actions_toggle else "")
        + "<button type=button class=tool data-attach-toggle aria-label='Anexar foto ou áudio' "
        "aria-expanded=false aria-controls=attach-choices hidden>"
        "<svg class=nav-icon viewBox='0 0 24 24' fill=none stroke=currentColor stroke-width=1.8 "
        "aria-hidden=true><path d='m8 12 7-7a3 3 0 0 1 4 4L9 19a5 5 0 0 1-7-7L12 2'/>"
        "</svg></button>"
        "<button type=button class=tool data-microphone aria-label='Ditar mensagem' hidden>"
        "<svg class=nav-icon viewBox='0 0 24 24' fill=none stroke=currentColor stroke-width=1.8 "
        "aria-hidden=true><rect x='9' y='2' width='6' height='12' rx='3'/>"
        "<path d='M5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8'/></svg></button>"
    )
    # Dictation, as the chats people know: the box becomes a bar with the
    # voice's wave and its time, X to drop it and the tick to read it into
    # the box. Only the script shows it.
    recording = (
        "<div class=recording data-recording hidden>"
        "<button type=button class=tool data-record-cancel aria-label='Descartar gravação'>"
        "<svg class=nav-icon viewBox='0 0 24 24' fill=none stroke=currentColor stroke-width=2 "
        "stroke-linecap=round aria-hidden=true><path d='m6 6 12 12M6 18 18 6'/></svg></button>"
        "<canvas class=wave data-wave aria-hidden=true></canvas>"
        "<span class=record-clock data-record-clock role=timer>0:00</span>"
        "<span class=record-state data-record-state role=status hidden>Transcrevendo…</span>"
        "<button type=button class='tool done' data-record-done aria-label='Concluir e transcrever'>"
        "<svg class=nav-icon viewBox='0 0 24 24' fill=none stroke=currentColor stroke-width=2.2 "
        "stroke-linecap=round stroke-linejoin=round aria-hidden=true><path d='m5 12 5 5 9-10'/>"
        "</svg></button></div>"
    )
    archive = (
        "<label class=archive-choice data-archive-choice hidden>"
        "<input type=checkbox data-archive>Guardar também no arquivo</label>"
        if can_archive else ""
    )
    # A document goes into the archive, as on Telegram: offered only to whom
    # may put one there. Its limits and refusals are Telegram's own words,
    # carried here so the script never keeps a copy of them.
    document = (
        "<input type=file data-document-input hidden "
        f"accept='{','.join(sorted(ingestion.DOCUMENT_EXTENSIONS))}' "
        f"data-limit={settings.max_doc_mb * 1024 * 1024} "
        f"data-too-large='{html.escape(ingestion.document_too_large())}' "
        f"data-format='{html.escape(ingestion.DOCUMENT_FORMAT_REFUSED)}'>"
        if can_archive else ""
    )
    attachments = (
        "<div class=attach-choices id=attach-choices data-attach-choices hidden>"
        "<button class=quiet type=button data-choose-photo>Foto</button>"
        "<button class=quiet type=button data-choose-audio>Arquivo de áudio</button>"
        + ("<button class=quiet type=button data-choose-document>Documento</button>"
           if can_archive else "")
        + "</div>"
        "<input type=file data-photo-input accept='image/jpeg,image/png' hidden>"
        "<input type=file data-audio-input accept='audio/*' hidden>" + document +
        "<div class=attachment-chip data-attachment-chip hidden>"
        "<span data-attachment-label></span>"
        "<button type=button class=quiet data-remove-attachment aria-label='Remover anexo'>×</button>"
        "</div>" + archive
    )
    # The button is a real submit button and is enabled here: without the
    # script, the form posts. The script turns it off while the box is empty.
    return (f"<form class=composer id={_ANCHOR} method=post action='{_HOME}mensagem'>"
            f"{note}<input type=hidden name=csrf value='{csrf}'>"
            "<div class=box>"
            f"<textarea id=texto name=texto rows=1 aria-label='Sua mensagem' "
            f"placeholder='Escreva sua mensagem…' maxlength={settings.max_question_chars} "
            f"required{disabled}></textarea>{hint}"
            f"{attachments}{recording}<div class=row><div class=composer-tools>{tools}</div>"
            f"<button type=submit class=send aria-label=Enviar{disabled}>"
            "<svg viewBox='0 0 24 24' fill=none stroke=currentColor stroke-width=2 "
            "stroke-linecap=round stroke-linejoin=round aria-hidden=true focusable=false>"
            "<path d='M12 19V5m-6 6 6-6 6 6'/></svg></button></div></div></form>")


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def conversation_page(request: Request, antes: str = "", aviso: str = "") -> Response:
    _switched_on()
    before = int(antes) if _number(antes) else None
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _enter(conn, request)
        if isinstance(entered, Response):
            return entered
        staff, session = entered
        # The request first, then the turns: a reply landing between the two
        # reads is then either among the turns or still awaited, and the
        # page keeps refreshing until it shows.
        newest = await panel.latest(conn, staff.id)
        turns, more = await conversation.page_of(conn, staff.id, before_id=before,
                                                 limit=PER_PAGE)

        since = await conversation.cursor(conn)
        notices = before is None and await offers_notices(conn, staff, session)
        oabs, processes = await publications.list_own_watches(conn, staff.id)

    csrf = csrf_for(session)
    state, busy, refresh = _state(newest, csrf)
    link_sources = explorer_open_to(staff)
    older = (f"<a class=older href='{_HOME}?antes={turns[0].id}'>Mensagens anteriores</a>"
             if more and turns else "")
    note = (f"<p class='note' role=alert id=aviso>{NOTES[aviso]}</p>" if aviso in NOTES
            else "<p class=note role=alert id=aviso hidden></p>")
    empty = "" if turns else "<p id=vazia>Nenhuma conversa ainda. Pergunte o que precisar.</p>"
    talk = (f"<ol class=talk>{''.join(_turn(t, csrf, link_sources=link_sources) for t in turns)}"
            f"</ol>")
    if before is not None:
        # An older page: the conversation as it was, nothing to send from.
        return page("Assistente", f"<h1>Assistente</h1>{older}{talk}",
                    wide=True, style=STYLE, navigation=nav(staff, session))
    status = newest.status if newest else "nenhum"
    can_archive = authorization.decide(staff, "document.ingest").allowed
    menu = actions.menu(staff, csrf, busy=busy, oabs=oabs, processes=processes)
    body = (f"<h1>Assistente</h1>{older}"
            f"<section id=conversa data-cursor='{since.isoformat()}'>{empty}{talk}</section>"
            f"<div id=estado data-estado={status}>{state}</div>"
            f"{menu}"
            f"{_composer(csrf, busy, note, can_archive=can_archive, actions_toggle=bool(menu))}"
            + ("<script type=module src='/web/notices.js'></script>" if notices else "")
            + f"<script type=module src='{SCRIPT}'></script>")
    # Without the script, the page looks again by itself while the answer is
    # on its way. In <noscript>: with the script, it is the one that looks.
    head = (f"<noscript><meta http-equiv=refresh content={REFRESH_SECONDS}></noscript>"
            if refresh else "")
    return page("Assistente", body, headers={"Content-Security-Policy": POLICY},
                wide=True, style=STYLE, head=head + app_lock_meta(staff),
                navigation=nav(staff, session, notices=notices))


@router.get("/conversa", include_in_schema=False)
async def changes(request: Request, desde: str = "") -> Response:
    """What the page may not have yet, for the script: the turns, the state
    and the next cursor, as markup it parses and places by turn id."""
    _switched_on()
    try:
        since: datetime | None = datetime.fromisoformat(desde) if desde else None
    except ValueError:
        since = None
    if since is not None and since.tzinfo is None:
        since = None
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _enter(conn, request, poll=True)
        if isinstance(entered, Response):
            return entered
        staff, session = entered
        newest = await panel.latest(conn, staff.id)
        turns, next_cursor, complete = await conversation.changed_since(
            conn, staff.id, since, request_id=newest.id if newest else None)

    csrf = csrf_for(session)
    state, _busy, _refresh = _state(newest, csrf)
    link_sources = explorer_open_to(staff)
    status = newest.status if newest else "nenhum"
    if not complete:
        return HTMLResponse("<div id=novas data-recarregar=1></div>", headers=HEADERS)
    fragment = (f"<div id=novas data-cursor='{next_cursor.isoformat()}'>"
                f"<ol>{''.join(_turn(t, csrf, link_sources=link_sources) for t in turns)}</ol>"
                f"<div id=estado data-estado={status}>{state}</div></div>")
    return HTMLResponse(fragment, headers=HEADERS)


@router.get("/assistant.js", include_in_schema=False)
async def script() -> Response:
    """The page's script. It holds nothing about anyone, so it needs no
    session; off, it does not exist, like the page."""
    _switched_on()
    return Response(_SCRIPT_BYTES, media_type="text/javascript; charset=utf-8",
                    headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})


# ── Sending, pressing, withdrawing ──────────────────────────────────────────


@router.post("/mensagem", include_in_schema=False)
async def send(request: Request):
    _switched_on()
    # The longest message the bot takes, each character up to four UTF-8
    # bytes, each byte "%XX" in the form, and the token.
    form = await _posted(request, _SMALL_BODY + settings.max_question_chars * 4 * 3)
    if isinstance(form, Response):
        return form
    # The attachment sent before this message, if any: the intake decides
    # whether it is still this person's to send.
    upload_id: UUID | None = None
    if form.get("anexo"):
        try:
            upload_id = UUID(form["anexo"])
        except ValueError:
            return _back(form, "no_upload")
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _enter(conn, request)
        if isinstance(entered, Response):
            return entered
        staff, _session = entered
        intake = await panel_intake.submit_message(conn, staff, form.get("texto", ""),
                                                   upload_id=upload_id)
    return _back(form, "" if intake.outcome in ("queued", "deferred") else intake.reason)


@router.post("/botao", include_in_schema=False)
async def press(request: Request):
    _switched_on()
    form = await _posted(request, _SMALL_BODY)
    if isinstance(form, Response):
        return form
    raw_turn = form.get("turno", "")
    if not _number(raw_turn):
        return _back(form, "no_button")
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _enter(conn, request)
        if isinstance(entered, Response):
            return entered
        staff, _session = entered
        intake = await panel_intake.submit_press(conn, staff, token=form.get("token", ""),
                                                 turn_id=int(raw_turn))
    return _back(form, "" if intake.outcome in ("queued", "deferred") else intake.reason)


@router.post("/acao", include_in_schema=False)
async def act(request: Request):
    """One of the list's actions: its command built from the checked fields,
    then sent as a typed message is. The permission is decided here on the
    staff row read for this request, and again by the worker."""
    _switched_on()
    form = await _posted(request, _SMALL_BODY)
    if isinstance(form, Response):
        return form
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _enter(conn, request)
        if isinstance(entered, Response):
            return entered
        staff, _session = entered
        action = actions.ACTIONS.get(form.get("acao", ""))
        if action is None or not authorization.decide(staff, action.permission).allowed:
            return _back(form, "refused")
        built = actions.build(action.key, form, local_now().date())
        if built is None:
            return _back(form, "action_fields")
        intake = await panel_intake.submit_message(conn, staff, built.command,
                                                   label=built.label)
    return _back(form, "" if intake.outcome in ("queued", "deferred") else intake.reason)


@router.get("/acompanhamentos", include_in_schema=False)
async def follows(request: Request) -> Response:
    """This person's follows, for the script to refresh the list with: a
    follow the worker just added or stopped shows without a reload."""
    _switched_on()
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _enter(conn, request, poll=True)
        if isinstance(entered, Response):
            return entered
        staff, session = entered
        oabs, processes = await publications.list_own_watches(conn, staff.id)
        newest = await panel.latest(conn, staff.id)
    csrf = csrf_for(session)
    _state_html, busy, _refresh = _state(newest, csrf)
    return HTMLResponse(actions.follows(csrf, oabs, processes, busy=busy), headers=HEADERS)


@router.post("/retirar", include_in_schema=False)
async def withdraw(request: Request):
    _switched_on()
    form = await _posted(request, _SMALL_BODY)
    if isinstance(form, Response):
        return form
    try:
        request_id = UUID(form.get("pedido", ""))
    except ValueError:
        return _back(form, "not_withdrawn")
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _enter(conn, request)
        if isinstance(entered, Response):
            return entered
        staff, _session = entered
        done = await panel_intake.withdraw(conn, staff, request_id)
    return _back(form, "withdrawn" if done else "not_withdrawn")


# ── An attachment, before the message that asks about it ───────────────────


# What the script is told when an attachment is refused, as text with a
# status: the reason, never anything the request carried.
UPLOAD_NOTES = {
    "photo_too_large": ("A foto passa de 10 MB.", 413),
    "voice_too_large": ("A nota de voz passa de 5 MB.", 413),
    "audio_too_large": ("O áudio passa de 100 MB.", 413),
    "type": ("Não reconheci esse arquivo como foto ou áudio.", 415),
    "heic": ("Essa foto está em HEIC. Exporte como JPEG e envie de novo.", 415),
    "archive": ("Arquivar pelo chat fica com advogados e administradores.", 403),
    "busy": (NOTES["busy"], 409),
    "full": ("Outros anexos estão chegando agora. Tente de novo em instantes.", 429),
    "slow": ("O envio do anexo parou no meio. Tente de novo.", 408),
    "no_room": ("Você tem anexos ainda sem mensagem. Envie a mensagem ou aguarde "
                "alguns minutos.", 409),
    "refused": (NOTES["refused"], 403),
    "malformed": (NOTES["refused"], 400),
    # A document, in Telegram's own words (`_queue_document`).
    "document_type": (ingestion.DOCUMENT_FORMAT_REFUSED, 415),
    "document_name": (ingestion.DOCUMENT_UNIDENTIFIED, 400),
}

# What a document is stored as, by the extension its bytes were checked
# against (`ingestion.document_matches`).
_DOCUMENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain", ".md": "text/markdown", ".htm": "text/html", ".html": "text/html",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".tif": "image/tiff", ".tiff": "image/tiff",
}
# Longest name a document may carry, in characters.
_NAME_MAX = 200


def document_name(raw: str) -> str | None:
    """The name a document was sent with, or None when it is not a name.

    It arrives percent-encoded as UTF-8 (a header carries no accents) and
    must decode strictly. A name is printable (no control, format or
    direction characters), bounded, without a path separator, and not
    padded with spaces. It is shown escaped and used as the document's title;
    it never becomes a path."""
    if not raw or not raw.isascii() or len(raw) > _NAME_MAX * 12:
        return None
    try:
        name = unquote(raw, errors="strict")
    except UnicodeDecodeError:
        return None
    if (not name or len(name) > _NAME_MAX or name != name.strip() or "/" in name
            or "\\" in name or name in (".", "..") or not name.isprintable()):
        return None
    return name

_PHOTO_TYPES = frozenset({"image/jpeg", "image/png"})
# MP4 and M4A brands an audio file or a recorded voice note arrives with
# (iOS records `audio/mp4`); a HEIF image shares the container.
_MP4_BRANDS = frozenset({
    b"M4A ", b"M4B ", b"F4A ", b"mp41", b"mp42", b"isom", b"iso2", b"iso4", b"iso5",
    b"iso6", b"dash", b"3gp4", b"3gp5", b"3gp6", b"qt  ", b"avc1", b"MSNV",
})
_HEIF_BRANDS = frozenset({
    b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs", b"mif1", b"msf1",
})


def sniff(data: bytes) -> str | None:
    """The media type by the file's first bytes, never by its name or the
    type the request declares; None for anything not on the list."""
    head = data[:16]
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _HEIF_BRANDS:
            return "image/heic"
        return "audio/mp4" if brand in _MP4_BRANDS else None
    if head.startswith(b"ID3"):
        return "audio/mpeg"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "audio/webm"
    if head.startswith(b"ADIF"):
        return "audio/aac"
    if len(head) >= 2 and head[0] == 0xFF:
        if head[1] & 0xF6 == 0xF0:  # ADTS: twelve sync bits, layer 00
            return "audio/aac"
        if head[1] & 0xE0 == 0xE0 and head[1] & 0x06:  # an MPEG audio frame
            return "audio/mpeg"
    return None


class _Slots:
    """Uploads arriving at once in this process. One event loop, and no await
    between the look and the take, so two requests cannot both take the last
    slot."""

    def __init__(self) -> None:
        self.taken = 0

    def take(self) -> bool:
        if self.taken >= settings.panel_uploads_in_flight:
            return False
        self.taken += 1
        return True

    def free(self) -> None:
        self.taken -= 1


_SLOTS = _Slots()


def _upload_refused(reason: str) -> Response:
    if reason == "document_too_large":
        text, status = ingestion.document_too_large(), 413
    else:
        text, status = UPLOAD_NOTES[reason]
    return PlainTextResponse(text, status_code=status, headers=HEADERS)


async def _body(request: Request, kind: panel_uploads.UploadKind) -> bytes | str:
    """The body, or the reason it was not taken. Counted as it arrives and
    stopped at the kind's cap whatever `Content-Length` claims; given up when
    the whole takes too long or a byte keeps it waiting too long."""
    cap = panel_uploads.max_bytes(kind)
    too_large = f"{kind}_too_large"
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > cap:
        return too_large
    loop = asyncio.get_running_loop()
    deadline = loop.time() + (settings.panel_upload_audio_seconds
                              if kind in ("audio", "document") else settings.panel_upload_seconds)
    chunks: list[bytes] = []
    size = 0
    stream = request.stream()
    try:
        while True:
            left = deadline - loop.time()
            if left <= 0:
                return "slow"
            try:
                chunk = await asyncio.wait_for(
                    anext(stream), timeout=min(left, settings.panel_upload_idle_seconds))
            except StopAsyncIteration:
                break
            except TimeoutError:
                return "slow"
            size += len(chunk)
            if size > cap:
                return too_large
            chunks.append(chunk)
    except ClientDisconnect:
        return "slow"
    finally:
        await stream.aclose()
    return b"".join(chunks)


@router.post("/anexo", include_in_schema=False)
async def attach(request: Request) -> Response:
    """One attachment, as the raw body, before the message that asks about
    it. Every check is made before a single byte of the body is read; the
    connection is given back before reading it, since a body may take
    minutes to arrive. Answers the sealed upload's id.

    A document (`X-Anexo-Tipo: document`) is for the archive, as on
    Telegram: it carries its name in `X-Anexo-Nome`, needs
    `document.ingest`, and its bytes must be what its name says."""
    _switched_on()
    if from_another_site(request):
        return refused_from_another_site()
    if not csrf_matches(request.cookies.get(COOKIE) or "", request.headers.get("x-csrf", "")):
        return _upload_refused("refused")
    kind = request.headers.get("x-anexo-tipo", "")
    flag = request.headers.get("x-arquivar", "0")
    if kind not in panel_uploads.KINDS or flag not in ("0", "1"):
        return _upload_refused("malformed")
    archive = flag == "1"
    name: str | None = None
    if kind == "document":
        name = document_name(request.headers.get("x-anexo-nome", ""))
        if name is None:
            return _upload_refused("document_name")
        if PurePath(name).suffix.lower() not in ingestion.DOCUMENT_EXTENSIONS:
            return _upload_refused("document_type")
        # A document always goes to the archive; the media box means nothing here.
        archive = False
    pool = await get_pool()
    async with pool.connection() as conn:
        signed_in = await current_staff(conn, request)
        if signed_in is None:
            return PlainTextResponse("ended", status_code=401, headers=HEADERS)
        staff, _session = signed_in
        if not assistant_open_to(staff):
            raise HTTPException(status_code=404)
        if archive and not authorization.decide(staff, "document.ingest").allowed:
            return _upload_refused("archive")
        if kind == "document":
            decision = authorization.decide(staff, "document.ingest")
            if not decision.allowed:
                # What Telegram says to the same rank (`_may`).
                return PlainTextResponse(decision.reason, status_code=403, headers=HEADERS)
        if await panel.in_flight(conn, staff.id) is not None:
            return _upload_refused("busy")
    if not _SLOTS.take():
        return _upload_refused("full")
    try:
        received = await _body(request, kind)
        if isinstance(received, str):
            return _upload_refused(received)
        if name is not None:
            if not ingestion.document_matches(received, name):
                return _upload_refused("document_type")
            media_type = _DOCUMENT_TYPES[PurePath(name).suffix.lower()]
        else:
            media_type = sniff(received) or ""
            if kind == "photo" and media_type not in _PHOTO_TYPES:
                return _upload_refused("heic" if media_type == "image/heic" else "type")
            if kind != "photo" and not media_type.startswith("audio/"):
                return _upload_refused("type")
        async with pool.connection() as conn:
            upload_id = await panel_uploads.store_upload(
                conn, staff.id, kind=kind, media_type=media_type, data=received,
                archive=archive, filename=name)
    finally:
        _SLOTS.free()
    if upload_id is None:
        return _upload_refused("no_room")
    return JSONResponse({"anexo": str(upload_id)}, headers=HEADERS)


# ── Dictation: a voice note read back into the box ──────────────────────────

# How many voice notes one person may dictate in an hour: each is a paid
# transcription, and the budget's ceiling is the firm's, not the person's.
DICTATIONS_PER_HOUR = 20
# The same count in this process, which holds even when the shared counter
# (Redis) is off or unreachable and answers "allowed".
_DICTATION_LIMITER = RateLimiter(DICTATIONS_PER_HOUR, 3600)
_DICTATION_REFUSED = {
    "silent": ("Não entendi: o áudio chegou sem fala audível. Pode gravar de novo mais "
               "perto do microfone ou escrever a mensagem?", 422),
    "limit": ("Você ditou muitas mensagens na última hora. Escreva esta, ou dite de novo "
              "daqui a pouco.", 429),
}


def _dictation_refused(text: str, status: int) -> Response:
    return PlainTextResponse(text, status_code=status, headers=HEADERS)


@router.post("/ditado", include_in_schema=False)
async def dictate(request: Request) -> Response:
    """A recorded voice note, as the raw body, transcribed now and answered as
    text for the box: the person reads it, corrects it and sends it as a
    typed message. Nothing is stored. The checks are the attachment's, made
    before a byte of the body is read; the transcription reserves its cost
    from the pilot budget, as the worker's does."""
    _switched_on()
    if from_another_site(request):
        return refused_from_another_site()
    if not csrf_matches(request.cookies.get(COOKIE) or "", request.headers.get("x-csrf", "")):
        return _upload_refused("refused")
    pool = await get_pool()
    async with pool.connection() as conn:
        signed_in = await current_staff(conn, request)
        if signed_in is None:
            return PlainTextResponse("ended", status_code=401, headers=HEADERS)
        staff, _session = signed_in
        if not assistant_open_to(staff):
            raise HTTPException(status_code=404)
    shared, _notify = await cache.rate_limit_hit(f"dictation:{staff.id}",
                                                 DICTATIONS_PER_HOUR, 3600)
    local, _notify = _DICTATION_LIMITER.check(staff.id)
    if not (shared and local):
        return _dictation_refused(*_DICTATION_REFUSED["limit"])
    if not _SLOTS.take():
        return _upload_refused("full")
    try:
        received = await _body(request, "voice")
        if isinstance(received, str):
            return _upload_refused(received)
        if not (sniff(received) or "").startswith("audio/"):
            return _upload_refused("type")
        # Imported here: the handler imports this package's neighbours.
        from core.handlers.legal_message import _VOICE_HINT
        try:
            transcript = await media.transcribe_audio(received, voice=True, prompt=_VOICE_HINT)
        except budget.BudgetExceeded as exc:
            return _dictation_refused(budget_reached(exc), 429)
        except media.MediaError as exc:
            return _dictation_refused(exc.message, 422)
    finally:
        _SLOTS.free()
    text = (transcript.text or "").strip()
    if is_silence_artifact(text):
        return _dictation_refused(*_DICTATION_REFUSED["silent"])
    return JSONResponse({"texto": text}, headers=HEADERS)


# ── A file the assistant offered ────────────────────────────────────────────


@router.get("/arquivo/{token}", include_in_schema=False)
async def download(request: Request, token: str):
    """Its owner's, before it expires, both decided in the query: any other
    token, someone else's included, is simply not found."""
    _switched_on()
    if len(token) > 100:
        raise HTTPException(status_code=404)
    pool = await get_pool()
    async with pool.connection() as conn:
        entered = await _enter(conn, request)
        if isinstance(entered, Response):
            return entered
        staff, _session = entered
        found = await panel.read_file(conn, token, staff_id=staff.id)
        if found is None:
            raise HTTPException(status_code=404)
        filename, _media_type, data = found
        await log_access(conn, action="download_export", staff_id=staff.id,
                         query="assistente")
    headers = dict(HEADERS)
    headers["Content-Disposition"] = attachment_disposition(filename)
    # Never a page: no script runs if a browser renders it anyway.
    headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return Response(content=data, media_type="application/octet-stream", headers=headers)


__all__ = ["NOTES", "PER_PAGE", "POLICY", "REFRESH_SECONDS", "SCRIPT", "UPLOAD_NOTES",
           "document_name", "router", "sniff"]
