"""Taking a message or a button press from the web panel.

The page never names who is speaking: the person is the one the session
re-read, and their chat is the one on their staff row. What they typed
becomes a `panel_requests` row and their turn, written together, and only
then an update on the same queue the Telegram webhook fills, deferred out of
hours by the same rule (`work_hours`). The worker answers it through the
panel's channel.

Sign-in commands are refused before anything is stored: "/mfa refazer
<código>" typed here must never reach the database. `/pje` is taken, with
nothing after it but one of its own words: its answer here is a link to a
panel page, never a token, and what follows the command is not a place for
a password either.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from core.config import settings
from core.integrations import update_queue
from core.models import StaffMember
from core.repositories import conversation, panel, panel_uploads
from core.schedule import OUT_OF_HOURS_MESSAGE
from core.services import pje, work_hours

logger = logging.getLogger(__name__)

# Done through the bot only: sign-in links, the authenticator, and the
# Microsoft sign-in, which runs in the background on Telegram. PJe enrolment
# is not here: the panel answers it with its own page (`_pje_words_only`).
REFUSED_COMMANDS = frozenset({
    "/web", "/acesso", "/mfa", "/autenticador", "/microsoft", "/ms",
})

_REFUSED = "Isso é feito pelo Telegram: envie {comando} ao bot."
# Microsoft is connected from the integrations page, not in a chat.
_MICROSOFT_WORDS = frozenset({"/microsoft", "/ms"})
_MICROSOFT_PAGE = "Conecte a Microsoft na aba Integrações."
_PJE_WORDS = ("No painel, envie só /pje, /pje cadastrar ou /pje remover. A senha do "
              "PJe é digitada na página de cadastro, nunca no chat.")
_BUSY = "Aguarde a resposta anterior."
_TOO_LONG = "A mensagem é muito longa. Pode resumir a pergunta?"
_EMPTY = "Escreva a mensagem antes de enviar."
_NO_CHAT = ("O assistente do painel usa o seu acesso pelo bot do Telegram. "
            "Peça o acesso pelo bot e volte aqui.")
_NOT_SENT = "Não consegui enviar a mensagem. Tente de novo em instantes."
_NO_BUTTON = "Esse botão não está mais disponível."
# Also what the worker says when a message's attachment is gone before it is read.
NO_UPLOAD = "O anexo não está mais disponível. Anexe de novo e envie."

Outcome = Literal["queued", "deferred", "refused", "busy", "failed", "no_chat"]


@dataclass
class Intake:
    outcome: Outcome
    request_id: UUID | None = None
    message: str = ""
    # Why, as a fixed word the page maps to its own sentence: what the person
    # typed is never carried back into the page.
    reason: str = ""


def command_word(text: str) -> str:
    """The first word, as the handler reads a command (casefolded, split on
    any whitespace), so both decide alike."""
    words = text.casefold().split(maxsplit=1)
    return words[0] if words else ""


def _pje_words_only(text: str) -> bool:
    """`/pje` alone or with one of the words its handler reads. Anything
    else after it could be a CPF or a password typed into the chat, and is
    refused before it is stored, as Telegram keeps it out of the record."""
    rest = text.casefold().split()[1:]
    return not rest or (len(rest) == 1 and rest[0] in pje.REMOVE_WORDS | pje.ENROL_WORDS)


def _chat(chat_id: str):
    """A Telegram chat id is a number; the update carries it as one."""
    text = str(chat_id)
    return int(text) if text.lstrip("-").isdigit() else text


async def submit_message(conn, staff: StaffMember, text: str, *,
                         upload_id: UUID | None = None, label: str | None = None) -> Intake:
    """A message, with the attachment sent before it when there is one. With
    an attachment the text may be empty (a photo or voice note alone); the
    attachment is bound to the request before the queue is handed it.
    `label` is shown in the person's bubble in place of the text (an action
    from the chat's list); the queue is handed the text."""
    if not staff.chat_id:
        return Intake("no_chat", message=_NO_CHAT, reason="no_chat")
    if not text.strip() and upload_id is None:
        return Intake("refused", message=_EMPTY, reason="empty")
    word = command_word(text)
    if word in _MICROSOFT_WORDS:
        return Intake("refused", message=_MICROSOFT_PAGE, reason="microsoft_page")
    if word in REFUSED_COMMANDS:
        return Intake("refused", message=_REFUSED.format(comando=word), reason="telegram")
    if word == "/pje" and not _pje_words_only(text):
        return Intake("refused", message=_PJE_WORDS, reason="pje_words")
    if len(text) > settings.max_question_chars:
        return Intake("refused", message=_TOO_LONG, reason="too_long")
    if await panel.in_flight(conn, staff.id) is not None:
        return Intake("busy", message=_BUSY, reason="busy")
    try:
        created = await panel.create_request(
            conn, staff_id=staff.id, chat_id=staff.chat_id, kind="message", text=text,
            upload_id=upload_id, label=label,
        )
    except panel_uploads.UploadUnavailable:
        return Intake("refused", message=NO_UPLOAD, reason="no_upload")
    if created is None:
        return Intake("busy", message=_BUSY, reason="busy")
    request_id, _turn = created
    chat = _chat(staff.chat_id)
    update = {
        "update_id": f"panel:{request_id}",
        "channel": "panel",
        "panel_request": str(request_id),
        "message": {"chat": {"id": chat, "type": "private"}, "from": {"id": chat},
                    "text": text, "recorded": True},
    }
    deferred = not await work_hours.may_work_now(staff.chat_id, button_press=False)
    return await _hand_over(conn, staff, request_id, update, deferred=deferred)


async def submit_press(conn, staff: StaffMember, *, token: str, turn_id: int) -> Intake:
    """A button on one of this person's panel replies. The press itself is
    decided by the worker's claim, as on Telegram; this only refuses a token
    the panel never showed them on that turn."""
    if not staff.chat_id:
        return Intake("no_chat", message=_NO_CHAT, reason="no_chat")
    turn_text = await panel.button_turn_text(conn, staff_id=staff.id, turn_id=turn_id,
                                             token=token)
    if turn_text is None:
        return Intake("refused", message=_NO_BUTTON, reason="no_button")
    if await panel.in_flight(conn, staff.id) is not None:
        return Intake("busy", message=_BUSY, reason="busy")
    created = await panel.create_request(
        conn, staff_id=staff.id, chat_id=staff.chat_id, kind="button", text=None,
    )
    if created is None:
        return Intake("busy", message=_BUSY, reason="busy")
    request_id, _turn = created
    chat = _chat(staff.chat_id)
    update = {
        "update_id": f"panel:{request_id}",
        "channel": "panel",
        "panel_request": str(request_id),
        "callback_query": {
            "id": f"panel:{request_id}",
            "from": {"id": chat},
            "data": token,
            "message": {"message_id": turn_id, "chat": {"id": chat, "type": "private"},
                        "text": turn_text},
        },
    }
    deferred = not await work_hours.may_work_now(staff.chat_id, button_press=True)
    return await _hand_over(conn, staff, request_id, update, deferred=deferred)


async def _hand_over(conn, staff: StaffMember, request_id: UUID, update: dict, *,
                     deferred: bool) -> Intake:
    try:
        await update_queue.enqueue_update(update, deferred=deferred)
    except (RuntimeError, ValueError):
        logger.exception("Could not queue a panel request")
        await panel.finish(conn, request_id, "failed")
        return Intake("failed", request_id, _NOT_SENT, reason="not_sent")
    await panel.mark_handed(conn, request_id, deferred=deferred)
    if deferred:
        # What Telegram is sent at this hour, said in the panel instead.
        await conversation.record_turn(
            conn, staff_id=staff.id, chat_id=staff.chat_id, speaker="bot",
            text=OUT_OF_HOURS_MESSAGE, channel="panel", request_id=request_id,
        )
    return Intake("deferred" if deferred else "queued", request_id)


async def withdraw(conn, staff: StaffMember, request_id: UUID) -> bool:
    """Take back a request still waiting; it never runs."""
    return await panel.withdraw(conn, request_id, staff_id=staff.id)


__all__ = ["Intake", "NO_UPLOAD", "REFUSED_COMMANDS", "command_word", "submit_message", "submit_press",
           "withdraw"]
