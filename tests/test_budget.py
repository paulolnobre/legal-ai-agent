"""The pilot budget: what the firm may spend on the model, and what happens after.

Every other limit in this project caps one thing, a document, a search, one
run's context. None of them capped the sum, so a bad week cost whatever a bad
week cost and nobody found out until the invoice.

The two properties worth holding are the ones a careless meter gets wrong. A
refused run must leave the counter exactly as it found it, or repeated refusals
would themselves spend the budget. And a run that finishes must be counted at
what it really used, not at the worst case it reserved, or the ceiling would
arrive many times too early.
"""
import asyncio

import pytest
from redis.exceptions import RedisError

from core.config import settings
from core.metrics import BUDGET_USED
from core.services import budget


class _FakeRedis:
    """Enough Redis for a counter: incrby, get and expire, all in memory."""

    def __init__(self, broken: bool = False):
        self.values: dict[str, int] = {}
        self.expiries: dict[str, int] = {}
        self.broken = broken

    async def incrby(self, key: str, amount: int) -> int:
        if self.broken:
            raise RedisError("no route to host")
        self.values[key] = self.values.get(key, 0) + amount
        return self.values[key]

    async def expire(self, key: str, ttl: int) -> bool:
        if self.broken:
            raise RedisError("no route to host")
        self.expiries[key] = ttl
        return True

    async def get(self, key: str):
        if self.broken:
            raise RedisError("no route to host")
        value = self.values.get(key)
        return None if value is None else str(value)


@pytest.fixture
def meter(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(budget, "_client", fake)
    monkeypatch.setattr(settings, "budget_enabled", True)
    monkeypatch.setattr(settings, "budget_day_input_tokens", 1_000)
    monkeypatch.setattr(settings, "budget_day_output_tokens", 200)
    monkeypatch.setattr(settings, "budget_month_input_tokens", 10_000)
    monkeypatch.setattr(settings, "budget_month_output_tokens", 2_000)
    return fake


def _counters(fake: _FakeRedis) -> dict[str, int]:
    """Just the window and kind, so a test does not depend on today's date."""
    return {
        f"{key.split(':')[2]}:{key.split(':')[4]}": value
        for key, value in fake.values.items()
    }


def test_a_finished_run_is_counted_at_what_it_really_used(meter):
    """The reservation is the task class's worst case, because there is no
    tokenizer to measure this prompt with. What stays on the meter afterwards
    is what the provider reported."""
    reservation = asyncio.run(budget.reserve(800, 150))
    asyncio.run(budget.settle(reservation, input_tokens=120, output_tokens=40))

    counters = _counters(meter)
    assert counters["day:input"] == 120
    assert counters["day:output"] == 40
    assert counters["month:input"] == 120
    assert counters["month:output"] == 40


def test_a_refused_run_leaves_the_counter_where_it_found_it(meter):
    """Otherwise the refusals themselves would spend the budget, and a firm at
    its ceiling would dig deeper every time somebody tried again."""
    first = asyncio.run(budget.reserve(800, 150))
    asyncio.run(budget.settle(first, input_tokens=800, output_tokens=150))
    before = _counters(meter)

    with pytest.raises(budget.BudgetExceeded):
        asyncio.run(budget.reserve(800, 150))

    assert _counters(meter) == before


def test_the_refusal_says_which_window_closed_and_when_it_reopens(meter):
    """"Try later" is not something anybody can plan around."""
    asyncio.run(budget.settle(asyncio.run(budget.reserve(900, 10)), 900, 10))

    with pytest.raises(budget.BudgetExceeded) as raised:
        asyncio.run(budget.reserve(900, 10))

    assert raised.value.window == "day"
    assert raised.value.reopens == "amanhã"


def test_the_month_can_refuse_a_day_that_still_fits(monkeypatch, meter):
    """The two windows are independent, and the tighter one decides."""
    monkeypatch.setattr(settings, "budget_month_input_tokens", 500)

    with pytest.raises(budget.BudgetExceeded) as raised:
        asyncio.run(budget.reserve(600, 10))

    assert raised.value.window == "month"
    assert raised.value.reopens == "no primeiro dia do mês que vem"


def test_two_runs_at_once_cannot_both_believe_they_fit(meter):
    """Why the counter is a Redis integer and every change is an INCRBY: with
    more than one worker, read-then-write would let both read the same number.
    """
    async def both():
        return await asyncio.gather(
            budget.reserve(600, 10), budget.reserve(600, 10),
            return_exceptions=True,
        )

    outcomes = asyncio.run(both())

    refused = [o for o in outcomes if isinstance(o, budget.BudgetExceeded)]
    assert len(refused) == 1, "exactly one of the two had to be turned away"


def test_an_unreachable_meter_lets_the_run_through(monkeypatch):
    """Deliberate, and documented in the module. Refusing to answer the firm
    because a counter is unavailable costs more than the tokens it saves, and
    the queue lives in the same Redis: one nobody can reach is delivering no
    work to spend on."""
    monkeypatch.setattr(budget, "_client", _FakeRedis(broken=True))
    monkeypatch.setattr(settings, "budget_enabled", True)

    assert asyncio.run(budget.reserve(800, 150)) is None


def test_settling_nothing_is_harmless(meter):
    """A run that was never metered, because the meter was down, still calls
    settle on the way out."""
    asyncio.run(budget.settle(None, 10, 10))

    assert _counters(meter) == {}


def test_a_disabled_budget_counts_nothing(monkeypatch, meter):
    monkeypatch.setattr(settings, "budget_enabled", False)

    assert asyncio.run(budget.reserve(800, 150)) is None
    assert _counters(meter) == {}


def test_a_run_that_used_more_than_it_reserved_is_not_refunded_into_credit(meter):
    """The per-run limits answer for a run that overruns its class. This meter
    must not quietly hand the budget back because the numbers disagree."""
    reservation = asyncio.run(budget.reserve(100, 10))
    asyncio.run(budget.settle(reservation, input_tokens=999, output_tokens=999))

    counters = _counters(meter)
    assert counters["day:input"] == 100, "counted at the reservation, never below"
    assert counters["day:output"] == 10


def test_every_counter_is_given_an_expiry(meter):
    """A counter for a window that has closed must not live for ever."""
    asyncio.run(budget.reserve(10, 10))

    assert meter.expiries, "nothing was given a TTL"
    assert all(ttl > 0 for ttl in meter.expiries.values())


def test_what_is_spent_is_published_as_a_share_of_the_ceiling(meter):
    """A ratio rather than a count, so one alert rule survives somebody
    changing a ceiling."""
    asyncio.run(budget.settle(asyncio.run(budget.reserve(500, 10)), 500, 10))

    assert BUDGET_USED.labels("day", "input")._value.get() == pytest.approx(0.5)


def test_the_report_reads_both_windows_with_their_ceilings(meter):
    asyncio.run(budget.settle(asyncio.run(budget.reserve(300, 20)), 300, 20))

    report = asyncio.run(budget.used())

    assert report["day"]["input"] == 300
    assert report["day"]["input_limit"] == 1_000
    assert report["month"]["input"] == 300
    assert report["month"]["input_limit"] == 10_000


# ── what the lawyer sees ────────────────────────────────────────────────────


def test_the_ceiling_reaches_the_lawyer_as_a_limit_not_as_a_failure(monkeypatch):
    """The graph turns every exception into "não consegui processar", which
    would be both untrue and unhelpful here: nothing broke, the firm set a
    ceiling and reached it, and the answer has to say which window closed and
    when it reopens."""
    from core.services.legal_assistant import graph

    async def refuse(_state, **_kwargs):
        raise budget.BudgetExceeded("day", "amanhã")

    monkeypatch.setattr(graph._app, "ainvoke", refuse)

    reply = asyncio.run(graph.run("connection", "qualquer pergunta"))

    assert "orçamento" in reply.text
    assert "amanhã" in reply.text
    assert reply.text != graph.GRAPH_ERROR
    # Everything that needs no model keeps working, and the answer says so.
    assert "/docs" in reply.text


def test_a_real_failure_still_reads_as_a_failure(monkeypatch):
    """The branch above must not swallow the generic error it sits beside."""
    from core.services.legal_assistant import graph

    async def blow_up(_state, **_kwargs):
        raise RuntimeError("something actually broke")

    monkeypatch.setattr(graph._app, "ainvoke", blow_up)

    reply = asyncio.run(graph.run("connection", "qualquer pergunta"))

    assert reply.text == graph.GRAPH_ERROR


# ── /status, for the people who answer for the spending ─────────────────────


def _status_as(monkeypatch, role: str, report: dict | None = None) -> str:
    """Send /status as someone of this rank and return what they were told."""
    from core.handlers import legal_message
    from core.models import StaffMember

    person = StaffMember(id=1, chat_id="100", name="Pessoa", role=role)
    sent: list[str] = []

    async def get_staff(_conn, chat_id):
        return person if str(chat_id) == "100" else None

    async def send(_chat_id, text, reply_markup=None):
        sent.append(text)
        return 1

    async def used():
        return {} if report is None else report

    legal_message._last_deliveries.clear()
    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(legal_message.budget, "used", used)
    monkeypatch.setattr(settings, "budget_enabled", True)

    update = {"update_id": 1, "message": {
        "chat": {"id": 100, "type": "private"},
        "from": {"id": 100, "first_name": "Pessoa"}, "text": "/status",
    }}
    asyncio.run(legal_message.process_legal_message("connection", update))
    return sent[-1]


_REPORT = {
    "day": {"input": 3422, "input_limit": 250_000, "output": 517, "output_limit": 50_000},
    "month": {"input": 3422, "input_limit": 3_000_000, "output": 517, "output_limit": 600_000},
}


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_owner_and_admin_see_what_the_pilot_spent(monkeypatch, pending_store, role):
    text = _status_as(monkeypatch, role, _REPORT)

    assert "Orçamento do piloto" in text
    # The firm's number format, not the server's.
    assert "3.422 de 250.000" in text
    assert "3.422 de 3.000.000" in text
    assert "no máximo US$ 0,01" in text


@pytest.mark.parametrize("role", ["advogado", "estagiario"])
def test_nobody_else_is_shown_the_spending(monkeypatch, pending_store, role):
    """The pilot policy says owner and admin. A lawyer asking what the
    assistant is waiting for gets exactly that and nothing about money."""
    text = _status_as(monkeypatch, role, _REPORT)

    assert "Orçamento" not in text
    assert "US$" not in text


def test_a_silent_meter_does_not_cost_the_rest_of_status(monkeypatch, pending_store):
    """The flow report is what /status is for. The budget is an addition to it,
    and an unreachable counter must say so rather than take the answer down."""
    text = _status_as(monkeypatch, "owner", report=None)

    assert "Nenhum fluxo" in text
    assert "o medidor não respondeu agora" in text


def test_the_cost_is_a_ceiling_at_the_dearer_models_rate():
    """The counters do not know which model spent what, so the figure is what
    it would be if every token had gone to gpt-4o, and it is labelled as such."""
    assert budget.cost_ceiling(1_000_000, 0) == pytest.approx(settings.budget_price_input_per_million)
    assert budget.cost_ceiling(0, 1_000_000) == pytest.approx(settings.budget_price_output_per_million)


def test_money_is_written_the_way_the_firm_writes_it():
    from core.handlers.legal_message import _br_money

    assert _br_money(0.0137) == "0,01"
    assert _br_money(1234.5) == "1.234,50"
    assert _br_money(25) == "25,00"
