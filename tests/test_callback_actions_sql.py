"""Retiring the other copies of a decision, against a real Postgres.

An access request is offered to every manager, each with their own buttons.
When one of them decides, `cancel_siblings` retires every other copy. It was
only ever tested through an in-memory double, and on Postgres the optional
filters (`%s IS NULL` with no chat id) could not be typed: the decision went
through and the press then failed (found 2026-10-09, recording the tutorial).
"""
import asyncio
import os
import uuid
from typing import LiteralString

import pytest
from psycopg import sql

from core.repositories import callback_actions

URL = os.environ.get("TEST_DATABASE_URL")
SCHEMA = f"callbacktest_{uuid.uuid4().hex[:12]}"

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
                await cur.execute("INSERT INTO staff (name) VALUES ('Marina'), ('Paulo')")
            await conn.commit()
            await callback_actions.ensure_callback_actions_schema(conn)
            try:
                return await body(conn)
            finally:
                async with conn.cursor() as cur:
                    await cur.execute(_in_schema("DROP SCHEMA IF EXISTS {schema} CASCADE"))
                await conn.commit()

    return asyncio.run(main())


async def _offer(conn, chat_id, staff_id, message_id, channel="telegram"):
    token = await callback_actions.issue(
        conn, chat_id=chat_id, staff_id=staff_id, issued_role="admin",
        action="access.review", ttl_seconds=600, resource="access_request:7",
        channel=channel)
    await callback_actions.attach_reference(conn, [token], reference=str(message_id))
    await conn.commit()
    return token


def test_a_decision_retires_the_copies_in_every_other_managers_chat():
    async def body(conn):
        pressed = await _offer(conn, "1003", 1, 501)
        await _offer(conn, "1000", 2, 777)
        retired = await callback_actions.cancel_siblings(
            conn, resource="access_request:7", keep_token=pressed)
        async with conn.cursor() as cur:
            await cur.execute("SELECT token, state FROM callback_actions ORDER BY chat_id")
            states = await cur.fetchall()
        return retired, pressed, states

    retired, pressed, states = _run(body)
    assert retired == [("1000", "telegram", "777")]
    assert dict((token, state) for token, state in states)[pressed] == "pending"
    assert sorted(state for _, state in states) == ["cancelled", "pending"]


def test_retiring_within_one_chat_leaves_the_others():
    async def body(conn):
        await _offer(conn, "1003", 1, 501)
        await _offer(conn, "1000", 2, 777)
        return await callback_actions.cancel_siblings(
            conn, resource="access_request:7", chat_id="1003")

    assert _run(body) == [("1003", "telegram", "501")]


def test_retiring_with_no_filter_retires_every_copy():
    async def body(conn):
        await _offer(conn, "1003", 1, 501)
        await _offer(conn, "1000", 2, 777)
        return await callback_actions.cancel_siblings(conn, resource="access_request:7")

    assert sorted(_run(body)) == [("1000", "telegram", "777"), ("1003", "telegram", "501")]


def test_a_token_issued_on_one_channel_cannot_be_claimed_from_the_other():
    async def body(conn):
        token = await _offer(conn, "1003", 1, 88, channel="panel")
        wrong = await callback_actions.claim(conn, token, chat_id="1003", staff_id=1,
                                             channel="telegram")
        right = await callback_actions.claim(conn, token, chat_id="1003", staff_id=1,
                                             channel="panel")
        return wrong, right

    wrong, right = _run(body)

    assert wrong.outcome == "other_channel" and not wrong.may_run
    assert right.outcome == "granted", "the refused press must not have spent the token"


def test_retired_siblings_come_back_with_their_channel_and_reference():
    async def body(conn):
        pressed = await _offer(conn, "1003", 1, 501)
        await _offer(conn, "1000", 2, 88, channel="panel")
        return await callback_actions.cancel_siblings(
            conn, resource="access_request:7", keep_token=pressed)

    assert _run(body) == [("1000", "panel", "88")]


def test_buttons_issued_before_the_migration_are_still_retired():
    """A button already on a manager's screen when this deploys has only its
    Telegram message id; the bootstrap carries it over as the reference."""
    async def body(conn):
        async with conn.cursor() as cur:
            await cur.execute(
                "ALTER TABLE callback_actions DROP COLUMN reference, DROP COLUMN channel")
            await cur.execute(
                "INSERT INTO callback_actions (token, chat_id, staff_id, issued_role, "
                "action, resource, message_id, expires_at) VALUES ('old', '1000', 2, "
                "'admin', 'access.review', 'access_request:7', 777, "
                "NOW() + interval '1 hour')")
        await conn.commit()
        await callback_actions.ensure_callback_actions_schema(conn)
        return await callback_actions.cancel_siblings(conn, resource="access_request:7")

    assert _run(body) == [("1000", "telegram", "777")]


def test_a_press_whose_sql_failed_still_records_that_it_failed():
    """The failed statement aborts the transaction; the failure result is
    written after a rollback, or the spent button would answer "already
    decided" for an action that never happened."""
    import psycopg

    from core.handlers import legal_message
    from core.models import StaffMember

    admin = StaffMember(id=1, chat_id="1003", name="Marina", role="admin")
    answered: list[str | None] = []

    async def answer(text=None):
        answered.append(text)

    async def broken(conn, *_args, **_kwargs):
        async with conn.cursor() as cur:
            await cur.execute("SELECT * FROM no_such_table")

    async def body(conn):
        token = await _offer(conn, "1003", 1, 501)
        original = legal_message._decide_access
        legal_message._decide_access = broken
        try:
            with pytest.raises(psycopg.errors.UndefinedTable):
                await legal_message._run_callback(
                    conn, {"text": "Solicitação"}, "1003", 501, admin, token, answer)
        finally:
            legal_message._decide_access = original
        async with conn.cursor() as cur:
            await cur.execute("SELECT state, result FROM callback_actions WHERE token = %s",
                              (token,))
            return await cur.fetchone()

    state, result = _run(body)

    assert result == legal_message._PRESS_FAILED
    assert state != "pending"


def test_a_failure_does_not_overwrite_a_result_already_recorded():
    """A press cut off after its decision committed keeps that decision's
    result; only a press that recorded nothing is marked as failed."""
    async def body(conn):
        decided = await _offer(conn, "1003", 1, 501)
        silent = await _offer(conn, "1000", 2, 777)
        await callback_actions.record_result(conn, decided, "Autorizado.")
        await callback_actions.record_result(conn, decided, "falhou", unless_recorded=True)
        await callback_actions.record_result(conn, silent, "falhou", unless_recorded=True)
        async with conn.cursor() as cur:
            await cur.execute("SELECT token, result FROM callback_actions")
            return decided, silent, dict(await cur.fetchall())

    decided, silent, results = _run(body)

    assert results[decided] == "Autorizado."
    assert results[silent] == "falhou"
