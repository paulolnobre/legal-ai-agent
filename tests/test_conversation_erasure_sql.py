"""Erasing a person (LGPD) deletes their conversation, and only theirs.

On the real schema (init.sql plus every startup bootstrap), because the
property is which rows a DELETE reaches: a statement-recording double would
pass with the actor's id where the target's belongs, erasing the wrong
person's conversation and keeping the one that was asked for.

Skipped without TEST_DATABASE_URL; CI provides one.
"""
import asyncio
import os
import uuid
from pathlib import Path
from typing import LiteralString, cast

import pytest
from psycopg import sql

from core.repositories import conversation
from core.repositories.staff import erase_person

URL = os.environ.get("TEST_DATABASE_URL")
SCHEMA = f"erasetest_{uuid.uuid4().hex[:12]}"

pytestmark = pytest.mark.skipif(not URL, reason="needs a real Postgres: set TEST_DATABASE_URL")


def _in_schema(statement: LiteralString) -> sql.Composed:
    return sql.SQL(statement).format(schema=sql.Identifier(SCHEMA))


async def _vector_in_public(conn) -> None:
    """As in tests/test_web_files.py: the extension belongs to the database."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT pg_advisory_lock(hashtext('test-vector-extension'))")
        try:
            await cur.execute(
                "SELECT n.nspname FROM pg_extension e "
                "JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'vector'")
            row = await cur.fetchone()
            if row is None:
                await cur.execute("CREATE EXTENSION vector SCHEMA public")
            elif row[0] != "public":
                await cur.execute("ALTER EXTENSION vector SET SCHEMA public")
            await conn.commit()
        finally:
            await cur.execute("SELECT pg_advisory_unlock(hashtext('test-vector-extension'))")
            await conn.commit()


def test_erasing_a_person_deletes_their_conversation_and_keeps_everyone_elses():
    import psycopg

    from tests.test_schema_bootstrap import _BOOTSTRAPS

    async def main():
        assert URL
        async with await psycopg.AsyncConnection.connect(URL) as conn:
            await _vector_in_public(conn)
            async with conn.cursor() as cur:
                await cur.execute(_in_schema("DROP SCHEMA IF EXISTS {schema} CASCADE"))
                await cur.execute(_in_schema("CREATE SCHEMA {schema}"))
                await cur.execute(_in_schema("SET search_path TO {schema}, public"))
                await cur.execute(cast(LiteralString,
                                       Path("infra/init.sql").read_text(encoding="utf-8")))
            await conn.commit()
            try:
                for bootstrap in _BOOTSTRAPS:
                    await bootstrap(conn)
                async with conn.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO staff (id, name, role, active, chat_id) VALUES "
                        "(1, 'Paulo', 'owner', TRUE, '100'), "
                        "(2, 'Rui', 'advogado', TRUE, '200'), "
                        "(3, 'Bia', 'estagiario', TRUE, '300')")
                await conn.commit()
                for staff_id in (1, 2, 3):
                    await conversation.record_turn(
                        conn, staff_id=staff_id, chat_id=str(staff_id * 100),
                        speaker="person", text=f"mensagem de {staff_id}")

                await erase_person(conn, actor_id=1, target_id=2)

                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT staff_id FROM conversation_turns ORDER BY staff_id")
                    return [row[0] for row in await cur.fetchall()]
            finally:
                async with conn.cursor() as cur:
                    await cur.execute(_in_schema("DROP SCHEMA IF EXISTS {schema} CASCADE"))
                await conn.commit()

    assert asyncio.run(main()) == [1, 3]
