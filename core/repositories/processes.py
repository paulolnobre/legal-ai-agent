"""The cases a person follows, their deadlines, and who was told about them.

A case is private (its creator's alone) or a team's (every current member's).
There is no firm shelf and no owner override: the owner sees no one's private
case, as with private folders. Every read and every write by id goes through
`process_clause`, so an id the reader cannot see behaves as one that does not
exist.

`deadline_alerts` is the alert worker's only memory: a row is written after
the person was told, so a crash in between tells them twice, never zero times.

Also created at startup by `ensure_process_schema` (lesson 17), after the
teams it references.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from psycopg import errors

from core.repositories.teams import Scope

# Returned by `create` and `set_visibility` when the scope already has this
# number open: the partial unique indexes decide, not a check-then-insert.
DUPLICATE = -1



@dataclass(frozen=True)
class Deadline:
    id: int
    process_id: int
    title: str
    due_date: date
    done_at: datetime | None


@dataclass(frozen=True)
class TrackedProcess:
    id: int
    process_number: str
    client: str | None
    note: str | None
    visibility: Literal["private", "team"]
    team_id: int | None
    team_name: str | None
    created_by: int
    deadlines: tuple[Deadline, ...]


async def ensure_process_schema(conn) -> None:
    """Inline, as `tests/test_schema_bootstrap.py` reads this source."""
    async with conn.cursor() as cur:
        for statement in (
        """
        CREATE TABLE IF NOT EXISTS tracked_processes (
            id              SERIAL PRIMARY KEY,
            process_number  VARCHAR(20) NOT NULL,
            client          VARCHAR(160),
            note            VARCHAR(500),
            visibility      TEXT NOT NULL CHECK (visibility IN ('private', 'team')),
            team_id         INTEGER REFERENCES teams(id),
            created_by      INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            archived_at     TIMESTAMPTZ,
            CHECK ((visibility = 'team') = (team_id IS NOT NULL))
        )
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_tracked_private ON tracked_processes
            (created_by, process_number) WHERE visibility = 'private' AND archived_at IS NULL
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_tracked_team ON tracked_processes
            (team_id, process_number) WHERE visibility = 'team' AND archived_at IS NULL
        """,
        """
        CREATE TABLE IF NOT EXISTS process_deadlines (
            id          SERIAL PRIMARY KEY,
            process_id  INTEGER NOT NULL REFERENCES tracked_processes(id) ON DELETE CASCADE,
            title       VARCHAR(120) NOT NULL,
            due_date    DATE NOT NULL,
            created_by  INTEGER REFERENCES staff(id) ON DELETE SET NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            done_at     TIMESTAMPTZ,
            done_by     INTEGER REFERENCES staff(id) ON DELETE SET NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_process_deadlines_open
            ON process_deadlines (due_date) WHERE done_at IS NULL
        """,
        """
        CREATE TABLE IF NOT EXISTS deadline_alerts (
            deadline_id  INTEGER NOT NULL REFERENCES process_deadlines(id) ON DELETE CASCADE,
            staff_id     INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
            threshold    SMALLINT NOT NULL CHECK (threshold IN (10, 5, 0)),
            sent_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (deadline_id, staff_id, threshold)
        )
        """,
        ):
            await cur.execute(statement)
    await conn.commit()


def process_clause(scope: Scope, alias: str = "p") -> tuple[str, list]:
    """SQL keeping only the cases this reader may see, and its params."""
    if scope.everything:
        return "TRUE", []
    if scope.staff_id is None:
        return "FALSE", []
    return (
        f"(({alias}.visibility = 'private' AND {alias}.created_by = %s)"
        f" OR ({alias}.visibility = 'team' AND {alias}.team_id = ANY(%s)))",
        [scope.staff_id, list(scope.team_ids)],
    )


def _team_allowed(scope: Scope, team_id: int | None) -> bool:
    return team_id is None or team_id in scope.team_ids


async def list_open(conn, scope: Scope) -> list[TrackedProcess]:
    """The reader's cases not archived, nearest open deadline first (none last)."""
    clause, params = process_clause(scope)
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT p.id, p.process_number, p.client, p.note, p.visibility, p.team_id,
                   t.name, p.created_by
              FROM tracked_processes p LEFT JOIN teams t ON t.id = p.team_id
             WHERE p.archived_at IS NULL AND {clause}
            """,
            params,
        )
        rows = await cur.fetchall()
        ids = [row[0] for row in rows]
        deadlines: dict[int, list[Deadline]] = {pid: [] for pid in ids}
        if ids:
            # The rule again: the case may have changed hands since the
            # statement above, and a deadline added since must not leak.
            await cur.execute(
                f"""
                SELECT d.id, d.process_id, d.title, d.due_date, d.done_at
                  FROM process_deadlines d JOIN tracked_processes p ON p.id = d.process_id
                 WHERE d.process_id = ANY(%s) AND p.archived_at IS NULL AND {clause}
                 ORDER BY d.due_date, d.id
                """,
                [ids, *params],
            )
            for row in await cur.fetchall():
                deadlines[row[1]].append(Deadline(*row))
    found = [
        TrackedProcess(id=row[0], process_number=row[1], client=row[2], note=row[3],
                       visibility=row[4], team_id=row[5], team_name=row[6],
                       created_by=row[7], deadlines=tuple(deadlines[row[0]]))
        for row in rows
    ]

    def nearest(process: TrackedProcess) -> tuple[date, int]:
        open_dates = [d.due_date for d in process.deadlines if d.done_at is None]
        return (min(open_dates) if open_dates else date.max, process.id)

    return sorted(found, key=nearest)


async def create(conn, scope: Scope, *, process_number: str, client: str | None,
                 note: str | None, team_id: int | None) -> int | None:
    """The new case's id; None when the reader may not put it there;
    DUPLICATE when that scope already has the number open."""
    if scope.staff_id is None or not _team_allowed(scope, team_id):
        return None
    try:
        # A savepoint: a duplicate undoes this insert, not the caller's work.
        async with conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO tracked_processes
                    (process_number, client, note, visibility, team_id, created_by)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
                """,
                (process_number, client, note, "team" if team_id else "private",
                 team_id, scope.staff_id),
            )
            row = await cur.fetchone()
    except errors.UniqueViolation:
        return DUPLICATE
    await conn.commit()
    return row[0] if row else None


async def add_deadline(conn, scope: Scope, process_id: int, *, title: str,
                       due_date: date, staff_id: int) -> int | None:
    """The deadline's id, or None when the case is not the reader's to see."""
    clause, params = process_clause(scope)
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            INSERT INTO process_deadlines (process_id, title, due_date, created_by)
            SELECT p.id, %s, %s, %s FROM tracked_processes p
             WHERE p.id = %s AND p.archived_at IS NULL AND {clause}
            RETURNING id
            """,
            [title, due_date, staff_id, process_id, *params],
        )
        row = await cur.fetchone()
    await conn.commit()
    return row[0] if row else None


async def mark_done(conn, scope: Scope, deadline_id: int, staff_id: int) -> bool:
    clause, params = process_clause(scope)
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            UPDATE process_deadlines d SET done_at = NOW(), done_by = %s
              FROM tracked_processes p
             WHERE d.id = %s AND d.done_at IS NULL AND p.id = d.process_id
               AND p.archived_at IS NULL AND {clause}
            """,
            [staff_id, deadline_id, *params],
        )
        changed = bool(cur.rowcount)
    await conn.commit()
    return changed


async def archive(conn, scope: Scope, process_id: int) -> bool:
    clause, params = process_clause(scope)
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            UPDATE tracked_processes p SET archived_at = NOW()
             WHERE p.id = %s AND p.archived_at IS NULL AND {clause}
            """,
            [process_id, *params],
        )
        changed = bool(cur.rowcount)
    await conn.commit()
    return changed


async def set_visibility(conn, scope: Scope, process_id: int, *,
                         team_id: int | None) -> bool | int:
    """Move a case between individual (None) and one of the reader's teams.
    Its creator's only; DUPLICATE when the destination has the number open."""
    if scope.staff_id is None or not _team_allowed(scope, team_id):
        return False
    clause, params = process_clause(scope)
    try:
        async with conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE tracked_processes p SET visibility = %s, team_id = %s
                 WHERE p.id = %s AND p.created_by = %s AND p.archived_at IS NULL
                   AND {clause}
                """,
                ["team" if team_id else "private", team_id, process_id, scope.staff_id,
                 *params],
            )
            changed = bool(cur.rowcount)
    except errors.UniqueViolation:
        return DUPLICATE
    await conn.commit()
    return changed


@dataclass(frozen=True)
class Candidate:
    """An open deadline due within ten days, and one person to tell."""
    deadline_id: int
    staff_id: int
    chat_id: str | None
    title: str
    due_date: date
    process_number: str
    client: str | None


async def alert_candidates(conn, today: date, *, staff_id: int | None = None,
                           ) -> tuple[list[Candidate], set[tuple[int, int, int]]]:
    """Open deadlines of open cases due from today to ten days on, with each
    person to tell (the creator of a private case, the members of a team's;
    active only), and the (deadline, person, threshold) already told.
    `staff_id` narrows it to one person, read again just before telling them."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT d.id, s.id, s.chat_id, d.title, d.due_date, p.process_number, p.client
              FROM process_deadlines d
              JOIN tracked_processes p ON p.id = d.process_id
              JOIN LATERAL (
                    SELECT p.created_by AS staff_id WHERE p.visibility = 'private'
                    UNION
                    SELECT tm.staff_id FROM team_members tm
                     WHERE p.visibility = 'team' AND tm.team_id = p.team_id
              ) r ON TRUE
              JOIN staff s ON s.id = r.staff_id AND s.active = TRUE
             WHERE d.done_at IS NULL AND p.archived_at IS NULL
               AND d.due_date BETWEEN %s AND %s
               AND (%s::int IS NULL OR s.id = %s::int)
             ORDER BY s.id, d.due_date, d.id
            """,
            (today, today + timedelta(days=10), staff_id, staff_id),
        )
        candidates = [Candidate(row[0], row[1], None if row[2] is None else str(row[2]),
                                *row[3:]) for row in await cur.fetchall()]
        told: set[tuple[int, int, int]] = set()
        ids = sorted({c.deadline_id for c in candidates})
        if ids:
            await cur.execute(
                "SELECT deadline_id, staff_id, threshold FROM deadline_alerts "
                "WHERE deadline_id = ANY(%s)", (ids,))
            told = {(row[0], row[1], row[2]) for row in await cur.fetchall()}
    return candidates, told


async def record_alerts(conn, rows: list[tuple[int, int, int]]) -> None:
    """(deadline, person, threshold) told. Written after the telling."""
    if not rows:
        return
    async with conn.cursor() as cur:
        await cur.executemany(
            "INSERT INTO deadline_alerts (deadline_id, staff_id, threshold) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", rows)
    await conn.commit()


async def purge_alerts(conn, *, keep_days: int) -> None:
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM deadline_alerts WHERE sent_at < NOW() - %s * INTERVAL '1 day'",
                          (keep_days,))
    await conn.commit()


__all__ = [
    "Candidate", "DUPLICATE", "Deadline", "TrackedProcess", "add_deadline", "alert_candidates",
    "archive", "create", "purge_alerts", "record_alerts",
    "ensure_process_schema", "list_open", "mark_done", "process_clause", "set_visibility",
]
