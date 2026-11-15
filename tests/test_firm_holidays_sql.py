"""The firm's calendar, against a real Postgres.

The answer to "did I add a day or correct one" comes from a CTE read in the
same statement as the upsert; a double would only test the double. So these
run against a database when one is given (see tests/test_web_access_sql.py
for how to start one) and skip otherwise.
"""
import asyncio
import os
import uuid
from datetime import date
from typing import LiteralString

import pytest
from psycopg import sql

from core.repositories import firm_holidays

URL = os.environ.get("TEST_DATABASE_URL")
# Unique per test process, so two runs against one database cannot drop each
# other's schema mid-test.
SCHEMA = f"holidaytest_{uuid.uuid4().hex[:12]}"

pytestmark = pytest.mark.skipif(not URL, reason="needs a real Postgres: set TEST_DATABASE_URL")


def _in_schema(statement: LiteralString) -> sql.Composed:
    return sql.SQL(statement).format(schema=sql.Identifier(SCHEMA))


def _run(body):
    import psycopg

    async def main():
        assert URL
        async with await psycopg.AsyncConnection.connect(URL) as conn:
            async with conn.cursor() as cur:
                await cur.execute(_in_schema("DROP SCHEMA IF EXISTS {schema} CASCADE"))
                await cur.execute(_in_schema("CREATE SCHEMA {schema}"))
                await cur.execute(_in_schema("SET search_path TO {schema}"))
                await cur.execute("CREATE TABLE staff (id SERIAL PRIMARY KEY, name TEXT)")
                await cur.execute("INSERT INTO staff (name) VALUES ('Paulo')")
            await conn.commit()
            await firm_holidays.ensure_firm_holidays_schema(conn)
            try:
                return await body(conn)
            finally:
                async with conn.cursor() as cur:
                    await cur.execute(_in_schema("DROP SCHEMA IF EXISTS {schema} CASCADE"))
                await conn.commit()

    return asyncio.run(main())


def test_adding_a_new_day_says_it_is_new():
    async def body(conn):
        return await firm_holidays.add_holiday(
            conn, date(2026, 8, 15), "Adesão do Pará", added_by=1,
        )

    assert _run(body) is None


def test_adding_a_day_again_corrects_it_and_returns_what_it_was():
    async def body(conn):
        await firm_holidays.add_holiday(conn, date(2026, 8, 15), "Adesao", added_by=1)
        previous = await firm_holidays.add_holiday(
            conn, date(2026, 8, 15), "Adesão do Pará", added_by=1,
        )
        days = await firm_holidays.holidays_between(conn, date(2026, 1, 1), date(2026, 12, 31))
        return previous, days

    previous, days = _run(body)

    assert previous == "Adesao"
    assert [(d.day, d.reason) for d in days] == [(date(2026, 8, 15), "Adesão do Pará")]


def test_removing_returns_the_reason_once():
    async def body(conn):
        await firm_holidays.add_holiday(conn, date(2026, 12, 8), "N. Sra. da Conceição",
                                        added_by=1)
        first = await firm_holidays.remove_holiday(conn, date(2026, 12, 8))
        second = await firm_holidays.remove_holiday(conn, date(2026, 12, 8))
        return first, second

    assert _run(body) == ("N. Sra. da Conceição", None)


def test_the_range_is_inclusive_and_ordered():
    async def body(conn):
        for day, reason in ((date(2026, 12, 8), "c"), (date(2026, 1, 12), "a"),
                            (date(2026, 8, 15), "b"), (date(2027, 1, 12), "d")):
            await firm_holidays.add_holiday(conn, day, reason, added_by=1)
        return await firm_holidays.holidays_between(conn, date(2026, 1, 12), date(2026, 12, 8))

    days = _run(body)

    assert [d.reason for d in days] == ["a", "b", "c"]


def test_the_open_ended_read_used_by_prazo_reaches_far_dates():
    """/prazo asks for everything from the start date on, up to date.max."""
    async def body(conn):
        await firm_holidays.add_holiday(conn, date(2030, 1, 2), "distante", added_by=1)
        return await firm_holidays.holidays_between(conn, date(2026, 1, 1), date.max)

    assert [d.reason for d in _run(body)] == ["distante"]


def test_a_removed_person_does_not_take_the_calendar_with_them():
    async def body(conn):
        await firm_holidays.add_holiday(conn, date(2026, 8, 15), "Adesão do Pará", added_by=1)
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM staff WHERE id = 1")
        await conn.commit()
        return await firm_holidays.holidays_between(conn, date(2026, 1, 1), date(2026, 12, 31))

    assert [d.reason for d in _run(body)] == ["Adesão do Pará"]
