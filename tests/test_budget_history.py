"""What a month cost, kept where nothing expires it.

The meter answers "am I near the ceiling" and forgets everything else: the
Redis counters expire in 40 days, and the Prometheus gauges live in the
application's memory, which a deploy resets. Measured in production on
2026-09-23: `legal_budget_used_ratio` returned empty after twelve hours up
with no model call, which is exactly what a gauge does and exactly why a
yearly budget cannot be built on one.

So the daily pass writes the month into a table. The tests below are about
the two ways that record could quietly be worth nothing: a month that stops
being written losing the figure it had, and a meter outage overwriting a real
total with a zero.
"""
import asyncio
from types import SimpleNamespace

import pytest

from core.repositories import budget_history
from core.services import maintenance


class _Cursor:
    def __init__(self, state):
        self.state = state
        self.rowcount = 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self.state.statements.append((" ".join(sql.split()), list(params or [])))

    async def fetchall(self):
        return self.state.rows


class _Conn:
    def __init__(self, rows=None):
        self.statements = []
        self.rows = rows or []
        self.commits = 0

    def cursor(self):
        return _Cursor(self)

    async def commit(self):
        self.commits += 1


# ── the record itself ───────────────────────────────────────────────────────


def test_a_month_is_written_once_and_then_updated_in_place():
    """Daily writes converge on the month's real total. Inserting a new row
    each day would make the table a log nobody can read a month out of."""
    conn = _Conn()

    asyncio.run(budget_history.record_month(
        conn, "2026-09", input_tokens=3422, output_tokens=517, cost_ceiling_usd=0.0138,
    ))

    sql, params = conn.statements[0]
    assert "INSERT INTO budget_history" in sql
    assert "ON CONFLICT (month) DO UPDATE" in sql
    assert params == ["2026-09", 3422, 517, 0.0138]
    assert conn.commits == 1


def test_every_value_column_is_refreshed_on_the_daily_rewrite():
    """The counters grow all month. A rewrite that keeps the first day's
    figures freezes the month near zero and the table lies quietly."""
    conn = _Conn()

    asyncio.run(budget_history.record_month(
        conn, "2026-09", input_tokens=1, output_tokens=1, cost_ceiling_usd=0.1,
    ))

    sql, _params = conn.statements[0]
    update = sql.split("DO UPDATE SET", 1)[1]
    for column in ("input_tokens", "output_tokens", "cost_ceiling_usd"):
        assert f"{column} = EXCLUDED.{column}" in update, column


def test_the_stored_cost_is_the_one_computed_at_the_time():
    """Prices in .env change. A past month must keep the figure that was true
    when it was spent, so the cost is a column and not a view."""
    conn = _Conn()

    asyncio.run(budget_history.record_month(
        conn, "2026-03", input_tokens=1_000_000, output_tokens=200_000,
        cost_ceiling_usd=4.50,
    ))

    sql, params = conn.statements[0]
    assert "cost_ceiling_usd" in sql
    assert params[3] == 4.50


def test_history_comes_back_newest_first_and_bounded():
    conn = _Conn(rows=[("2026-09", 3422, 517, 0.0138), ("2026-08", 900000, 120000, 3.45)])

    months = asyncio.run(budget_history.history(conn, limit=500))

    sql, params = conn.statements[0]
    assert "ORDER BY month DESC" in sql
    assert params == [120], "an unbounded limit is a query nobody meant to run"
    assert [m.month for m in months] == ["2026-09", "2026-08"]
    assert months[0].cost_ceiling_usd == 0.0138


# ── the daily pass ──────────────────────────────────────────────────────────


@pytest.fixture
def pass_state(monkeypatch):
    state = SimpleNamespace(recorded=[], report={
        "day": {"input": 100, "output": 20},
        "month": {"input": 3422, "output": 517},
    })

    class _Pool:
        def connection(self):
            return self

        async def __aenter__(self):
            return "conn"

        async def __aexit__(self, *exc):
            return False

    async def pool():
        return _Pool()

    async def used():
        return state.report

    async def record_month(_conn, month, **kwargs):
        state.recorded.append((month, kwargs))

    monkeypatch.setattr(maintenance, "get_pool", pool)
    monkeypatch.setattr(maintenance.budget, "used", used)
    monkeypatch.setattr(maintenance, "record_month", record_month)
    return state


def test_the_daily_pass_records_the_month_that_is_running(pass_state, monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setattr(
        maintenance, "local_now",
        lambda *_a, **_k: datetime(2026, 9, 23, 3, tzinfo=timezone.utc),
    )

    asyncio.run(maintenance.record_budget_history())

    month, values = pass_state.recorded[0]
    assert month == "2026-09"
    assert values["input_tokens"] == 3422 and values["output_tokens"] == 517
    assert values["cost_ceiling_usd"] > 0


def test_an_unreachable_meter_writes_nothing_rather_than_a_zero(pass_state):
    """budget.used() returns {} when Redis is down. Recording that as zero
    would erase a real total with an outage."""
    pass_state.report = {}

    asyncio.run(maintenance.record_budget_history())

    assert pass_state.recorded == []


def test_recording_failure_does_not_take_the_retention_pass_down(pass_state, monkeypatch):
    """The pass has other work, and tomorrow's run writes the same month."""
    async def blow_up(*_args, **_kwargs):
        raise RuntimeError("database gone")

    monkeypatch.setattr(maintenance, "record_month", blow_up)

    with pytest.raises(RuntimeError):
        asyncio.run(maintenance.record_budget_history())
    # The worker wraps both calls in one try/except, verified below.


def test_the_worker_records_beside_the_retention_pass():
    """One daily wake-up does both, and the worker's except covers them."""
    import inspect

    source = inspect.getsource(maintenance.run_maintenance_worker)
    assert "await run_retention_pass()" in source
    assert "await record_budget_history()" in source
    retention = source.index("await run_retention_pass()")
    record = source.index("await record_budget_history()")
    guard = source.index("except Exception")
    assert retention < record < guard, "both run inside the same failure guard"
