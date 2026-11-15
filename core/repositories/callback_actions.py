"""Button presses that can be trusted: opaque outside, authoritative inside.

What travels through Telegram is a random token and nothing else. No client
name, process number or document content goes into ``callback_data``, so a
payload that leaks, is logged by a device, or is forwarded to somebody else
reveals nothing about the matter it belongs to.

Everything that decides the press lives in the row: which chat, which person,
which action, which resource, when it was issued and when it stops being valid.
A press is checked against that row *and* against the current staff record, so
a button issued before a demotion stops working at the demotion rather than at
its own expiry.

Claiming is a single conditional UPDATE. Two presses of the same button — a
double tap, or Telegram redelivering — cannot both come away holding it: the
first flips the state, the second is told the outcome that was already reached
instead of running the action again.
"""
import secrets
from dataclasses import dataclass
from typing import Literal

from core.repositories import panel

# Telegram caps callback_data at 64 bytes. A 32-byte urlsafe token is 43
# characters, which leaves room and is far past guessing.
_TOKEN_BYTES = 32

Outcome = Literal["granted", "already_used", "cancelled", "expired", "unknown", "forbidden",
                  "other_channel"]


@dataclass(frozen=True)
class CallbackClaim:
    """The verdict on one press."""

    outcome: Outcome
    action: str | None = None
    operation: str | None = None
    resource: str | None = None
    result: str | None = None

    @property
    def may_run(self) -> bool:
        return self.outcome == "granted"


async def issue(
    conn,
    *,
    chat_id,
    staff_id: int,
    issued_role: str,
    action: str,
    ttl_seconds: float,
    channel: str,
    operation: str | None = None,
    resource: str | None = None,
) -> str:
    """Create a button and return the opaque token to put in callback_data.

    `channel` is where the button is shown (Telegram or the panel), and only a
    press arriving by that channel may claim it. Required, so no caller can
    forget to say which one.
    """
    async with panel.publishing_current(conn):
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO callback_actions
                    (token, chat_id, staff_id, issued_role, action, operation,
                     resource, channel, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW() + make_interval(secs => %s))
                """,
                (
                    token, str(chat_id), staff_id, issued_role,
                    action, operation, resource, channel, float(ttl_seconds),
                ),
            )
        if not panel.request_is_bound():
            await conn.commit()
        return token


async def claim(conn, token: str, *, chat_id, staff_id: int, channel: str) -> CallbackClaim:
    """Take a button press, once.

    The UPDATE is the whole decision for the happy path: it matches only a
    pending, unexpired row belonging to this chat and this person, and flips it
    in the same statement. When it matches nothing, a second read says why —
    and the difference matters, because "you already did this" and "somebody
    handed you a token that is not yours" are not the same answer.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE callback_actions
            SET state = 'used', used_at = NOW()
            WHERE token = %s
              AND chat_id = %s
              AND staff_id = %s
              AND channel = %s
              AND state = 'pending'
              AND expires_at > NOW()
            RETURNING action, operation, resource
            """,
            (token, str(chat_id), staff_id, channel),
        )
        row = await cur.fetchone()
        if row:
            await conn.commit()
            return CallbackClaim(
                outcome="granted", action=row[0], operation=row[1], resource=row[2],
            )

        await cur.execute(
            """
            SELECT chat_id, staff_id, state, action, operation, resource, result,
                   expires_at <= NOW(), channel
            FROM callback_actions
            WHERE token = %s
            """,
            (token,),
        )
        found = await cur.fetchone()
    await conn.commit()

    if not found:
        return CallbackClaim(outcome="unknown")

    row_chat, row_staff, state, action, operation, resource, result, expired, row_channel = found
    if row_chat != str(chat_id) or row_staff != staff_id:
        # Deliberately not told apart from an unknown token in the reply the
        # presser sees; the distinction exists here for the audit trail.
        return CallbackClaim(outcome="forbidden")
    if row_channel != channel:
        # The right person, the wrong window: a button shown in the panel is
        # pressed there, and one on Telegram on Telegram. The token is not
        # spent, so the copy where it belongs still works.
        return CallbackClaim(outcome="other_channel")
    if state == "cancelled":
        return CallbackClaim(
            outcome="cancelled", action=action, operation=operation, resource=resource,
        )
    if state == "used":
        return CallbackClaim(
            outcome="already_used", action=action, operation=operation,
            resource=resource, result=result,
        )
    return CallbackClaim(
        outcome="expired", action=action, operation=operation, resource=resource,
    )


async def attach_reference(conn, tokens: list[str], *, reference: str) -> None:
    """Record which message carries these buttons, in its channel's terms:
    Telegram's message id, or the panel turn id.

    The reference is only known after the message is sent, and the tokens have
    to exist before that — they are what the buttons carry. So it is written
    back once the message lands, and a failure here costs the message rewrite,
    not the decision.
    """
    if not tokens:
        return
    async with panel.publishing_current(conn):
        reference = str(reference)
        # Telegram's references are its message ids; keeping the old column filled
        # for them keeps every reader of it working.
        message_id = int(reference) if reference.isdigit() else None
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE callback_actions SET reference = %s, "
                "message_id = COALESCE(%s::bigint, message_id) WHERE token = ANY(%s)",
                (reference, message_id, list(tokens)),
            )
        if not panel.request_is_bound():
            await conn.commit()


async def record_result(conn, token: str, result: str, *,
                        unless_recorded: bool = False) -> None:
    """Store what the press produced, so a repeat can be answered from it.

    `unless_recorded` leaves a result already there: a press cut off after
    its decision committed keeps that decision's answer, not a failure.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE callback_actions SET result = %s WHERE token = %s"
            " AND (NOT %s OR result IS NULL)",
            (result[:2000], token, unless_recorded),
        )
    await conn.commit()


async def cancel_siblings(
    conn,
    *,
    resource: str,
    chat_id=None,
    keep_token: str | None = None,
) -> list[tuple[str, str, str]]:
    """Retire the other buttons on a resource, and say which messages held them.

    Returns the (chat_id, channel, reference) of every copy that was still
    live, so the
    caller can rewrite each one — invalidating the buttons and telling that
    manager what was decided, rather than leaving a button that fails later.

    A decision, a cancellation or an expiry has to leave no live button behind,
    or the same request can be authorized twice from two messages.

    ``chat_id`` is optional because the same request is offered to every
    manager, each in their own chat: a decision has to reach all of those
    copies, not only the one that was pressed.

    The optional filters are cast: a bare ``%s IS NULL`` gives Postgres no
    type for the parameter when it is None, and the press failed after the
    decision had already been taken.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE callback_actions
            SET state = 'cancelled'
            WHERE resource = %s
              AND state = 'pending'
              AND (%s::text IS NULL OR chat_id = %s)
              AND (%s::text IS NULL OR token <> %s)
            RETURNING chat_id, channel, reference
            """,
            (
                resource,
                None if chat_id is None else str(chat_id),
                None if chat_id is None else str(chat_id),
                keep_token, keep_token,
            ),
        )
        retired = [(row[0], row[1], row[2]) for row in await cur.fetchall()
                   if row[2] is not None]
    await conn.commit()
    return retired


async def purge_expired(conn, *, keep_days: int = 7) -> int:
    """Drop rows well past their expiry. Returns how many were dropped.

    Used rows are kept for a while on purpose: answering a repeated press from
    the stored result only works while the row still exists.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            DELETE FROM callback_actions
            WHERE expires_at <= NOW() - make_interval(days => %s)
            """,
            (int(keep_days),),
        )
        removed = cur.rowcount or 0
    await conn.commit()
    return removed


async def ensure_callback_actions_schema(conn) -> None:
    """Create the table on a database that already exists.

    infra/init.sql only runs when PostgreSQL initializes an empty data
    directory, so it never reaches a database that has been running.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS callback_actions (
                token VARCHAR(43) PRIMARY KEY,
                chat_id VARCHAR(50) NOT NULL,
                staff_id INTEGER NOT NULL REFERENCES staff(id),
                issued_role VARCHAR(50) NOT NULL,
                action VARCHAR(64) NOT NULL,
                operation VARCHAR(32),
                resource VARCHAR(128),
                state VARCHAR(16) NOT NULL DEFAULT 'pending',
                message_id BIGINT,
                result TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at TIMESTAMPTZ NOT NULL,
                used_at TIMESTAMPTZ,
                CHECK (state IN ('pending', 'used', 'cancelled'))
            )
            """
        )
        # Shipped one release before the column existed, so an existing
        # database needs the column added rather than the table created.
        await cur.execute(
            "ALTER TABLE callback_actions ADD COLUMN IF NOT EXISTS operation VARCHAR(32)"
        )
        await cur.execute(
            "ALTER TABLE callback_actions ADD COLUMN IF NOT EXISTS message_id BIGINT"
        )
        # Where the button is shown, and the message that carries it in that
        # channel's terms. A button issued before these existed was always a
        # Telegram one, and its message id is its reference.
        await cur.execute(
            "ALTER TABLE callback_actions "
            "ADD COLUMN IF NOT EXISTS channel VARCHAR(8) NOT NULL DEFAULT 'telegram'"
        )
        await cur.execute(
            "ALTER TABLE callback_actions ADD COLUMN IF NOT EXISTS reference VARCHAR(64)"
        )
        await cur.execute(
            "UPDATE callback_actions SET reference = message_id::text "
            "WHERE reference IS NULL AND message_id IS NOT NULL"
        )
        # Restated as a named constraint on purpose: the CREATE above only runs
        # when the table is absent, so a database that already has it would keep
        # an older, narrower check the day this list grows, and refuse the new
        # value in production while every test passes. Installing it by name is
        # what makes that widening safe.
        await cur.execute(
            """
            ALTER TABLE callback_actions
            DROP CONSTRAINT IF EXISTS callback_actions_state_check
            """
        )
        await cur.execute(
            """
            ALTER TABLE callback_actions
            DROP CONSTRAINT IF EXISTS callback_actions_state_allowed
            """
        )
        await cur.execute(
            """
            ALTER TABLE callback_actions
            ADD CONSTRAINT callback_actions_state_allowed
            CHECK (state IN ('pending', 'used', 'cancelled'))
            """
        )
        await cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_callback_actions_resource
            ON callback_actions(chat_id, resource)
            """
        )
        await cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_callback_actions_expires
            ON callback_actions(expires_at)
            """
        )
    await conn.commit()
