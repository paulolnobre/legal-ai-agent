"""Replies to the person being served leave through one channel.

Today that channel is Telegram; the panel's comes next. These pin the seam
itself: which channel and which chat are bound while an update is served,
and that the Telegram channel makes exactly the calls the handler made
before the seam existed.
"""
import asyncio

import pytest

from core.config import LEGAL_TELEGRAM_URL
from core.services import channels
from core.services.channels import Button


def test_the_default_channel_is_telegram():
    assert channels.current() is channels.TELEGRAM
    assert channels.served_chat() is None


def test_serving_binds_and_resets_channel_and_chat():
    other = object.__new__(type("Other", (), {"name": "other"}))

    with channels.serving(channels.TELEGRAM, 100):
        assert channels.served_chat() == "100"
        with channels.serving(other, "200"):  # type: ignore[arg-type]
            assert channels.current() is other and channels.served_chat() == "200"
        assert channels.current() is channels.TELEGRAM and channels.served_chat() == "100"

    with pytest.raises(RuntimeError):
        with channels.serving(other, 300):  # type: ignore[arg-type]
            raise RuntimeError("boom")
    assert channels.current() is channels.TELEGRAM and channels.served_chat() is None


def test_for_chat_is_the_current_channel_only_for_the_served_chat():
    other = object.__new__(type("Other", (), {"name": "other"}))

    with channels.serving(other, 100):  # type: ignore[arg-type]
        assert channels.for_chat(100) is other
        assert channels.for_chat("100") is other
        assert channels.for_chat(999) is channels.TELEGRAM


def _record(monkeypatch, name: str, result=None):
    calls: list[tuple[tuple, dict]] = []

    async def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr(channels, name, fake)
    return calls


def test_telegram_send_text_builds_the_inline_keyboard_from_buttons(monkeypatch):
    calls = _record(monkeypatch, "send_message", result=42)

    reference = asyncio.run(channels.TELEGRAM.send_text(
        7, "Ajudou?", [[Button("Ajudou", "t1"), Button("Não ajudou", "t2")]], preview=False))

    assert reference == "42"
    assert calls == [((7, "Ajudou?"), {
        "base_url": LEGAL_TELEGRAM_URL, "parse_mode": None, "preview": False,
        "reply_markup": {"inline_keyboard": [[
            {"text": "Ajudou", "callback_data": "t1"},
            {"text": "Não ajudou", "callback_data": "t2"}]]},
    })]


def test_telegram_channel_calls_exactly_todays_functions(monkeypatch):
    sent = _record(monkeypatch, "send_message", result=None)
    documents = _record(monkeypatch, "send_document", result=True)
    typing = _record(monkeypatch, "send_typing")
    edits = _record(monkeypatch, "edit_message_text", result=True)
    answers = _record(monkeypatch, "answer_callback_query", result=True)

    async def run():
        plain = await channels.TELEGRAM.send_text(7, "oi", None, preview=True)
        document = await channels.TELEGRAM.send_file(
            7, "a.pdf", b"%PDF", "Arquivo", kind="original", document_id=3)
        await channels.TELEGRAM.typing(7)
        await channels.TELEGRAM.edit_text(7, "55", "decidido")
        await channels.TELEGRAM.ack_button("q1", "feito")
        return plain, document

    plain, document = asyncio.run(run())

    assert plain is None  # Telegram could not send: no reference
    assert document  # sent
    assert sent == [((7, "oi"), {"base_url": LEGAL_TELEGRAM_URL, "parse_mode": None,
                                 "preview": True, "reply_markup": None})]
    assert documents == [((7, b"%PDF", "a.pdf"), {"caption": "Arquivo",
                                                  "base_url": LEGAL_TELEGRAM_URL})]
    assert typing == [((7,), {"base_url": LEGAL_TELEGRAM_URL})]
    assert edits == [((7, 55, "decidido"), {"base_url": LEGAL_TELEGRAM_URL})]
    assert answers == [(("q1", "feito"), {"base_url": LEGAL_TELEGRAM_URL})]


def test_telegram_ack_without_a_callback_id_does_nothing(monkeypatch):
    answers = _record(monkeypatch, "answer_callback_query", result=True)

    asyncio.run(channels.TELEGRAM.ack_button(None, "x"))

    assert answers == []
