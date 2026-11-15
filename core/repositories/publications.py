"""What each person follows in the DJEN, and what they were already told.

Two ways to follow. A lawyer's OAB, which the DJEN answers across every court,
covers every case they are counsel of record in, including the ones nobody
thought to register. A process number covers a case the firm is not in yet.

What was sent is kept per person and per publication, because the same
intimation can reach two people by two routes (one follows the OAB, the other
the process) and each must be told once, not once between them.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

# The 26 states and the Distrito Federal: the only places an OAB section is.
OAB_UFS = frozenset({
    "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS", "MT",
    "PA", "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC", "SE", "SP", "TO",
})


@dataclass(frozen=True)
class Watch:
    """One thing one person follows, and since when."""

    staff_id: int
    chat_id: str
    kind: Literal["oab", "process"]
    # "12345/PA" for an OAB, the 20 digits for a process.
    key: str
    since: datetime


async def ensure_publication_schema(conn) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS watched_oabs (
                staff_id    INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
                oab_number  VARCHAR(10) NOT NULL,
                oab_uf      CHAR(2) NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (staff_id, oab_number, oab_uf)
            )
            """
        )
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS watched_processes (
                staff_id        INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
                process_number  VARCHAR(20) NOT NULL,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (staff_id, process_number)
            )
            """
        )
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS publication_notices (
                publication_id  BIGINT NOT NULL,
                staff_id        INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
                sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (publication_id, staff_id)
            )
            """
        )
    await conn.commit()


async def watch_oab(conn, staff_id: int, number: str, uf: str) -> bool:
    """Follow an OAB. False when this person already follows it."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO watched_oabs (staff_id, oab_number, oab_uf) VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (staff_id, number, uf),
        )
        added = bool(cur.rowcount)
    await conn.commit()
    return added


async def unwatch_oab(conn, staff_id: int, number: str, uf: str) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM watched_oabs WHERE staff_id = %s AND oab_number = %s AND oab_uf = %s",
            (staff_id, number, uf),
        )
        removed = bool(cur.rowcount)
    await conn.commit()
    return removed


async def watch_process(conn, staff_id: int, process_number: str) -> bool:
    """Follow a process. False when this person already follows it."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO watched_processes (staff_id, process_number) VALUES (%s, %s)
            ON CONFLICT DO NOTHING
            """,
            (staff_id, process_number),
        )
        added = bool(cur.rowcount)
    await conn.commit()
    return added


async def unwatch_process(conn, staff_id: int, process_number: str) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM watched_processes WHERE staff_id = %s AND process_number = %s",
            (staff_id, process_number),
        )
        removed = bool(cur.rowcount)
    await conn.commit()
    return removed


async def list_own_watches(conn, staff_id: int) -> tuple[list[str], list[str]]:
    """What one person follows: OABs as "12345/PA", and process numbers."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT oab_number || '/' || oab_uf FROM watched_oabs
            WHERE staff_id = %s ORDER BY oab_uf, oab_number
            """,
            (staff_id,),
        )
        oabs = [row[0] for row in await cur.fetchall()]
        await cur.execute(
            """
            SELECT process_number FROM watched_processes
            WHERE staff_id = %s ORDER BY created_at
            """,
            (staff_id,),
        )
        processes = [row[0] for row in await cur.fetchall()]
    return oabs, processes


async def list_active_watches(conn) -> list[Watch]:
    """Everything followed by somebody who can still be told.

    A deactivated account, or one with no chat, is left out: an intimation
    sent to nobody is not a notice, and it would be recorded as one.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT w.staff_id, s.chat_id, 'oab', w.oab_number || '/' || w.oab_uf, w.created_at
            FROM watched_oabs w JOIN staff s ON s.id = w.staff_id
            WHERE s.active = TRUE AND s.chat_id IS NOT NULL
            UNION ALL
            SELECT w.staff_id, s.chat_id, 'process', w.process_number, w.created_at
            FROM watched_processes w JOIN staff s ON s.id = w.staff_id
            WHERE s.active = TRUE AND s.chat_id IS NOT NULL
            """
        )
        rows = await cur.fetchall()
    return [
        Watch(staff_id=row[0], chat_id=str(row[1]), kind=row[2], key=row[3], since=row[4])
        for row in rows
    ]


async def noticed(conn, publication_ids: list[int]) -> set[tuple[int, int]]:
    """The (publication, person) pairs already told, among these publications."""
    if not publication_ids:
        return set()
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT publication_id, staff_id FROM publication_notices
            WHERE publication_id = ANY(%s)
            """,
            (publication_ids,),
        )
        return {(row[0], row[1]) for row in await cur.fetchall()}


async def record_notices(conn, staff_id: int, publication_ids: list[int]) -> None:
    """Remember that one person was told about these publications."""
    if not publication_ids:
        return
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO publication_notices (publication_id, staff_id)
            SELECT unnest(%s::bigint[]), %s
            ON CONFLICT DO NOTHING
            """,
            (publication_ids, staff_id),
        )
    await conn.commit()


async def purge_old_notices(conn, *, keep_days: int) -> int:
    """Forget notices older than any window a query could still return."""
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM publication_notices WHERE sent_at < NOW() - make_interval(days => %s)",
            (keep_days,),
        )
        removed = cur.rowcount or 0
    await conn.commit()
    return removed
