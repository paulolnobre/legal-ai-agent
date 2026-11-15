"""What each month cost, kept where nothing expires it.

The budget meter lives in two places and neither of them remembers. The Redis
counters carry a 40-day expiry, so a month's total is gone six weeks after it
closed. The Prometheus gauges live in the application's memory, so a deploy
resets them, and they were measured empty in production on 2026-09-23 after
twelve hours with no model call: not a fault, just what a gauge is.

That is fine for "am I about to hit the ceiling", which is what both were
built for. It is useless for "what did this cost in March", which is the
question a yearly budget, a renegotiation and an invoice all ask.

So the daily maintenance pass writes the current month's totals here, and the
row for a month that has closed simply stops being updated: the last write of
that month is its final figure. Upserting daily rather than writing once on
the first of the month means a missed day, a restart or a late deploy costs
nothing, and the table is always at most a day behind.

The cost is stored as it was computed, not recomputed on read, because the
prices in `.env` change and a past month must keep the figure that was true
when it was spent.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MonthlySpend:
    """One month of the pilot budget, as the meter last read it."""

    month: str
    input_tokens: int
    output_tokens: int
    cost_ceiling_usd: float


async def ensure_budget_history_schema(conn) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS budget_history (
                month             CHAR(7) PRIMARY KEY,
                input_tokens      BIGINT NOT NULL,
                output_tokens     BIGINT NOT NULL,
                cost_ceiling_usd  NUMERIC(10, 4) NOT NULL,
                recorded_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    await conn.commit()


async def record_month(conn, month: str, *, input_tokens: int, output_tokens: int,
                       cost_ceiling_usd: float, commit: bool = True) -> None:
    """Write where this month stands, replacing what was written before.

    The counters only ever grow inside a window, so the newest write of a
    month is also its largest: a row updated daily converges on the month's
    real total and freezes there when the month rolls over and nothing writes
    it again.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO budget_history
                (month, input_tokens, output_tokens, cost_ceiling_usd, recorded_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (month) DO UPDATE SET
                input_tokens = EXCLUDED.input_tokens,
                output_tokens = EXCLUDED.output_tokens,
                cost_ceiling_usd = EXCLUDED.cost_ceiling_usd,
                recorded_at = NOW()
            """,
            (month, input_tokens, output_tokens, cost_ceiling_usd),
        )
    if commit:
        await conn.commit()


async def history(conn, *, limit: int = 12) -> list[MonthlySpend]:
    """The most recent months, newest first."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT month, input_tokens, output_tokens, cost_ceiling_usd
            FROM budget_history ORDER BY month DESC LIMIT %s
            """,
            (max(1, min(int(limit), 120)),),
        )
        rows = await cur.fetchall()
    return [
        MonthlySpend(month=row[0], input_tokens=int(row[1]), output_tokens=int(row[2]),
                     cost_ceiling_usd=float(row[3]))
        for row in rows
    ]


__all__ = ["MonthlySpend", "ensure_budget_history_schema", "history", "record_month"]
