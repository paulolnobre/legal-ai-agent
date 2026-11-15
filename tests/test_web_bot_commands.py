"""The bot's half of the web login: /web hands out the first factor, /mfa
looks after the second.

The property that matters most here is a negative one. The Telegram account
is the first factor, so nothing it can do alone may replace the second:
starting the authenticator over from the bot asks for a code the person still
has, and someone who has lost everything goes through the owner.
"""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core.config import settings
from core.handlers import legal_message
from core.services import channels
from core.models import StaffMember
from core.repositories.web_access import Enrolment
from core.services import second_factor

pytestmark = pytest.mark.usefixtures(
    "isolate_message_rate_limits", "pending_store", "callback_store",
)

_OWNER = StaffMember(id=1, chat_id="100", name="Paulo", role="owner")
_LAWYER = StaffMember(id=4, chat_id="400", name="Rui", role="advogado")


@pytest.fixture
def bot(monkeypatch):
    state = SimpleNamespace(
        sent=[], previews=[], links=[], forgotten=[], audit=[], checked=[],
        enrolment=Enrolment(staff_id=_LAWYER.id,
                            enrolled_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
                            recovery_left=6, confirmed=True),
        verdict=second_factor.Check.ACCEPTED, target=_LAWYER,
    )

    async def get_staff(_conn, chat_id):
        return _OWNER if str(chat_id) == "100" else _LAWYER

    async def send(chat_id, message, reply_markup=None, *, preview=True):
        state.sent.append((str(chat_id), message))
        state.previews.append(preview)
        return 1

    async def typing(*_args, **_kwargs):
        return None

    async def open_login_link(_conn, staff_id, *, ttl_seconds):
        state.links.append((staff_id, ttl_seconds))
        return "token-do-link"

    async def enrolment_of(_conn, _staff_id):
        return state.enrolment

    async def forget_enrolment(_conn, staff_id):
        state.forgotten.append(staff_id)
        return True

    async def check(_conn, staff_id, code):
        state.checked.append((staff_id, code))
        return state.verdict

    async def log_access(_conn, **kwargs):
        state.audit.append(kwargs)

    async def get_staff_by_id(_conn, _staff_id):
        return state.target

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(channels, "send_typing", typing)
    monkeypatch.setattr(legal_message.web_access, "open_login_link", open_login_link)
    monkeypatch.setattr(legal_message.web_access, "enrolment_of", enrolment_of)
    monkeypatch.setattr(legal_message.web_access, "forget_enrolment", forget_enrolment)
    monkeypatch.setattr(legal_message.second_factor, "check", check)
    monkeypatch.setattr(legal_message, "log_access", log_access)
    monkeypatch.setattr(legal_message, "get_staff_by_id", get_staff_by_id)
    monkeypatch.setattr(settings, "web_enabled", True)
    monkeypatch.setattr(settings, "public_base_url", "https://agente.exemplo.br/")
    monkeypatch.setattr(settings, "web_base_url", None)
    monkeypatch.setattr(settings, "web_login_link_ttl", 300)
    return state


def _say(text: str, chat: int = 400):
    update = {"update_id": 1, "message": {
        "chat": {"id": chat, "type": "private"}, "from": {"id": chat}, "text": text,
    }}
    asyncio.run(legal_message.process_legal_message("connection", update))


# ── /web ─────────────────────────────────────────────────────────────────────


def test_the_link_goes_to_the_person_who_asked(bot):
    _say("/web")

    assert bot.links == [(_LAWYER.id, 300)]
    chat, text = bot.sent[-1]
    assert chat == _LAWYER.chat_id
    assert "https://agente.exemplo.br/web/login/token-do-link" in text


def test_the_link_is_sent_without_a_preview(bot):
    """A preview makes Telegram's servers fetch the link first."""
    _say("/web")

    assert bot.previews[-1] is False


def test_the_link_says_it_lasts_minutes_and_works_once(bot):
    _say("/web")

    text = bot.sent[-1][1]
    assert "5 minutos" in text and "uma vez" in text


def test_no_link_while_the_pages_are_switched_off(bot, monkeypatch):
    monkeypatch.setattr(settings, "web_enabled", False)

    _say("/web")

    assert bot.links == []
    assert "/web/login/" not in bot.sent[-1][1]


def test_the_link_points_at_the_pages_host_when_there_is_one(bot, monkeypatch):
    monkeypatch.setattr(settings, "web_base_url", "https://lex.exemplo.br")

    _say("/web")

    assert "https://lex.exemplo.br/web/login/token-do-link" in bot.sent[-1][1]
    assert "agente.exemplo.br" not in bot.sent[-1][1]


def test_no_link_without_a_public_address(bot, monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "")
    monkeypatch.setattr(settings, "web_base_url", None)

    _say("/web")

    assert bot.links == []


# ── /mfa: what it says ───────────────────────────────────────────────────────


def test_the_status_counts_the_recovery_codes_left(bot):
    _say("/mfa")

    text = bot.sent[-1][1]
    assert "configurado" in text and "6" in text
    assert bot.forgotten == []


def test_someone_not_set_up_is_pointed_at_web(bot):
    bot.enrolment = None

    _say("/mfa")

    assert "/web" in bot.sent[-1][1]


def test_the_secret_is_never_sent_through_the_bot(bot):
    """The bot is the first factor. A secret sent here would give whoever
    holds the Telegram account both."""
    for text in ("/mfa", "/web"):
        _say(text)

    assert not any("otpauth://" in message for _, message in bot.sent)


# ── /mfa refazer: not by the Telegram account alone ──────────────────────────


def test_starting_over_without_a_code_does_nothing(bot):
    _say("/mfa refazer")

    assert bot.forgotten == []
    assert "código" in bot.sent[-1][1]


def test_starting_over_with_a_good_code_forgets_the_enrolment(bot):
    _say("/mfa refazer 123456")

    assert bot.checked == [(_LAWYER.id, "123456")]
    assert bot.forgotten == [_LAWYER.id]
    assert bot.audit[-1]["action"] == "reset_second_factor"


def test_starting_over_with_a_wrong_code_does_nothing(bot):
    bot.verdict = second_factor.Check.REFUSED

    _say("/mfa refazer 000000")

    assert bot.forgotten == []


def test_starting_over_while_locked_does_nothing(bot):
    bot.verdict = second_factor.Check.LOCKED

    _say("/mfa refazer 123456")

    assert bot.forgotten == []


def test_an_unconfirmed_enrolment_can_be_dropped_freely(bot):
    """It never worked, so it protects nothing, and the person may have
    closed the tab before saving the secret."""
    bot.enrolment = Enrolment(staff_id=_LAWYER.id,
                              enrolled_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
                              confirmed=False)

    _say("/mfa refazer")

    assert bot.checked == []
    assert bot.forgotten == [_LAWYER.id]


# ── /mfa liberar: the owner's way back in for someone ────────────────────────


def test_only_the_owner_may_release_someone_else(bot):
    _say(f"/mfa liberar {_OWNER.id}")

    assert bot.forgotten == []


def test_the_owner_releases_and_the_person_is_told(bot):
    _say(f"/mfa liberar {_LAWYER.id}", chat=100)

    assert bot.forgotten == [_LAWYER.id]
    told = [chat for chat, _ in bot.sent]
    assert _LAWYER.chat_id in told, "the person must hear their factor was reset"
    assert bot.audit[-1]["action"] == "reset_second_factor"
    assert bot.audit[-1]["staff_id"] == _OWNER.id


def test_releasing_an_unknown_person_does_nothing(bot):
    bot.target = None

    _say("/mfa liberar 99", chat=100)

    assert bot.forgotten == []


def test_release_without_an_id_explains_itself(bot):
    _say("/mfa liberar", chat=100)

    assert bot.forgotten == []
    assert "/mfa liberar <id>" in bot.sent[-1][1]


# ── revoking access reaches the browser ──────────────────────────────────────


def _deactivate(monkeypatch, close):
    from types import SimpleNamespace as NS

    sent = []

    async def send(_chat_id, message, reply_markup=None, *, preview=True):
        sent.append(message)
        return 1

    async def deactivate(_conn, *, actor_id, target_id):
        return NS(status="deactivated", staff=_LAWYER)

    async def none_private(_conn, _staff_id):
        return []

    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(legal_message, "deactivate_staff", deactivate)
    monkeypatch.setattr(legal_message, "private_documents_of", none_private)
    monkeypatch.setattr(legal_message.web_access, "revoke_devices", close)
    return asyncio.run(
        legal_message._apply_deactivation("conn", 100, _OWNER, _LAWYER.id),
    ), sent


def test_deactivating_somebody_closes_their_web_sessions(monkeypatch):
    closed = []

    async def close(_conn, staff_id):
        closed.append(staff_id)
        return 1

    _deactivate(monkeypatch, close)

    assert closed == [_LAWYER.id]


def test_a_failure_to_close_sessions_does_not_block_the_deactivation(monkeypatch):
    """The staff row is re-read on every request, so the session is already
    worthless. Closing it is tidiness, and must not cost the revocation."""
    async def broken(_conn, _staff_id):
        raise RuntimeError("database is away")

    result, sent = _deactivate(monkeypatch, broken)

    assert "excluído" in result
    assert any("revogado" in message for message in sent), "the person was not told"
