"""Pure-logic checks for the webhook, schedule and document ingestion."""
import asyncio
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException, Request

from core.handlers import legal_message
from core.services import channels, work_hours
from core.schedule import is_office_hours
from core.services.ingestion import extract_text_bytes
from core.web import app as web_app


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 9, 1, hour, minute, tzinfo=ZoneInfo("America/Sao_Paulo"))


def test_office_window_boundaries():
    assert not is_office_hours(_at(7, 59))
    assert is_office_hours(_at(8, 0))
    assert is_office_hours(_at(20, 0))
    assert not is_office_hours(_at(20, 1))


def test_ingestion_accepts_text_bytes():
    assert extract_text_bytes("Cláusula primeira".encode(), "contrato.txt") == "Cláusula primeira"


def test_caption_fields_are_explicit():
    caption = "caso: Cliente X; tipo: contrato"
    assert legal_message._caption_field(caption, "(?:caso|processo|matéria)") == "Cliente X"
    assert legal_message._caption_field(caption, "tipo") == "contrato"


class _Request(Request):
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload

    async def json(self) -> Any:
        return self.payload


def test_out_of_hours_webhook_persists_before_reply(
    monkeypatch: pytest.MonkeyPatch,
):
    calls = []

    async def enqueue(update, *, deferred):
        calls.append(("queue", update["update_id"], deferred))
        return True

    async def send(chat_id, text, parse_mode=None, reply_markup=None):
        calls.append(("send", chat_id, parse_mode, text))
        return True

    monkeypatch.setattr(web_app.settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(work_hours, "is_office_hours", lambda: False)
    monkeypatch.setattr(web_app.update_queue, "enqueue_update", enqueue)
    monkeypatch.setattr(web_app.telegram, "send_message", send)

    result = asyncio.run(
        web_app.telegram_webhook(
            _Request({"update_id": 10, "message": {"chat": {"id": 99}}}),
            "secret",
        )
    )
    assert result == {"ok": True, "queued": True, "deferred": True}
    assert calls[0] == ("queue", 10, True)
    assert calls[1][0:3] == ("send", 99, None)


def test_a_button_press_is_never_deferred(monkeypatch: pytest.MonkeyPatch):
    """A press answers a message already on someone's screen.

    Out of hours the deferred queue is not drained until morning, so a press
    put there is a press that never runs: no answer to Telegram, no decision,
    a button that spins and stops. From inside the application that looks
    exactly like an update that never arrived, which is the shape this bug
    took in production.
    """
    calls = []

    async def enqueue(update, *, deferred):
        calls.append(("queue", update["update_id"], deferred))
        return True

    async def send(chat_id, text, parse_mode=None, reply_markup=None):
        calls.append(("send", chat_id, text))
        return True

    async def unrestricted(chat_id):
        calls.append(("access-check", chat_id))
        return False

    monkeypatch.setattr(web_app.settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(work_hours, "is_office_hours", lambda: False)
    monkeypatch.setattr(work_hours, "_may_work_out_of_hours", unrestricted)
    monkeypatch.setattr(web_app.update_queue, "enqueue_update", enqueue)
    monkeypatch.setattr(web_app.telegram, "send_message", send)

    press = {
        "update_id": 11,
        "callback_query": {
            "id": "q1",
            "data": "token",
            "from": {"id": 99},
            "message": {"message_id": 5, "chat": {"id": 99}},
        },
    }
    result = asyncio.run(web_app.telegram_webhook(_Request(press), "secret"))

    assert result == {"ok": True, "queued": True, "deferred": False}
    assert calls == [("queue", 11, False)], (
        "a press must reach the active queue, without an access lookup and "
        "without the out-of-hours reply"
    )


def test_a_message_out_of_hours_is_still_deferred(monkeypatch: pytest.MonkeyPatch):
    """The exception is the press, not the hour."""
    queued_as = []

    async def enqueue(update, *, deferred):
        queued_as.append(deferred)
        return True

    async def send(chat_id, text, parse_mode=None, reply_markup=None):
        return True

    async def unrestricted(chat_id):
        return False

    monkeypatch.setattr(web_app.settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(work_hours, "is_office_hours", lambda: False)
    monkeypatch.setattr(work_hours, "_may_work_out_of_hours", unrestricted)
    monkeypatch.setattr(web_app.update_queue, "enqueue_update", enqueue)
    monkeypatch.setattr(web_app.telegram, "send_message", send)

    asyncio.run(web_app.telegram_webhook(
        _Request({"update_id": 12, "message": {"chat": {"id": 99}}}), "secret",
    ))

    assert queued_as == [True]


def test_webhook_rejects_invalid_secret(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(web_app.settings, "telegram_webhook_secret", "expected")
    with pytest.raises(HTTPException) as error:
        asyncio.run(web_app.telegram_webhook(_Request({"update_id": 1}), "wrong"))
    assert error.value.status_code == 401


def test_webhook_refuses_a_secret_outside_ascii(monkeypatch: pytest.MonkeyPatch):
    """Headers arrive decoded as latin-1; a byte above 0x7f is a refusal, not a 500."""
    monkeypatch.setattr(web_app.settings, "telegram_webhook_secret", "expected")
    with pytest.raises(HTTPException) as error:
        asyncio.run(web_app.telegram_webhook(_Request({"update_id": 1}), "expécted"))
    assert error.value.status_code == 401


def test_document_is_ingested_only_after_download(monkeypatch: pytest.MonkeyPatch):
    calls = []

    async def typing(*args, **kwargs):
        return None

    async def download(*args, **kwargs):
        calls.append("download")
        return b"document"

    async def ingest(conn, data, **kwargs):
        calls.append(("ingest", conn, data, kwargs["uploaded_by"]))
        return 12, 3

    async def send(chat_id, text, reply_markup=None):
        calls.append(("send", chat_id, text))

    monkeypatch.setattr(channels, "send_typing", typing)
    monkeypatch.setattr(legal_message, "download_file", download)
    monkeypatch.setattr(legal_message, "ingest_bytes", ingest)
    monkeypatch.setattr(legal_message, "_send", send)

    asyncio.run(
        legal_message._queue_document(
            "connection",
            99,
            {"file_id": "file", "file_name": "contrato.pdf", "file_size": 8},
            SimpleNamespace(id=7),
            caption="caso: Cliente X",
        )
    )
    assert calls[0] == "download"
    assert calls[1][0] == "send"
    assert calls[2] == ("ingest", "connection", b"document", 7)
    assert calls[3][0] == "send"


def test_original_document_is_returned_only_through_the_authorized_handler(monkeypatch):
    calls = []

    async def source(conn, document_id, scope=None):
        return SimpleNamespace(
            source_storage_key="ab/cd/document.pdf",
            source_wrapped_key=None,
            source_sha256="b" * 64,
            source_filename="contrato.pdf",
        )

    async def typing(*args, **kwargs):
        calls.append("typing")

    async def send_original(*args, **kwargs):
        calls.append(("document", args, kwargs))
        return True

    async def log(*args, **kwargs):
        calls.append(("audit", kwargs))

    monkeypatch.setattr(legal_message, "get_document_source", source)
    async def _read(key, wrapped_key=None, **kwargs):
        return b"original"

    monkeypatch.setattr(legal_message.document_storage, "read", _read)
    monkeypatch.setattr(channels, "send_typing", typing)
    monkeypatch.setattr(channels, "send_document", send_original)
    monkeypatch.setattr(legal_message, "log_access", log)

    asyncio.run(legal_message._send_original_document("connection", 99, 12, 7))

    # Two audit entries, not one, and in this order. The first says the
    # original was reconstituted in clear on this host; the second says it
    # reached the person. With the archive in a bucket those are different
    # facts, because the bucket only ever held ciphertext, and a delivery that
    # fails after decryption must still leave the first entry behind.
    assert calls[0] == (
        "audit",
        {
            "action": "decrypt_original",
            "staff_id": 7,
            "chat_id": 99,
            "document_ids": [12],
        },
    )
    assert calls[1] == "typing"
    assert calls[2][0] == "document"
    assert calls[2][1][2] == "contrato.pdf"
    assert calls[3] == (
        "audit",
        {
            "action": "download_original",
            "staff_id": 7,
            "chat_id": 99,
            "document_ids": [12],
        },
    )


def test_an_update_from_outside_never_passes_for_a_panel_request(
        monkeypatch: pytest.MonkeyPatch):
    """Only the server builds a panel update; the keys that mark one are
    dropped from anything the webhook receives."""
    queued: list[dict] = []

    async def enqueue(update, *, deferred):
        queued.append(update)
        return True

    monkeypatch.setattr(web_app.settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(work_hours, "is_office_hours", lambda: True)
    monkeypatch.setattr(web_app.update_queue, "enqueue_update", enqueue)

    asyncio.run(web_app.telegram_webhook(_Request({
        "update_id": 13, "channel": "panel", "panel_request": "x",
        "message": {"chat": {"id": 99}, "text": "oi", "recorded": True},
    }), "secret"))

    assert queued == [{"update_id": 13, "message": {"chat": {"id": 99}, "text": "oi"}}]
