"""The one step a chat is waiting on, kept where a restart cannot lose it.

These rows are conversation state, not legal content: which question is open,
and what answers it. They never hold a message, a document or a name.

The table has one row per chat by primary key, which is the invariant the
handler used to maintain by hand — starting a flow had to remember to clear the
other one, and forgetting meant two live questions both claiming the next
message. Here, starting a flow replaces whatever was open.
"""
from dataclasses import dataclass
from typing import Literal

from psycopg.types.json import Jsonb

from core.repositories import panel

PendingKind = Literal["identifier", "decision", "export", "action"]


@dataclass(frozen=True)
class PendingAction:
    """A step awaiting the person's next message."""

    kind: PendingKind
    action: str | None = None
    approved: bool | None = None
    target_id: int | None = None
    seconds_left: float = 0.0
    # A partly filled action from conversation: the arguments known so far.
    args: dict | None = None
    # The window that asked: a destructive confirmation is consumed only from it.
    channel: str = "telegram"

    @property
    def expired(self) -> bool:
        return self.seconds_left <= 0


def _row_to_action(row) -> PendingAction:
    kind, action, approved, target_id, seconds_left, args, channel = row
    return PendingAction(
        kind=kind,
        action=action,
        approved=approved,
        target_id=target_id,
        seconds_left=float(seconds_left),
        args=args,
        channel=channel,
    )


async def ensure_pending_actions_schema(conn) -> None:
    """Create the table on a database that already exists.

    ``infra/init.sql`` only runs when PostgreSQL initializes an empty data
    directory, so it reaches a fresh install and never an existing one. Every
    table added after the first deploy needs this path too, or the first query
    against it fails on a database that has simply been running for a while.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_actions (
                chat_id VARCHAR(50) PRIMARY KEY,
                kind VARCHAR(16) NOT NULL,
                action VARCHAR(32),
                approved BOOLEAN,
                target_id INTEGER,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at TIMESTAMPTZ NOT NULL,
                CHECK (kind IN ('identifier', 'decision', 'export', 'action'))
            )
            """
        )
        # The arguments of a partly filled action (kind 'action'). Added, not
        # created, so a table from before it gains the column.
        await cur.execute(
            "ALTER TABLE pending_actions ADD COLUMN IF NOT EXISTS args JSONB"
        )
        # Which window asked (telegram or panel): the key stays the chat, so a
        # newer question from either still replaces an older one.
        await cur.execute(
            "ALTER TABLE pending_actions ADD COLUMN IF NOT EXISTS "
            "channel VARCHAR(8) NOT NULL DEFAULT 'telegram'"
        )
        # Restated as a named constraint on purpose: the CREATE above only runs
        # when the table is absent, so a database that already has it would keep
        # an older, narrower check the day this list grows, and refuse the new
        # value in production while every test passes. Installing it by name is
        # what makes that widening safe.
        await cur.execute(
            """
            ALTER TABLE pending_actions
            DROP CONSTRAINT IF EXISTS pending_actions_kind_check
            """
        )
        await cur.execute(
            """
            ALTER TABLE pending_actions
            DROP CONSTRAINT IF EXISTS pending_actions_kind_allowed
            """
        )
        await cur.execute(
            """
            ALTER TABLE pending_actions
            ADD CONSTRAINT pending_actions_kind_allowed
            CHECK (kind IN ('identifier', 'decision', 'export', 'action'))
            """
        )
        await cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pending_actions_expires
            ON pending_actions(expires_at)
            """
        )
    await conn.commit()


async def remember(
    conn,
    chat_id,
    *,
    kind: PendingKind,
    ttl_seconds: float,
    action: str | None = None,
    approved: bool | None = None,
    target_id: int | None = None,
    args: dict | None = None,
    channel: str = "telegram",
) -> None:
    """Open a step for this chat, replacing any step already open."""
    async with panel.publishing_current(conn):
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO pending_actions
                    (chat_id, kind, action, approved, target_id, args, channel, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW() + make_interval(secs => %s))
                ON CONFLICT (chat_id) DO UPDATE SET
                    kind       = EXCLUDED.kind,
                    action     = EXCLUDED.action,
                    approved   = EXCLUDED.approved,
                    target_id  = EXCLUDED.target_id,
                    args       = EXCLUDED.args,
                    channel    = EXCLUDED.channel,
                    created_at = NOW(),
                    expires_at = EXCLUDED.expires_at
                """,
                (str(chat_id), kind, action, approved, target_id,
                 Jsonb(args) if args is not None else None, channel, float(ttl_seconds)),
            )


async def peek(conn, chat_id) -> PendingAction | None:
    """Read the open step without consuming it.

    Used where reading must not end the flow — reporting status, and deciding
    whether a message is an answer before knowing if it is a valid one.

    An expired step is still returned, carrying a non-positive ``seconds_left``.
    The caller needs that distinction: a step that ran out earns "that expired,
    start again", while a chat that was never waiting earns silence.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT kind, action, approved, target_id,
                   EXTRACT(EPOCH FROM (expires_at - NOW())), args, channel
            FROM pending_actions
            WHERE chat_id = %s
            """,
            (str(chat_id),),
        )
        row = await cur.fetchone()
    return _row_to_action(row) if row else None


async def consume(conn, chat_id) -> PendingAction | None:
    """Take the open step and close it, in one statement.

    The delete and the read are the same statement on purpose: two callers
    racing on the same chat cannot both come away holding the step, so a
    duplicated message cannot run the action twice.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            DELETE FROM pending_actions
            WHERE chat_id = %s AND expires_at > NOW()
            RETURNING kind, action, approved, target_id,
                      EXTRACT(EPOCH FROM (expires_at - NOW())), args, channel
            """,
            (str(chat_id),),
        )
        row = await cur.fetchone()
    return _row_to_action(row) if row else None


async def forget(conn, chat_id) -> None:
    """Close whatever step is open, expired or not."""
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM pending_actions WHERE chat_id = %s",
            (str(chat_id),),
        )


async def purge_expired(conn) -> int:
    """Drop steps nobody came back to answer. Returns how many were dropped."""
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM pending_actions WHERE expires_at <= NOW()")
        return cur.rowcount or 0
