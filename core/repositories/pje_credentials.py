"""The PJe credential a lawyer enrolled, and the one-time link that took it.

A PJe password is the most dangerous secret this system holds. A Microsoft
refresh token reads somebody's files; a PJe password acts as the lawyer in
court. It is therefore sealed with its own key, separate from the Microsoft
one, so that one leaking does not open the other, and it never passes through
Telegram or the Redis queue.

**The password is typed into a page we serve, not into the chat.** Anything
typed in Telegram is on Telegram's servers, in our update queue, and in the
chat history on the lawyer's phone, none of which we control or can wipe. The
agent therefore sends a link, the browser posts the credential straight to
this host over HTTPS, and the chat only ever carries the link.

**The link is single use and short lived**, and what is stored here is the
SHA-256 of its token rather than the token. The raw token exists only in the
Telegram message, so a leaked database does not hand over a live enrolment
link. The claim is one conditional UPDATE, the same shape the callback buttons
use: a link that two people open at once is spent exactly once.

Nothing here can act on the credential. The MNI client is read-only by
construction and a separate module; this one only keeps the secret.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime

from core.config import settings
from core.crypto import SealError, Sealer

logger = logging.getLogger(__name__)

# Long enough that guessing is hopeless, short enough to tap on a phone.
_TOKEN_BYTES = 32

def sealer() -> Sealer:
    """The one that opens PJe credentials, built from the current settings.

    Built per call rather than cached: the object is cheap, and a cached one
    keyed on a setting is a second place where the key lives, which is how a
    rotated key keeps working in one code path and stops in another.
    """
    return Sealer(
        settings.pje_cred_key,
        env_var="PJE_CRED_KEY",
        subject="a PJe credential",
        on_open_failure=(
            "A stored PJe credential could not be decrypted. The key in "
            "PJE_CRED_KEY is not the one it was sealed with; whoever "
            "enrolled has to enrol again."
        ),
    )


@dataclass(frozen=True)
class Credential:
    """One lawyer's PJe access, with the password left sealed."""

    staff_id: int
    cpf: str
    court: str
    enrolled_at: datetime
    last_used_at: datetime | None = None


def token_digest(token: str) -> str:
    """What is stored for a link, so the stored form cannot be used as one."""
    return hashlib.sha256(token.encode()).hexdigest()


async def ensure_pje_schema(conn) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS pje_credentials (
                staff_id      INTEGER PRIMARY KEY REFERENCES staff(id) ON DELETE CASCADE,
                cpf           VARCHAR(14) NOT NULL,
                password      BYTEA NOT NULL,
                court         VARCHAR(16) NOT NULL,
                enrolled_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_used_at  TIMESTAMPTZ
            )
            """
        )
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS pje_enrolments (
                token_digest  CHAR(64) PRIMARY KEY,
                staff_id      INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at    TIMESTAMPTZ NOT NULL,
                used_at       TIMESTAMPTZ
            )
            """
        )
    await conn.commit()


# ── the one-time link ────────────────────────────────────────────────────────


async def open_enrolment(conn, staff_id: int, *, ttl_seconds: float,
                         commit: bool = True) -> str:
    """Mint a link for this person and return its raw token.

    Any link this person had open is dropped first: two live links mean a
    message forwarded by mistake still works after a new one was asked for.
    """
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM pje_enrolments WHERE staff_id = %s", (staff_id,))
        await cur.execute(
            "INSERT INTO pje_enrolments (token_digest, staff_id, expires_at) "
            "VALUES (%s, %s, NOW() + make_interval(secs => %s))",
            (token_digest(token), staff_id, float(ttl_seconds)),
        )
    if commit:
        await conn.commit()
    return token


async def peek_enrolment(conn, token: str) -> int | None:
    """Whose link this is, without spending it. For rendering the form."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT staff_id FROM pje_enrolments "
            "WHERE token_digest = %s AND used_at IS NULL AND expires_at > NOW()",
            (token_digest(token),),
        )
        row = await cur.fetchone()
    return row[0] if row else None


async def spend_enrolment(conn, token: str, *, commit: bool = True) -> int | None:
    """Take the link, once. Returns whose it was, or None if it is not usable.

    One conditional UPDATE decides it, so two browsers posting at the same
    moment cannot both believe they won.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE pje_enrolments SET used_at = NOW() "
            "WHERE token_digest = %s AND used_at IS NULL AND expires_at > NOW() "
            "RETURNING staff_id",
            (token_digest(token),),
        )
        row = await cur.fetchone()
    if commit:
        await conn.commit()
    return row[0] if row else None


async def purge_expired_enrolments(conn, *, commit: bool = True) -> int:
    """Drop spent and expired links. Swept by the daily maintenance pass."""
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM pje_enrolments WHERE expires_at < NOW() OR used_at IS NOT NULL"
        )
        removed = cur.rowcount or 0
    if commit:
        await conn.commit()
    return removed


# ── the credential ───────────────────────────────────────────────────────────


async def save_credential(conn, staff_id: int, *, cpf: str, password: str,
                          court: str, commit: bool = True) -> None:
    """Store, or replace, this person's PJe access. One per person."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO pje_credentials (staff_id, cpf, password, court)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (staff_id) DO UPDATE SET
                cpf = EXCLUDED.cpf,
                password = EXCLUDED.password,
                court = EXCLUDED.court,
                enrolled_at = NOW(),
                last_used_at = NULL
            """,
            (staff_id, cpf, sealer().seal(password), court),
        )
    if commit:
        await conn.commit()


async def get_credential(conn, staff_id: int) -> Credential | None:
    """Who enrolled and when, without opening the password."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT staff_id, cpf, court, enrolled_at, last_used_at "
            "FROM pje_credentials WHERE staff_id = %s",
            (staff_id,),
        )
        row = await cur.fetchone()
    if not row:
        return None
    return Credential(staff_id=row[0], cpf=row[1], court=row[2],
                      enrolled_at=row[3], last_used_at=row[4])


async def get_password(conn, staff_id: int) -> str | None:
    """The password itself, for the one caller that has to spend it."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT password FROM pje_credentials WHERE staff_id = %s", (staff_id,),
        )
        row = await cur.fetchone()
    return sealer().unseal(row[0]) if row else None


async def touch(conn, staff_id: int, commit: bool = True) -> None:
    """Record that the credential was used, so a stale one is visible."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE pje_credentials SET last_used_at = NOW() WHERE staff_id = %s",
            (staff_id,),
        )
    if commit:
        await conn.commit()


async def forget_credential(conn, staff_id: int, commit: bool = True) -> bool:
    """Drop the credential here. Changing it at the PJe is the person's own act."""
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM pje_credentials WHERE staff_id = %s", (staff_id,))
        removed = bool(cur.rowcount)
    if commit:
        await conn.commit()
    return removed


__all__ = [
    "Credential", "SealError", "ensure_pje_schema", "forget_credential",
    "get_credential", "get_password", "open_enrolment", "peek_enrolment",
    "purge_expired_enrolments", "save_credential", "sealer", "spend_enrolment",
    "token_digest", "touch",
]
