"""/prazo, /feriados and /feriado, through the real message handler.

The calculator and the calendar each have their own tests. What is tested here
is where they meet: a day the firm registers has to move a due date counted by
/prazo, and only an admin may register one, since every deadline the firm
counts reads that calendar.
"""
import asyncio
from datetime import date, datetime

import pytest

from core.handlers import legal_message
from core.services import channels
from core.models import StaffMember
from core.repositories.firm_holidays import FirmHoliday

pytestmark = pytest.mark.usefixtures(
    "isolate_message_rate_limits", "pending_store", "callback_store",
)

_PEOPLE = {
    "100": StaffMember(id=1, chat_id="100", name="Paulo", role="admin"),
    "400": StaffMember(id=4, chat_id="400", name="Rui", role="advogado"),
    "500": StaffMember(id=5, chat_id="500", name="Bia", role="estagiario"),
}


@pytest.fixture
def bot(monkeypatch):
    calendar: dict[date, str] = {}
    sent: list[tuple[str, str]] = []

    async def get_staff(_conn, chat_id):
        return _PEOPLE.get(str(chat_id))

    async def send(chat_id, message, reply_markup=None, *, preview=True):
        sent.append((str(chat_id), message))
        return 1

    async def typing(*_args, **_kwargs):
        return None

    async def holidays_between(_conn, start, end):
        return [FirmHoliday(day, reason) for day, reason in sorted(calendar.items())
                if start <= day <= end]

    async def add_holiday(_conn, day, reason, *, added_by):
        previous = calendar.get(day)
        calendar[day] = reason
        return previous

    async def remove_holiday(_conn, day):
        return calendar.pop(day, None)

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(channels, "send_typing", typing)
    monkeypatch.setattr(legal_message, "local_now", lambda *_: datetime(2026, 10, 6, 9, 0))
    monkeypatch.setattr(legal_message.firm_holidays, "holidays_between", holidays_between)
    monkeypatch.setattr(legal_message.firm_holidays, "add_holiday", add_holiday)
    monkeypatch.setattr(legal_message.firm_holidays, "remove_holiday", remove_holiday)
    return calendar, sent


def _say(text: str, chat: int):
    update = {"update_id": 1, "message": {
        "chat": {"id": chat, "type": "private"}, "from": {"id": chat}, "text": text,
    }}
    asyncio.run(legal_message.process_legal_message("connection", update))


def _last(sent) -> str:
    return sent[-1][1]


# ── /prazo ───────────────────────────────────────────────────────────────────


def test_an_intern_gets_the_due_date(bot):
    _calendar, sent = bot
    # Monday 10/08/2026, five working days: 11, 12, 13, 14, 17.
    _say("/prazo 10/08/2026 5 úteis", 500)

    assert _last(sent).startswith("📅 Vencimento: 17/08/2026 (segunda-feira)")


def test_a_day_the_firm_registered_moves_the_due_date(bot):
    calendar, sent = bot
    calendar[date(2026, 8, 14)] = "Suspensão por portaria do TJPA"

    _say("/prazo 10/08/2026 5 úteis", 500)

    text = _last(sent)
    assert text.startswith("📅 Vencimento: 18/08/2026"), "the registered day was counted"
    assert "Suspensão por portaria do TJPA" in text
    assert "Nenhum feriado local" not in text


def test_with_nothing_registered_the_answer_says_only_national_holidays_counted(bot):
    _calendar, sent = bot

    _say("/prazo 10/08/2026 5 úteis", 500)

    assert "Nenhum feriado local do escritório cadastrado para 2026" in _last(sent)


def test_dobro_doubles_the_deadline_the_command_counts(bot):
    """Through the handler, not the calculator: the handler is what has to
    pass the doubled count on."""
    _calendar, sent = bot
    # Monday 10/08/2026, ten working days: 11..14, 17..21, 24.
    _say("/prazo 10/08/2026 5 úteis dobro", 500)

    assert _last(sent).startswith("📅 Vencimento: 24/08/2026"), _last(sent)
    assert "Prazo em dobro: 5 dias contados como 10" in _last(sent)


def test_without_the_regime_it_asks(bot):
    _calendar, sent = bot

    _say("/prazo 10/08/2026 5", 500)

    assert "úteis ou corridos" in _last(sent)
    assert "Vencimento" not in _last(sent)


# ── /feriado and /feriados ───────────────────────────────────────────────────


def test_a_lawyer_cannot_change_the_calendar(bot):
    calendar, sent = bot

    _say("/feriado 14/08/2026 Suspensão", 400)

    assert calendar == {}
    assert "admins" in _last(sent)


def test_an_admin_registers_corrects_and_removes_a_day(bot):
    calendar, sent = bot

    _say("/feriado 15/08/2026 Adesao", 100)
    assert calendar == {date(2026, 8, 15): "Adesao"}
    assert _last(sent).startswith("Cadastrado: 15/08/2026")

    _say("/feriado 15/08/2026 Adesão do Pará", 100)
    assert calendar == {date(2026, 8, 15): "Adesão do Pará"}
    assert '"Adesao"' in _last(sent)

    _say("/feriado remover 15/08/2026", 100)
    assert calendar == {}
    assert _last(sent).startswith("Removido do calendário do escritório: 15/08/2026")
    assert "dia útil" not in _last(sent), "a Saturday or national holiday stays one"


def test_a_day_without_a_name_is_not_registered(bot):
    calendar, sent = bot

    _say("/feriado 15/08/2026", 100)

    assert calendar == {}
    assert "Qual é o nome" in _last(sent)


def test_anyone_can_read_the_calendar(bot):
    calendar, sent = bot
    calendar[date(2026, 12, 8)] = "Nossa Senhora da Conceição (Belém)"

    _say("/feriados", 500)

    assert "08/12/2026: Nossa Senhora da Conceição (Belém)" in _last(sent)


def test_the_new_commands_are_in_the_telegram_menu():
    """The menu is generated from the help text; a command missing from it is
    a command nobody finds by typing "/"."""
    names = {item["command"] for item in legal_message.command_menu()}

    assert {"prazo", "feriados", "feriado"} <= names
