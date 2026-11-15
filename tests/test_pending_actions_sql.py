"""A pending step that holds a partly filled action, against a real Postgres.

"São dias úteis ou corridos?" leaves a deadline half filled: the date and the
day count are known, the regime is not. The answer has to find them again,
after a restart too, so they live in the step's row. And the new kind has to
reach a database whose table was created before it existed, which only the
constraint installed by name on startup does.
"""
import asyncio
import os
import uuid
from typing import LiteralString

import pytest
from psycopg import sql

from core.repositories import pending_actions

URL = os.environ.get("TEST_DATABASE_URL")
SCHEMA = f"pendingtest_{uuid.uuid4().hex[:12]}"

pytestmark = pytest.mark.skipif(not URL, reason="needs a real Postgres: set TEST_DATABASE_URL")


def _in_schema(statement: LiteralString) -> sql.Composed:
    return sql.SQL(statement).format(schema=sql.Identifier(SCHEMA))


def _run(body, *, before=None):
    import psycopg

    async def main():
        assert URL
        async with await psycopg.AsyncConnection.connect(URL) as conn:
            async with conn.cursor() as cur:
                await cur.execute(_in_schema("DROP SCHEMA IF EXISTS {schema} CASCADE"))
                await cur.execute(_in_schema("CREATE SCHEMA {schema}"))
                await cur.execute(_in_schema("SET search_path TO {schema}"))
                if before:
                    await cur.execute(before)
            await conn.commit()
            await pending_actions.ensure_pending_actions_schema(conn)
            try:
                return await body(conn)
            finally:
                async with conn.cursor() as cur:
                    await cur.execute(_in_schema("DROP SCHEMA IF EXISTS {schema} CASCADE"))
                await conn.commit()

    return asyncio.run(main())


def test_an_action_step_keeps_its_arguments():
    async def body(conn):
        await pending_actions.remember(
            conn, 100, kind="action", action="deadline.calculate",
            args={"start": "2026-10-05", "days": 15}, ttl_seconds=60,
        )
        return await pending_actions.peek(conn, 100), await pending_actions.consume(conn, 100)

    peeked, consumed = _run(body)
    assert peeked is not None and peeked.kind == "action"
    assert peeked.action == "deadline.calculate"
    assert peeked.args == {"start": "2026-10-05", "days": 15}
    assert consumed is not None and consumed.args == {"start": "2026-10-05", "days": 15}


def test_a_step_without_arguments_reads_none():
    async def body(conn):
        await pending_actions.remember(conn, 100, kind="identifier", action="details",
                                       ttl_seconds=60)
        return await pending_actions.peek(conn, 100)

    step = _run(body)
    assert step is not None and step.args is None


def test_a_database_with_the_old_table_accepts_the_new_kind_and_column():
    """The table as it was before this change: no args, three kinds."""
    old = """
        CREATE TABLE pending_actions (
            chat_id VARCHAR(50) PRIMARY KEY, kind VARCHAR(16) NOT NULL,
            action VARCHAR(32), approved BOOLEAN, target_id INTEGER,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT pending_actions_kind_allowed
                CHECK (kind IN ('identifier', 'decision', 'export')))
    """

    async def body(conn):
        await pending_actions.remember(conn, 100, kind="action", action="holidays.list",
                                       args={}, ttl_seconds=60)
        return await pending_actions.peek(conn, 100)

    step = _run(body, before=old)
    assert step is not None and step.kind == "action"


def test_a_new_step_replaces_the_arguments_of_the_old_one():
    """One step per chat: answering "úteis" refills the same row, and the
    arguments it carries must be the new ones, not the first question's."""
    async def body(conn):
        await pending_actions.remember(conn, 100, kind="action", action="deadline.calculate",
                                       args={"days": 15}, ttl_seconds=60)
        await pending_actions.remember(conn, 100, kind="action", action="deadline.calculate",
                                       args={"days": 15, "regime": "úteis"}, ttl_seconds=60)
        return await pending_actions.peek(conn, 100)

    step = _run(body)
    assert step is not None and step.args == {"days": 15, "regime": "úteis"}
