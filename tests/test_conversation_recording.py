"""Every message in and out of a conversation is recorded.

The person's message is recorded once it is text (a voice note as its
transcript), and every message the bot sends that person is recorded where
every reply already passes: `_send`. A message to someone else (an admin's
notification) is not this conversation. Recording must never cost a reply.
"""
import asyncio
from datetime import datetime

import pytest

from core.handlers import legal_message
from core.services import channels
from core.models import StaffMember

pytestmark = pytest.mark.usefixtures(
    "isolate_message_rate_limits", "pending_store", "callback_store",
)

_PEOPLE = {"100": StaffMember(id=1, chat_id="100", name="Paulo", role="admin")}


@pytest.fixture
def bot(monkeypatch):
    turns: list[tuple[int, str, str, str]] = []
    delivered: list[tuple[str, str]] = []

    async def get_staff(_conn, chat_id):
        return _PEOPLE.get(str(chat_id))

    async def send_message(chat_id, text, **_kwargs):
        delivered.append((str(chat_id), text))
        return 1

    async def typing(*_args, **_kwargs):
        return None

    async def record_turn(_conn, *, staff_id, chat_id, speaker, text):
        turns.append((staff_id, str(chat_id), speaker, text))

    async def holidays_between(_conn, _start, _end):
        return []

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(channels, "send_message", send_message)
    monkeypatch.setattr(channels, "send_typing", typing)
    monkeypatch.setattr(legal_message, "local_now", lambda *_: datetime(2026, 10, 6, 9, 0))
    monkeypatch.setattr(legal_message.firm_holidays, "holidays_between", holidays_between)
    monkeypatch.setattr(legal_message.conversation, "record_turn", record_turn)
    return turns, delivered


def _say(chat: int, *, text: str | None = None, voice: bool = False):
    message: dict = {"chat": {"id": chat, "type": "private"}, "from": {"id": chat}}
    if voice:
        message["voice"] = {"file_id": "v", "mime_type": "audio/ogg"}
    else:
        message["text"] = text
    asyncio.run(legal_message.process_legal_message(
        "connection", {"update_id": 1, "message": message}))


def test_a_typed_question_and_its_answer_are_both_recorded(bot):
    turns, _ = bot

    _say(100, text="/prazo 10/08/2026 5 úteis")

    assert turns[0] == (1, "100", "person", "/prazo 10/08/2026 5 úteis")
    assert turns[1][:3] == (1, "100", "bot")
    assert turns[1][3].startswith("📅 Vencimento: 17/08/2026")
    assert len(turns) == 2


def test_a_voice_note_is_recorded_as_its_transcript(bot, monkeypatch):
    turns, _ = bot

    async def transcript(_chat_id, _voice):
        return "/feriados"

    monkeypatch.setattr(legal_message, "_voice_to_text", transcript)

    _say(100, voice=True)

    assert (1, "100", "person", "/feriados") in turns
    # The person spoke first; the bot's "🎤 Entendi" echo comes after, or the
    # router would read the conversation in the wrong order.
    speakers = [turn[2] for turn in turns]
    assert speakers[0] == "person"
    assert turns[1][3].startswith("🎤 Entendi")


def test_a_message_to_another_chat_is_not_this_conversation(bot):
    turns, delivered = bot

    async def go():
        async with legal_message._serving("connection", _PEOPLE["100"]):
            await legal_message._send(999, "aviso para outra pessoa")
            await legal_message._send(100, "resposta a quem perguntou")

    asyncio.run(go())

    assert [turn[3] for turn in turns] == ["resposta a quem perguntou"]
    assert ("999", "aviso para outra pessoa") in delivered


def test_a_long_reply_sent_in_parts_is_one_turn(bot):
    turns, delivered = bot
    long_text = "x" * ((channels.TELEGRAM.max_text or 0) * 2 + 10)

    async def go():
        async with legal_message._serving("connection", _PEOPLE["100"]):
            await legal_message._send(100, long_text)

    asyncio.run(go())

    assert len(delivered) == 3
    assert [turn[3] for turn in turns] == [long_text]


def test_a_recording_failure_never_stops_the_reply(bot, monkeypatch):
    _turns, delivered = bot

    async def broken(*_args, **_kwargs):
        raise RuntimeError("database down")

    monkeypatch.setattr(legal_message.conversation, "record_turn", broken)

    _say(100, text="/prazo 10/08/2026 5 úteis")

    assert any(text.startswith("📅 Vencimento") for _, text in delivered)


def test_nothing_is_recorded_for_someone_not_on_the_staff(bot, monkeypatch):
    turns, _ = bot

    async def no_request(*_args, **_kwargs):
        return None

    monkeypatch.setattr(legal_message, "_handle_pending_access", no_request)

    _say(555, text="olá")

    assert turns == []


def test_outside_a_conversation_nothing_is_recorded(bot):
    turns, delivered = bot

    asyncio.run(legal_message._send(100, "lembrete agendado"))

    assert turns == [] and delivered == [("100", "lembrete agendado")]


def test_once_the_message_is_served_nothing_more_is_recorded_as_it(bot):
    """The worker serves one update after another in the same task: a binding
    left behind would file the next chat's replies under this person."""
    turns, _ = bot

    async def go():
        async with legal_message._serving("connection", _PEOPLE["100"]):
            pass
        await legal_message._send(100, "depois")

    asyncio.run(go())

    assert turns == []


# ── found in the final review ────────────────────────────────────────────────


@pytest.mark.parametrize("typed", ["/mfa refazer ABCD-1234-EFGH", "/web", "/pje cadastrar"])
def test_sign_in_flows_are_never_recorded_beyond_the_command_name(bot, monkeypatch, typed):
    """A recovery code typed by the person, a one-time login or PJe link sent
    by the bot: none may sit in the table for 90 days or reach the router."""
    turns, _ = bot

    async def leak(conn, chat_id, staff, *args, **kwargs):
        await legal_message._send(chat_id, "https://lex.example/web/login/SECRETTOKEN123456789")

    for handler in ("_handle_web", "_handle_mfa", "_handle_pje"):
        if hasattr(legal_message, handler):
            monkeypatch.setattr(legal_message, handler, leak)

    _say(100, text=typed)

    recorded = " ".join(turn[3] for turn in turns)
    assert "SECRETTOKEN" not in recorded and "ABCD-1234" not in recorded
    assert [turn[3] for turn in turns if turn[2] == "person"] == [typed.split()[0]]


def test_an_access_link_is_masked_wherever_it_appears(bot):
    turns, _ = bot

    async def go():
        async with legal_message._serving("connection", _PEOPLE["100"]):
            await legal_message._send(100, "Seu link: https://lex.example/web/login/abcDEF123456789xyz "
                                           "e o PJe: https://lex.example/pje/QWERTYUIOP0987654321")

    asyncio.run(go())

    assert "abcDEF123456789xyz" not in turns[0][3]
    assert "QWERTYUIOP0987654321" not in turns[0][3]
    assert "[link de acesso]" in turns[0][3]


def test_a_dictated_recovery_code_is_not_recorded_either(bot, monkeypatch):
    turns, _ = bot

    async def transcript(_chat_id, _voice):
        return "/mfa refazer ABCD-1234-EFGH"

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(legal_message, "_voice_to_text", transcript)
    monkeypatch.setattr(legal_message, "_handle_mfa", nothing)

    _say(100, voice=True)

    assert "ABCD-1234" not in " ".join(turn[3] for turn in turns)
