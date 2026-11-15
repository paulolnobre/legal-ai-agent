"""The conversation table, against a real Postgres.

Two of its properties are security properties, so a double would only test
the double: a person reads only their own conversation, and nothing outlives
the retention window. The window the router sees (2 hours, 20 turns, 6,000
characters, bot turns cut to 600) is what keeps every message from growing
expensive. These skip without TEST_DATABASE_URL.
"""
import asyncio
import os
import uuid
from typing import LiteralString

import pytest
from psycopg import sql

from core.repositories import conversation, panel

URL = os.environ.get("TEST_DATABASE_URL")
SCHEMA = f"conversationtest_{uuid.uuid4().hex[:12]}"

_needs_db = pytest.mark.skipif(not URL, reason="needs a real Postgres: set TEST_DATABASE_URL")


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
                await cur.execute("INSERT INTO staff (name) VALUES ('Paulo'), ('Bia')")
            await conn.commit()
            await conversation.ensure_conversation_schema(conn)
            # The history leaves out panel requests not yet started.
            await panel.ensure_panel_schema(conn)
            try:
                return await body(conn)
            finally:
                async with conn.cursor() as cur:
                    await cur.execute(_in_schema("DROP SCHEMA IF EXISTS {schema} CASCADE"))
                await conn.commit()

    return asyncio.run(main())


async def _say(conn, staff_id, speaker, text, *, ago: str | None = None):
    await conversation.record_turn(conn, staff_id=staff_id, chat_id=str(100 + staff_id),
                                   speaker=speaker, text=text)
    if ago:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE conversation_turns SET created_at = NOW() - %s::interval "
                "WHERE id = (SELECT max(id) FROM conversation_turns)", (ago,))
        await conn.commit()


@_needs_db
def test_each_person_reads_only_their_own():
    async def body(conn):
        await _say(conn, 1, "person", "prazo do Paulo")
        await _say(conn, 2, "person", "prazo da Bia")
        return await conversation.recent_turns(conn, 1)

    texts = [turn.text for turn in _run(body)]
    assert texts == ["prazo do Paulo"]


@_needs_db
def test_older_than_two_hours_is_left_out():
    async def body(conn):
        await _say(conn, 1, "person", "de manhã", ago="3 hours")
        await _say(conn, 1, "person", "agora há pouco", ago="1 hour")
        return await conversation.recent_turns(conn, 1)

    assert [turn.text for turn in _run(body)] == ["agora há pouco"]


@_needs_db
def test_the_window_keeps_the_newest_twenty_oldest_first():
    async def body(conn):
        for n in range(25):
            await _say(conn, 1, "person", f"m{n}")
        return await conversation.recent_turns(conn, 1)

    assert [turn.text for turn in _run(body)] == [f"m{n}" for n in range(5, 25)]


@_needs_db
def test_the_window_keeps_the_newest_six_thousand_characters():
    async def body(conn):
        for letter in "abcd":
            await _say(conn, 1, "person", letter * 2500)
        return await conversation.recent_turns(conn, 1)

    assert [turn.text[0] for turn in _run(body)] == ["c", "d"]


@_needs_db
def test_a_long_bot_turn_is_cut_and_a_person_turn_never_is():
    async def body(conn):
        await _say(conn, 1, "person", "p" * 2000)
        await _say(conn, 1, "bot", "b" * 2000)
        return await conversation.recent_turns(conn, 1)

    person, bot = _run(body)
    assert person.speaker == "person" and person.text == "p" * 2000
    assert bot.speaker == "bot" and bot.text == "b" * 600


@_needs_db
def test_purge_removes_only_what_is_older_than_the_window():
    async def body(conn):
        await _say(conn, 1, "person", "velha", ago="91 days")
        await _say(conn, 1, "person", "recente", ago="89 days")
        deleted = await conversation.purge_older_than(conn, 90)
        async with conn.cursor() as cur:
            await cur.execute("SELECT text FROM conversation_turns")
            return deleted, [row[0] for row in await cur.fetchall()]

    assert _run(body) == (1, ["recente"])


@_needs_db
def test_only_person_and_bot_are_speakers():
    import psycopg

    async def body(conn):
        with pytest.raises(psycopg.errors.CheckViolation):
            await _say(conn, 1, "admin", "x")  # type: ignore[arg-type]
        await conn.rollback()

    _run(body)


def test_the_daily_pass_purges_the_conversation_at_the_query_window(monkeypatch):
    """Without this call the table only ever grows."""
    from core.config import settings
    from core.services import maintenance

    class _Pool:
        def connection(self):
            return self

        async def __aenter__(self):
            return "conn"

        async def __aexit__(self, *exc):
            return False

    async def pool():
        return _Pool()

    async def nothing(*_args, **_kwargs):
        return 0

    windows = []

    async def purge(_conn, days):
        windows.append(days)
        return 0

    monkeypatch.setattr(maintenance, "get_pool", pool)
    for name in ("purge_expired_access_log", "purge_expired_steps", "purge_expired_callbacks",
                 "purge_expired_enrolments", "purge_expired_sign_ins", "purge_expired_sessions",
                 "purge_expired_files", "purge_old_requests"):
        monkeypatch.setattr(maintenance, name, nothing)
    monkeypatch.setattr(maintenance, "purge_conversation", purge)

    asyncio.run(maintenance.run_retention_pass())

    assert windows == [settings.query_retention_days]


@_needs_db
def test_a_failed_recording_leaves_the_connection_usable():
    """The handler records on the connection it keeps using for the reply.
    A failed INSERT must not leave it in an aborted transaction."""
    from core.handlers import legal_message

    async def body(conn):
        await conn.execute("SELECT 1")  # the reply's work, already under way
        await legal_message._record(conn, 9999, "100", "person", "pessoa que não existe")
        async with conn.cursor() as cur:
            await cur.execute("SELECT 41 + 1")
            return (await cur.fetchone())[0]

    assert _run(body) == 42


@_needs_db
def test_record_turn_returns_its_id_and_keeps_channel_extras_request():
    request = uuid.uuid4()

    async def body(conn):
        first = await conversation.record_turn(conn, staff_id=1, chat_id="101",
                                               speaker="person", text="oi")
        second = await conversation.record_turn(
            conn, staff_id=1, chat_id="101", speaker="bot", text="olá", channel="panel",
            extras={"buttons": [[{"label": "Ajudou", "token": "t1"}]]}, request_id=request)
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, channel, extras, request_id, updated_at IS NOT NULL "
                "FROM conversation_turns ORDER BY id")
            return first, second, await cur.fetchall()

    first, second, rows = _run(body)

    assert second > first
    assert rows == [
        (first, "telegram", None, None, True),
        (second, "panel", {"buttons": [[{"label": "Ajudou", "token": "t1"}]]}, request, True),
    ]


@_needs_db
def test_new_columns_reach_a_database_created_before_them():
    async def body(conn):
        async with conn.cursor() as cur:
            await cur.execute("ALTER TABLE conversation_turns DROP COLUMN channel, "
                              "DROP COLUMN extras, DROP COLUMN request_id, "
                              "DROP COLUMN updated_at")
        await conn.commit()
        await conversation.ensure_conversation_schema(conn)
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'conversation_turns'")
            return {row[0] for row in await cur.fetchall()}

    assert {"channel", "extras", "request_id", "updated_at"} <= _run(body)


def test_access_links_are_redacted_by_the_shared_rule():
    text = "Abra https://x.adv.br/web/login/abc123 e https://x.adv.br/pje/zz9 agora"

    assert conversation.redact_access_links(text) == (
        "Abra [link de acesso] e [link de acesso] agora")
