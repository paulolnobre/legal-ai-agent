"""The days the firm's courts are closed that no national law says so.

`core/services/deadlines.py` knows the national holidays and nothing else, on
purpose: a state, municipal or court holiday it invented would produce a wrong
deadline that looks right. What it does not know it receives, and this table
is where the firm writes it down: Pará's and Belém's holidays, the TJPA's own
closures, a suspension by portaria announced last week.

One calendar for the whole firm, because the firm works mainly before one
state's courts. A day here is a day without court business for every deadline
the firm counts. If the firm ever litigates seriously elsewhere, the day grows
a column saying where it applies; until then that column would only be a
question every lawyer has to answer for nothing.

A day missing from here is counted as a working day. The calculator says so in
every answer, so nobody reads a computed deadline as a checked one.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class FirmHoliday:
    day: date
    reason: str


async def ensure_firm_holidays_schema(conn) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS firm_holidays (
                day        DATE PRIMARY KEY,
                reason     TEXT NOT NULL,
                added_by   INTEGER REFERENCES staff(id) ON DELETE SET NULL,
                added_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    await conn.commit()


async def add_holiday(conn, day: date, reason: str, *, added_by: int) -> str | None:
    """Record a day without court business, or rename one already recorded.

    Returns the reason it had before, or None when the day is new, so the
    person is told whether they added a day or corrected one.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            WITH previous AS (SELECT reason FROM firm_holidays WHERE day = %s)
            INSERT INTO firm_holidays (day, reason, added_by)
            VALUES (%s, %s, %s)
            ON CONFLICT (day) DO UPDATE SET
                reason = EXCLUDED.reason,
                added_by = EXCLUDED.added_by,
                added_at = NOW()
            RETURNING (SELECT reason FROM previous)
            """,
            (day, day, reason, added_by),
        )
        row = await cur.fetchone()
    await conn.commit()
    return row[0] if row else None


async def remove_holiday(conn, day: date) -> str | None:
    """Forget a day. Returns its reason, or None when it was not there."""
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM firm_holidays WHERE day = %s RETURNING reason", (day,),
        )
        row = await cur.fetchone()
    await conn.commit()
    return row[0] if row else None


async def holidays_between(conn, start: date, end: date) -> list[FirmHoliday]:
    """The firm's days without court business in [start, end], oldest first."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT day, reason FROM firm_holidays WHERE day BETWEEN %s AND %s "
            "ORDER BY day",
            (start, end),
        )
        rows = await cur.fetchall()
    return [FirmHoliday(day=row[0], reason=row[1]) for row in rows]


__all__ = [
    "FirmHoliday", "add_holiday", "ensure_firm_holidays_schema",
    "holidays_between", "remove_holiday",
]
