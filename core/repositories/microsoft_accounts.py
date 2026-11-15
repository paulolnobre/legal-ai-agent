"""The Microsoft account each lawyer connected, and the token that proves it.

A refresh token is a long-lived credential: whoever holds it can read that
person's OneDrive until it is revoked. It is therefore the one value in this
database that is encrypted before it is written, and the reason is specific
rather than ceremonial.

Everything else here is already protected by the same wall: an attacker who
can read the database can read the corpus, and encrypting a token against
that reader would buy nothing. The backup is different. It leaves the host
every night for Magalu Cloud, and the key that decrypts these tokens does not go
with it: it lives in `.env`, which the deploy writes and the backup never
touches. So a leaked backup is a leaked corpus, which is bad, and not a set of
live keys to ten people's OneDrive, which is worse.

Losing the key means every lawyer reconnects. That is a nuisance measured in
one command each, and it is the right side of the trade.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from core.config import settings
from core.crypto import SealError, Sealer

logger = logging.getLogger(__name__)

# Kept as the name this module has always raised, now an alias of the shared
# one: callers that catch TokenSealError go on working, and there is a single
# implementation of the sealing rather than two that drift.
TokenSealError = SealError


@dataclass(frozen=True)
class ConnectedAccount:
    """One lawyer's Microsoft connection, without the secret in it."""

    staff_id: int
    account_name: str
    connected_at: datetime
    last_used_at: datetime | None = None


@dataclass(frozen=True)
class SignIn:
    """A sign-in the panel is waiting on: the code to show, and the one to poll
    with, which never leaves the server."""

    staff_id: int
    flow_id: UUID
    device_code: str
    user_code: str
    verification_uri: str
    interval_seconds: int
    next_poll_at: datetime
    expires_at: datetime


_SIGN_IN_COLUMNS = ("staff_id, flow_id, device_code_sealed, user_code, verification_uri, "
                    "interval_seconds, next_poll_at, expires_at")


def _sealer() -> Sealer:
    return Sealer(
        settings.ms_token_key,
        env_var="MS_TOKEN_KEY",
        subject="a Microsoft token",
        on_open_failure=(
            "A stored Microsoft token could not be decrypted. The key in "
            "MS_TOKEN_KEY is not the one it was sealed with; whoever connected "
            "has to connect again."
        ),
    )


def seal(refresh_token: str) -> bytes:
    """Encrypt a refresh token for storage."""
    return _sealer().seal(refresh_token)


def unseal(sealed: bytes) -> str:
    """Decrypt a stored token, or fail without echoing anything of it."""
    return _sealer().unseal(sealed)


async def ensure_microsoft_schema(conn) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS microsoft_accounts (
                staff_id       INTEGER PRIMARY KEY REFERENCES staff(id) ON DELETE CASCADE,
                account_name   VARCHAR(255) NOT NULL,
                refresh_token  BYTEA NOT NULL,
                connected_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_used_at   TIMESTAMPTZ
            )
            """
        )
        # A sign-in the panel waits on, one per person: a new "Conectar"
        # replaces the earlier code. The device code is a bearer credential
        # until it expires, so it is sealed like the token.
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS microsoft_sign_ins (
                staff_id            INTEGER PRIMARY KEY REFERENCES staff(id) ON DELETE CASCADE,
                flow_id             UUID NOT NULL,
                device_code_sealed  BYTEA NOT NULL,
                user_code           VARCHAR(32) NOT NULL,
                verification_uri    VARCHAR(255) NOT NULL,
                interval_seconds    INTEGER NOT NULL,
                next_poll_at        TIMESTAMPTZ NOT NULL,
                expires_at          TIMESTAMPTZ NOT NULL,
                created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    await conn.commit()


async def save_account(conn, staff_id: int, *, account_name: str,
                       refresh_token: str, commit: bool = True) -> None:
    """Store, or replace, this person's connection. One account per person."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO microsoft_accounts (staff_id, account_name, refresh_token)
            VALUES (%s, %s, %s)
            ON CONFLICT (staff_id) DO UPDATE SET
                account_name = EXCLUDED.account_name,
                refresh_token = EXCLUDED.refresh_token,
                connected_at = NOW(),
                last_used_at = NULL
            """,
            (staff_id, account_name, seal(refresh_token)),
        )
    if commit:
        await conn.commit()


async def get_account(conn, staff_id: int) -> ConnectedAccount | None:
    """Who this person connected as, without touching the token."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT staff_id, account_name, connected_at, last_used_at
            FROM microsoft_accounts WHERE staff_id = %s
            """,
            (staff_id,),
        )
        row = await cur.fetchone()
    if not row:
        return None
    return ConnectedAccount(staff_id=row[0], account_name=row[1],
                            connected_at=row[2], last_used_at=row[3])


async def get_refresh_token(conn, staff_id: int) -> str | None:
    """The token itself, for the one caller that has to spend it."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT refresh_token FROM microsoft_accounts WHERE staff_id = %s",
            (staff_id,),
        )
        row = await cur.fetchone()
    return unseal(row[0]) if row else None


async def touch(conn, staff_id: int, commit: bool = True) -> None:
    """Record that the connection was used, so a stale one is visible."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE microsoft_accounts SET last_used_at = NOW() WHERE staff_id = %s",
            (staff_id,),
        )
    if commit:
        await conn.commit()


async def forget_account(conn, staff_id: int, commit: bool = True) -> bool:
    """Drop the connection here. Revoking at Microsoft is the person's own act."""
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM microsoft_accounts WHERE staff_id = %s", (staff_id,))
        removed = bool(cur.rowcount)
    if commit:
        await conn.commit()
    return removed


def _sign_in(row) -> SignIn:
    return SignIn(staff_id=row[0], flow_id=row[1], device_code=unseal(bytes(row[2])),
                  user_code=row[3], verification_uri=row[4], interval_seconds=row[5],
                  next_poll_at=row[6], expires_at=row[7])


async def start_sign_in(conn, staff_id: int, *, device_code: str, user_code: str,
                        verification_uri: str, interval_seconds: int,
                        expires_in: int) -> None:
    """Keep the code Microsoft gave, replacing any earlier one of this person."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO microsoft_sign_ins (staff_id, flow_id, device_code_sealed, user_code,
                verification_uri, interval_seconds, next_poll_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW() + make_interval(secs => %s),
                    NOW() + make_interval(secs => %s))
            ON CONFLICT (staff_id) DO UPDATE SET
                flow_id = EXCLUDED.flow_id,
                device_code_sealed = EXCLUDED.device_code_sealed,
                user_code = EXCLUDED.user_code,
                verification_uri = EXCLUDED.verification_uri,
                interval_seconds = EXCLUDED.interval_seconds,
                next_poll_at = EXCLUDED.next_poll_at,
                expires_at = EXCLUDED.expires_at,
                created_at = NOW()
            """,
            (staff_id, uuid.uuid4(), seal(device_code), user_code, verification_uri,
             interval_seconds, interval_seconds, expires_in, ),
        )
    await conn.commit()


async def live_sign_in(conn, staff_id: int) -> SignIn | None:
    """This person's sign-in, while its code still holds."""
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_SIGN_IN_COLUMNS} FROM microsoft_sign_ins "
            "WHERE staff_id = %s AND expires_at > NOW()",
            (staff_id,),
        )
        row = await cur.fetchone()
    return _sign_in(row) if row else None


# How long a claimed poll holds the turn: longer than a slow request to
# Microsoft, so a second tab never polls the same code meanwhile. The poller
# hands the turn back at the usual interval once Microsoft has answered
# (`delay_poll`); a poller that died lets the next one in after this.
CLAIM_HOLD_SECONDS = 60


async def claim_poll(conn, staff_id: int) -> SignIn | None:
    """The sign-in, when a poll is due, with the turn taken in the same
    statement: of two tabs asking at once, one gets it and the other waits
    until it is handed back."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE microsoft_sign_ins "
            "SET next_poll_at = NOW() + make_interval(secs => %s) "
            "WHERE staff_id = %s AND next_poll_at <= NOW() AND expires_at > NOW() "
            f"RETURNING {_SIGN_IN_COLUMNS}",
            (CLAIM_HOLD_SECONDS, staff_id),
        )
        row = await cur.fetchone()
    await conn.commit()
    return _sign_in(row) if row else None


async def delay_poll(conn, staff_id: int, flow_id: UUID, seconds: int) -> None:
    """The next poll of that flow, `seconds` from now: the turn handed back
    after an answer, or more room when Microsoft asked for it (`slow_down`)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE microsoft_sign_ins SET interval_seconds = %s, "
            "next_poll_at = NOW() + make_interval(secs => %s) "
            "WHERE staff_id = %s AND flow_id = %s",
            (seconds, seconds, staff_id, flow_id),
        )
    await conn.commit()


async def end_sign_in(conn, staff_id: int, flow_id: UUID | None = None, *,
                      commit: bool = True) -> bool:
    """Drop that flow (any of this person's, without one). True when one went."""
    async with conn.cursor() as cur:
        if flow_id is None:
            await cur.execute("DELETE FROM microsoft_sign_ins WHERE staff_id = %s", (staff_id,))
        else:
            await cur.execute("DELETE FROM microsoft_sign_ins WHERE staff_id = %s "
                              "AND flow_id = %s", (staff_id, flow_id))
        removed = bool(cur.rowcount)
    if commit:
        await conn.commit()
    return removed


async def finish_sign_in(conn, staff_id: int, flow_id: UUID, *, account_name: str,
                         refresh_token: str) -> bool:
    """Connect, only if this flow is still the one waiting: a flow replaced by
    a newer code, or cancelled by a disconnect, while its poll was out,
    connects nothing. One transaction."""
    if not await end_sign_in(conn, staff_id, flow_id, commit=False):
        await conn.commit()
        return False
    await save_account(conn, staff_id, account_name=account_name,
                       refresh_token=refresh_token, commit=False)
    await conn.commit()
    return True


async def disconnect(conn, staff_id: int) -> bool:
    """Drop the connection and any sign-in still waiting, together: a code
    the person already typed cannot connect the account again afterwards.

    The sign-in first, in the order `finish_sign_in` takes them: a poll that
    took its flow a moment earlier makes this wait until its account is
    committed, and that account is then the one deleted here."""
    await end_sign_in(conn, staff_id, commit=False)
    removed = await forget_account(conn, staff_id, commit=False)
    await conn.commit()
    return removed


async def purge_expired_sign_ins(conn) -> int:
    """Codes past their life: refused already by every read; this removes them."""
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM microsoft_sign_ins WHERE expires_at <= NOW()")
        removed = cur.rowcount or 0
    await conn.commit()
    return removed


__all__ = [
    "ConnectedAccount", "SignIn", "TokenSealError", "claim_poll", "delay_poll", "disconnect",
    "end_sign_in", "ensure_microsoft_schema", "finish_sign_in", "forget_account",
    "live_sign_in", "purge_expired_sign_ins", "start_sign_in",
    "get_account", "get_refresh_token", "save_account", "seal", "touch", "unseal",
]
