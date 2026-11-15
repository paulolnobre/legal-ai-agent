"""Where replies to the person being served go.

The handler used to call Telegram directly in twenty-one places. It now asks
for the current channel instead, so the same code can answer a message that
came from the web panel without any of it reaching Telegram. The channel and
the chat it serves are bound per update, like the reader's scope: a path that
forgets to bind them falls back to Telegram, which is what every path did
before this module existed.

Only the person being served goes through the bound channel. A notice to
anyone else (a manager told of an access request, a holder told a document
was taken) is not that person's conversation and keeps going by Telegram;
`for_chat` is how a caller tells the two apart.
"""
from __future__ import annotations

import contextlib
import logging
import mimetypes
from contextvars import ContextVar
from typing import Literal, NamedTuple, Protocol
from uuid import UUID

from core import document_crypto
from core.config import LEGAL_TELEGRAM_URL, settings
from core.integrations.telegram import (
    answer_callback_query,
    edit_message_text,
    send_document,
    send_message,
    send_typing,
)
from core.repositories import conversation, panel

logger = logging.getLogger(__name__)

FileKind = Literal["original", "export"]


class Button(NamedTuple):
    label: str
    token: str


class Channel(Protocol):
    name: str
    # True when sending is itself the record of the reply (the panel's rows),
    # so `_send` must not record it a second time.
    records_itself: bool
    # The longest text one message carries, or None for no limit.
    max_text: int | None

    async def send_text(self, chat_id, text: str, buttons: list[list[Button]] | None,
                        *, preview: bool) -> str | None: ...

    async def send_file(self, chat_id, filename: str, data: bytes, caption: str | None,
                        *, kind: FileKind, document_id: int | None) -> str | None: ...

    async def typing(self, chat_id) -> None: ...

    async def edit_text(self, chat_id, reference: str, text: str) -> None: ...

    async def ack_button(self, callback_id: str | None, text: str | None) -> None: ...


def _keyboard(buttons: list[list[Button]] | None) -> dict | None:
    if not buttons:
        return None
    return {"inline_keyboard": [[{"text": b.label, "callback_data": b.token} for b in row]
                                for row in buttons]}


class _Telegram:
    """Today's calls, made exactly as the handler made them."""

    name: str = "telegram"
    records_itself: bool = False
    max_text: int | None = 4000  # safe limit below Telegram's 4096-char ceiling

    async def send_text(self, chat_id, text, buttons, *, preview):
        message_id = await send_message(
            chat_id, text, base_url=LEGAL_TELEGRAM_URL, parse_mode=None,
            preview=preview, reply_markup=_keyboard(buttons),
        )
        return None if message_id is None else str(message_id)

    async def send_file(self, chat_id, filename, data, caption, *, kind, document_id):
        sent = await send_document(chat_id, data, filename, caption=caption,
                                   base_url=LEGAL_TELEGRAM_URL)
        return "sent" if sent else None

    async def typing(self, chat_id):
        await send_typing(chat_id, base_url=LEGAL_TELEGRAM_URL)

    async def edit_text(self, chat_id, reference, text):
        await edit_message_text(chat_id, int(reference), text, base_url=LEGAL_TELEGRAM_URL)

    async def ack_button(self, callback_id, text):
        if callback_id:
            await answer_callback_query(callback_id, text, base_url=LEGAL_TELEGRAM_URL)


TELEGRAM: Channel = _Telegram()


_TOO_BIG = "Esse arquivo é grande demais para o painel."


class PanelChannel:
    """Replies to a message typed in the web panel: rows in the conversation,
    never a call to Telegram.

    The row is the delivery (revision 8): a reply, its buttons and its
    request in one statement, whole however long it is, and a failure to
    write it raises, so the request ends failed instead of the person
    waiting on a reply that was never kept. One is built per update, bound
    to the person, their chat and the request being answered.

    Every write goes through `panel.publishing`, so an erasure that ran
    while the answer was being made is never undone by it: the request is
    gone, nothing is written, and `panel.RequestGone` stops the request.
    """

    name: str = "panel"
    records_itself: bool = True
    max_text: int | None = None

    def __init__(self, conn, *, staff_id: int, chat_id, request_id: UUID):
        self._conn = conn
        self._staff_id = staff_id
        self._chat_id = str(chat_id)
        self._request_id = request_id

    async def _say(self, text: str, extras: dict | None = None, *,
                   require_active: bool = True) -> str:
        async with panel.publishing(self._conn, self._request_id,
                                    require_active=require_active) as ok:
            if not ok:
                raise panel.RequestGone
            async with self._conn.cursor() as cur:
                turn_id = await conversation.insert_turn(
                    cur, staff_id=self._staff_id, chat_id=self._chat_id, speaker="bot",
                    text=conversation.redact_access_links(text), channel="panel",
                    extras=extras, request_id=self._request_id,
                )
        return str(turn_id)

    async def refuse(self, text: str) -> str:
        """Tell a person no longer active why nothing else runs: the one write
        that does not need them active, still only while the request exists."""
        return await self._say(text, require_active=False)

    async def send_text(self, chat_id, text, buttons, *, preview):
        extras = None
        if buttons:
            extras = panel.buttons_extras([[(b.label, b.token) for b in row]
                                           for row in buttons])
        return await self._say(text, extras)

    async def send_file(self, chat_id, filename, data, caption, *, kind, document_id):
        label = caption or filename
        if kind == "original":
            # Linked, never copied: the download decides rank and scope again
            # when it is used, and writes the audit entries then.
            return await self._say(label, {"file": {
                "href": f"/web/arquivos/{int(document_id)}/original", "name": filename}})
        media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        hours = settings.panel_file_ttl_hours
        turn_id = 0
        # The file and the reply that offers it in one guarded transaction:
        # an erasure finds both or neither.
        try:
            async with panel.publishing(self._conn, self._request_id) as ok:
                if not ok:
                    raise panel.RequestGone
                token = await panel.store_file(self._conn, staff_id=self._staff_id,
                                               filename=filename, media_type=media_type,
                                               data=data, commit=False)
                if token is not None:
                    async with self._conn.cursor() as cur:
                        turn_id = await conversation.insert_turn(
                            cur, staff_id=self._staff_id, chat_id=self._chat_id,
                            speaker="bot",
                            text=conversation.redact_access_links(
                                f"{label}\nDisponível por {hours} h."),
                            channel="panel", request_id=self._request_id,
                            extras={"file": {"href": f"/web/assistente/arquivo/{token}",
                                             "name": filename, "hours": hours}},
                        )
        except document_crypto.EncryptionError:
            logger.error("A panel file could not be sealed: the documents' key is missing")
            return None
        if token is None:
            return await self._say(_TOO_BIG)
        return str(turn_id)

    async def typing(self, chat_id):
        return None

    async def edit_text(self, chat_id, reference, text):
        if not str(reference).isdigit():
            return
        async with panel.publishing(self._conn, self._request_id) as ok:
            if not ok:
                raise panel.RequestGone
            await conversation.edit_panel_turn(self._conn, staff_id=self._staff_id,
                                               turn_id=int(reference), text=text,
                                               commit=False)

    async def ack_button(self, callback_id, text):
        # What Telegram shows as a toast over the button, said as a short reply.
        if text:
            await self._say(text)

_channel: ContextVar[Channel] = ContextVar("reply_channel", default=TELEGRAM)
_served: ContextVar[str | None] = ContextVar("served_chat", default=None)


def current() -> Channel:
    """The channel of the update being served."""
    return _channel.get()


def served_chat() -> str | None:
    """The chat of the person being served, as a string, or None."""
    return _served.get()


def for_chat(chat_id) -> Channel:
    """The channel a message to this chat goes by: the bound one for the
    person being served, Telegram for anyone else."""
    served = _served.get()
    return _channel.get() if served is not None and str(chat_id) == served else TELEGRAM


@contextlib.contextmanager
def serving(channel: Channel, chat_id):
    """Bind the channel and the served chat for one update, then restore."""
    channel_token = _channel.set(channel)
    chat_token = _served.set(None if chat_id is None else str(chat_id))
    try:
        yield
    finally:
        _served.reset(chat_token)
        _channel.reset(channel_token)


__all__ = ["Button", "Channel", "FileKind", "PanelChannel", "TELEGRAM", "current",
           "for_chat", "served_chat", "serving"]
