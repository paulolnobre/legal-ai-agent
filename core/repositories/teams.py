"""Who may see which documents: the personal folder, the team's, the firm's.

Until now the archive was one shelf: anybody authorized saw everything on it.
That is right for the law and wrong for the rest. A contract somebody uploads
about their own client is not the firm's to search from another lawyer's
question, and the answer that quotes it would carry the client's material into
a conversation it never belonged in.

Three visibilities, decided by the firm on 2026-09-22:

- **private**: the person who put it there, and nobody else. The default for
  anything a person sends, because the safe default is the narrow one.
- **team**: a group of people working a matter together.
- **firm**: what the office knows rather than what a client gave it. Laws,
  súmulas, teses, model pleadings. This is the knowledge that must circulate.

The vector index stays single, and every chunk carries its document's
visibility into the search: one shelf, three sets of keys. Splitting the index
instead would mean the firm's own law library could not answer a question that
also touches a private document, and would put the rule in two places.
"""
from __future__ import annotations

from dataclasses import dataclass

VISIBILITIES = ("private", "team", "firm")


@dataclass(frozen=True)
class Scope:
    """What one reader may see. Built once per request, passed into the reads."""

    staff_id: int | None = None
    team_ids: tuple[int, ...] = ()
    # Maintenance paths (the CLI, the weekly law refresh) act on a document by
    # id, on the operator's behalf, and are not a person browsing the archive.
    everything: bool = False

    @classmethod
    def unrestricted(cls) -> "Scope":
        return cls(everything=True)

    @property
    def firm_only(self) -> bool:
        """A reader with no identity sees only what the whole firm sees."""
        return not self.everything and self.staff_id is None


def visibility_clause(scope: Scope, alias: str = "d") -> tuple[str, list]:
    """SQL that keeps only the documents this reader may see, and its params.

    Returned as a fragment rather than applied here, because the callers build
    their own statements and a filter bolted on afterwards is the kind that one
    query quietly forgets.
    """
    if scope.everything:
        return "TRUE", []
    if scope.staff_id is None:
        return f"{alias}.visibility = 'firm'", []
    return (
        f"({alias}.visibility = 'firm'"
        f" OR ({alias}.visibility = 'private' AND {alias}.uploaded_by = %s)"
        f" OR ({alias}.visibility = 'team' AND {alias}.team_id = ANY(%s)))",
        [scope.staff_id, list(scope.team_ids)],
    )


async def ensure_team_schema(conn) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS teams (
                id          SERIAL PRIMARY KEY,
                name        VARCHAR(80) UNIQUE NOT NULL,
                created_by  INTEGER REFERENCES staff(id),
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS team_members (
                team_id    INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                staff_id   INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
                added_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (team_id, staff_id)
            )
            """
        )
        # The archive as it stood belongs to the firm: it is laws and material
        # everybody already saw. Only what arrives from now on is private.
        await cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS visibility TEXT")
        await cur.execute("UPDATE documents SET visibility = 'firm' WHERE visibility IS NULL")
        await cur.execute("ALTER TABLE documents ALTER COLUMN visibility SET DEFAULT 'private'")
        await cur.execute("ALTER TABLE documents ALTER COLUMN visibility SET NOT NULL")
        # Named, so widening it later is one statement and not a guess.
        await cur.execute(
            """
            DO $$ BEGIN
                ALTER TABLE documents ADD CONSTRAINT documents_visibility_check
                    CHECK (visibility IN ('private', 'team', 'firm'));
            EXCEPTION WHEN duplicate_object THEN NULL; END $$
            """
        )
        await cur.execute(
            "ALTER TABLE documents ADD COLUMN IF NOT EXISTS team_id INTEGER REFERENCES teams(id)"
        )
        await cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_visibility ON documents(visibility)"
        )
    await conn.commit()


async def create_team(conn, name: str, *, created_by: int | None = None) -> int | None:
    """Create a team. Returns its id, or None when the name is taken."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO teams (name, created_by) VALUES (%s, %s)
            ON CONFLICT (name) DO NOTHING RETURNING id
            """,
            (name.strip(), created_by),
        )
        row = await cur.fetchone()
    await conn.commit()
    return row[0] if row else None


async def find_team(conn, name: str) -> tuple[int, str] | None:
    """A team by name, case-insensitively: people type it as they say it."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, name FROM teams WHERE lower(name) = lower(%s)", (name.strip(),)
        )
        row = await cur.fetchone()
    return (row[0], row[1]) if row else None


async def add_member(conn, team_id: int, staff_id: int) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO team_members (team_id, staff_id) VALUES (%s, %s)
            ON CONFLICT DO NOTHING
            """,
            (team_id, staff_id),
        )
        added = bool(cur.rowcount)
    await conn.commit()
    return added


async def remove_member(conn, team_id: int, staff_id: int) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM team_members WHERE team_id = %s AND staff_id = %s",
            (team_id, staff_id),
        )
        removed = bool(cur.rowcount)
    await conn.commit()
    return removed


async def list_teams(conn) -> list[tuple[int, str, int]]:
    """Every team, with how many people are in it."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT t.id, t.name, count(m.staff_id)
            FROM teams t LEFT JOIN team_members m ON m.team_id = t.id
            GROUP BY t.id, t.name ORDER BY t.name
            """
        )
        return [(row[0], row[1], row[2]) for row in await cur.fetchall()]


async def teams_of(conn, staff_id: int) -> list[tuple[int, str]]:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT t.id, t.name FROM teams t
            JOIN team_members m ON m.team_id = t.id
            WHERE m.staff_id = %s ORDER BY t.name
            """,
            (staff_id,),
        )
        return [(row[0], row[1]) for row in await cur.fetchall()]


async def members_of(conn, team_id: int) -> list[int]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT staff_id FROM team_members WHERE team_id = %s ORDER BY staff_id",
            (team_id,),
        )
        return [row[0] for row in await cur.fetchall()]


async def scope_for(conn, staff_id: int | None) -> Scope:
    """What this person may see right now, teams included."""
    if staff_id is None:
        return Scope()
    teams = await teams_of(conn, staff_id)
    return Scope(staff_id=staff_id, team_ids=tuple(team_id for team_id, _ in teams))


__all__ = [
    "Scope", "VISIBILITIES", "add_member", "create_team", "ensure_team_schema",
    "find_team", "list_teams", "members_of", "remove_member", "scope_for",
    "teams_of", "visibility_clause",
]
