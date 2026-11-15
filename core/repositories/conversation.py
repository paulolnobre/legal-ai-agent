"""What each person said to the bot and what it answered, for a while.

The router reads the last two hours of it, which is what lets "e em dobro?"
follow a deadline. The web panel will read the same rows. Each person reads
only their own, every row goes after `query_retention_days`, and erasing a
person (LGPD) deletes theirs outright (`staff.erase_person`).

This is client matter in the database, like the corpus; it was accepted as
such when the panel's conversation tab was decided.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from psycopg.types.json import Jsonb

Speaker = Literal["person", "bot"]

# A login or PJe enrolment link, wherever it appears: a one-time credential
# that must never sit in the conversation for its 90 days.
_ACCESS_LINK = re.compile(r"https?://\S*/(?:web/login|pje)/\S+")


def redact_access_links(text: str) -> str:
    """The text with every access link replaced, the rule both channels'
    recording shares."""
    return _ACCESS_LINK.sub("[link de acesso]", text)


@dataclass(frozen=True)
class Turn:
    speaker: Speaker
    text: str
    at: datetime
    id: int = 0
    channel: str = "telegram"
    extras: dict | None = None
    updated_at: datetime | None = None


async def ensure_conversation_schema(conn) -> None:
    """Create the table on a database that already exists (init.sql only
    runs on an empty data directory)."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_turns (
                id         BIGSERIAL PRIMARY KEY,
                staff_id   INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
                chat_id    VARCHAR(50) NOT NULL,
                speaker    VARCHAR(8) NOT NULL,
                text       TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        # Installed by name, as pending_actions does: a speaker added later
        # must reach a database that already has the table.
        await cur.execute(
            "ALTER TABLE conversation_turns "
            "DROP CONSTRAINT IF EXISTS conversation_turns_speaker_allowed"
        )
        await cur.execute(
            """
            ALTER TABLE conversation_turns
            ADD CONSTRAINT conversation_turns_speaker_allowed
            CHECK (speaker IN ('person', 'bot'))
            """
        )
        await cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_conversation_turns_staff_time
            ON conversation_turns(staff_id, created_at DESC)
            """
        )
        # The web panel's conversation: which window a turn came by, the
        # buttons and file link a panel reply carries, the panel request it
        # belongs to, and when it last changed (a pressed button rewrites its
        # turn). clock_timestamp, not NOW(): a turn rewritten later in the same
        # transaction must carry the later time.
        await cur.execute(
            "ALTER TABLE conversation_turns "
            "ADD COLUMN IF NOT EXISTS channel VARCHAR(8) NOT NULL DEFAULT 'telegram'"
        )
        await cur.execute(
            "ALTER TABLE conversation_turns ADD COLUMN IF NOT EXISTS extras JSONB"
        )
        await cur.execute(
            "ALTER TABLE conversation_turns ADD COLUMN IF NOT EXISTS request_id UUID"
        )
        await cur.execute(
            "ALTER TABLE conversation_turns ADD COLUMN IF NOT EXISTS updated_at "
            "TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()"
        )
    await conn.commit()


async def record_turn(conn, *, staff_id: int, chat_id, speaker: Speaker, text: str,
                      channel: str = "telegram", extras: dict | None = None,
                      request_id: UUID | None = None) -> int:
    """Inside a savepoint: the handler records on the connection it keeps
    using for the reply, and a failed INSERT must not leave that connection
    in an aborted transaction. Returns the turn's id."""
    async with conn.transaction():
        async with conn.cursor() as cur:
            turn_id = await insert_turn(
                cur, staff_id=staff_id, chat_id=chat_id, speaker=speaker, text=text,
                channel=channel, extras=extras, request_id=request_id,
            )
    await conn.commit()
    return turn_id


async def insert_turn(cur, *, staff_id: int, chat_id, speaker: Speaker, text: str,
                      channel: str = "telegram", extras: dict | None = None,
                      request_id: UUID | None = None) -> int:
    """The INSERT alone, on the caller's cursor and transaction, for a turn
    written together with something else (a panel request). Returns its id."""
    await cur.execute(
        "INSERT INTO conversation_turns "
        "(staff_id, chat_id, speaker, text, channel, extras, request_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (staff_id, str(chat_id), speaker, text, channel,
         None if extras is None else Jsonb(extras), request_id),
    )
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def edit_panel_turn(conn, *, staff_id: int, turn_id: int, text: str,
                          commit: bool = True) -> bool:
    """Rewrite one of this person's panel replies, as Telegram edits a
    message: the new text, its buttons gone, and the time it changed, so the
    page polling by `updated_at` sees it. Redacted like every recorded turn."""
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE conversation_turns
                   SET text = %s, extras = extras - 'buttons', updated_at = clock_timestamp()
                 WHERE id = %s AND staff_id = %s AND channel = 'panel' AND speaker = 'bot'
                """,
                (redact_access_links(text), turn_id, staff_id),
            )
            changed = (cur.rowcount or 0) == 1
    if commit:
        await conn.commit()
    return changed


async def recent_turns(
    conn,
    staff_id: int,
    *,
    hours: float = 2,
    max_turns: int = 20,
    max_chars: int = 6000,
    bot_cut: int = 600,
) -> list[Turn]:
    """This person's last turns, oldest first, within the window.

    A bot turn is cut to its first `bot_cut` characters: the router needs to
    know what was answered, not to reread a research answer. Then the oldest
    turns go until the rest fits in `max_chars`.

    A panel message the worker has not started (still waiting, withdrawn, or
    failed before it ran) is not context: an older update answered now must
    not read it as said before. The request being answered was started before
    this is read, so it is never left out.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT t.speaker, t.text, t.created_at FROM conversation_turns t
            WHERE t.staff_id = %s AND t.created_at > NOW() - make_interval(secs => %s)
              AND NOT EXISTS (
                  SELECT 1 FROM panel_requests r
                  WHERE r.id = t.request_id AND r.started_at IS NULL)
            ORDER BY t.created_at DESC, t.id DESC
            LIMIT %s
            """,
            (staff_id, float(hours) * 3600, max_turns),
        )
        rows = await cur.fetchall()
    kept: list[Turn] = []
    used = 0
    for speaker, text, at in rows:  # newest first
        if speaker == "bot":
            text = text[:bot_cut]
        if used + len(text) > max_chars:
            break
        used += len(text)
        kept.append(Turn(speaker=speaker, text=text, at=at))
    kept.reverse()
    return kept


async def page_of(conn, staff_id: int, *, before_id: int | None = None,
                  limit: int = 50) -> tuple[list[Turn], bool]:
    """One page of this person's conversation, from both windows, oldest
    first, and whether older turns remain. `before_id` pages back."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id, speaker, text, created_at, channel, extras, updated_at
            FROM conversation_turns
            WHERE staff_id = %s AND (%s::bigint IS NULL OR id < %s::bigint)
            ORDER BY id DESC
            LIMIT %s
            """,
            (staff_id, before_id, before_id, limit + 1),
        )
        rows = await cur.fetchall()
    more = len(rows) > limit
    turns = [Turn(speaker=speaker, text=text, at=at, id=int(turn_id), channel=channel,
                  extras=extras, updated_at=updated)
             for turn_id, speaker, text, at, channel, extras, updated in rows[:limit]]
    turns.reverse()
    return turns, more


async def cursor(conn) -> datetime:
    """The database's clock now: where the next `changed_since` starts."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT clock_timestamp()")
        row = await cur.fetchone()
    return row[0] if row else datetime.now().astimezone()


# A write commits at most this long after it is stamped: the worker cancels
# an update after 300 s, and the panel's own writes are single statements.
LATE_COMMIT = timedelta(minutes=10)
_CHANGED_LIMIT = 200


async def changed_since(conn, staff_id: int, since: datetime | None, *,
                        request_id: UUID | None) -> tuple[list[Turn], datetime, bool]:
    """This person's turns the page may not have yet, oldest first, the
    cursor for the next call, and whether that is all of them.

    A turn stamped before the cursor can commit after it, so a cursor alone
    would lose it: every turn changed in the last `LATE_COMMIT` comes back
    again, and all of the newest request's. The page places turns by id, so
    one seen twice changes nothing. More than `_CHANGED_LIMIT` (a tab that
    slept through a long conversation) is not all of them: the page then
    loads itself again instead of moving past what it never received.
    """
    next_cursor = await cursor(conn)
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id, speaker, text, created_at, channel, extras, updated_at
            FROM conversation_turns
            WHERE staff_id = %s AND (
                updated_at > %s::timestamptz
                OR updated_at > %s::timestamptz - make_interval(secs => %s)
                OR (%s::uuid IS NOT NULL AND request_id = %s::uuid))
            ORDER BY id DESC
            LIMIT %s
            """,
            (staff_id, since or next_cursor, next_cursor, LATE_COMMIT.total_seconds(),
             request_id, request_id, _CHANGED_LIMIT + 1),
        )
        rows = await cur.fetchall()
    if len(rows) > _CHANGED_LIMIT:
        return [], next_cursor, False
    turns = [Turn(speaker=speaker, text=text, at=at, id=int(turn_id), channel=channel,
                  extras=extras, updated_at=updated)
             for turn_id, speaker, text, at, channel, extras, updated in rows]
    turns.reverse()
    return turns, next_cursor, True


async def purge_older_than(conn, days: int) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM conversation_turns "
            "WHERE created_at < NOW() - make_interval(days => %s)",
            (int(days),),
        )
        deleted = cur.rowcount or 0
    await conn.commit()
    return deleted


# What a follow-up may still draw on: an attachment read in the panel, for
# two hours. Cut here, in SQL, so a long transcript is never read whole.
ATTACHMENT_MAX_CHARS = 60_000
ATTACHMENT_HOURS = 2.0


async def latest_attachment_text(conn, staff_id: int, *,
                                 within_hours: float = ATTACHMENT_HOURS) -> str | None:
    """This person's latest photo reading or audio transcript in the panel,
    when it is younger than `within_hours`; None otherwise.

    Only theirs: the turn's own staff id decides, never the chat. A voice
    note is not an attachment here: its transcript was the question itself,
    answered as Telegram answers one, and carrying it on would only keep
    the next two hours of questions out of the answer cache.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT LEFT(text, %s) FROM conversation_turns "
            "WHERE staff_id = %s AND channel = 'panel' "
            "AND extras->>'reading' IN ('photo', 'audio') "
            "AND created_at > NOW() - make_interval(secs => %s) "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (ATTACHMENT_MAX_CHARS, staff_id, max(0.0, float(within_hours)) * 3600),
        )
        row = await cur.fetchone()
    return str(row[0]) if row else None


__all__ = [
    "ATTACHMENT_HOURS", "ATTACHMENT_MAX_CHARS", "LATE_COMMIT", "Speaker", "Turn",
    "changed_since", "cursor", "edit_panel_turn", "ensure_conversation_schema",
    "insert_turn", "latest_attachment_text", "page_of", "purge_older_than",
    "recent_turns", "record_turn", "redact_access_links",
]

