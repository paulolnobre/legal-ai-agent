"""Who may open the web pages, and for how long.

Two things live here because they answer the same question from different
sides: what a person enrolled once (their authenticator secret and their
recovery codes), and what they hold right now (a session).

**Sessions are rows, not signed cookies.** A signed cookie is cheaper and
needs no table, and it cannot be revoked: it stays valid until it expires,
whatever happens to the person holding it. This firm has just built a command
for the day somebody leaves, and an account that keeps working until its
cookie ages out is the same gap in another place. A row can be deleted.

**Recovery codes are stored hashed, the secret is stored sealed.** The
difference is not an oversight. A recovery code is checked by comparing, so a
hash is enough and is strictly better; a TOTP secret has to be reproduced to
generate the next code, so it can only be sealed. Where a hash will do, a hash
is what is used.

The seal has its own key rather than sharing one. The reasoning that separated
the Microsoft and PJe keys holds here too: these open different doors. It is
worth saying that with the backup now encrypted, this key's marginal value is
smaller than theirs, since the leak it defends against is a database that
escapes without `.env`. It is kept because the cost is one environment
variable and the pattern is already what everything else follows.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, NamedTuple

from core.config import settings
from core.crypto import SealError, Sealer

logger = logging.getLogger(__name__)

RECOVERY_CODES = 8
MAX_PASSKEYS = 5
# 64 bits each, shown as 16 hexadecimal characters: enough that guessing is
# hopeless against a code that works once, short enough to type off paper.
_RECOVERY_BYTES = 8
_SESSION_BYTES = 32


@dataclass(frozen=True)
class Enrolment:
    """What somebody enrolled, without the secret in it."""

    staff_id: int
    enrolled_at: datetime
    last_counter: int | None = None
    recovery_left: int = 0
    # An enrolment counts only once a first code from the app has been
    # accepted. Until then the person may have closed the tab before saving
    # the secret, and an enrolment nobody can produce codes for would lock
    # them out rather than protect them.
    confirmed: bool = False


@dataclass(frozen=True)
class Session:
    """One live browser session."""

    staff_id: int
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime


def sealer() -> Sealer:
    """The one that opens authenticator secrets."""
    return Sealer(
        settings.web_totp_key,
        env_var="WEB_TOTP_KEY",
        subject="an authenticator secret",
        on_open_failure=(
            "A stored authenticator secret could not be decrypted. The key in "
            "WEB_TOTP_KEY is not the one it was sealed with; whoever enrolled "
            "has to enrol again."
        ),
    )


def digest(value: str) -> str:
    """What is stored for a token or a recovery code, so the stored form is
    not usable as one."""
    return hashlib.sha256(value.encode()).hexdigest()


async def ensure_web_access_schema(conn) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS staff_totp (
                staff_id      INTEGER PRIMARY KEY REFERENCES staff(id) ON DELETE CASCADE,
                secret        BYTEA NOT NULL,
                enrolled_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_counter  BIGINT
            )
            """
        )
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS staff_recovery_codes (
                staff_id     INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
                code_digest  CHAR(64) NOT NULL,
                used_at      TIMESTAMPTZ,
                PRIMARY KEY (staff_id, code_digest)
            )
            """
        )
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS web_sessions (
                token_digest  CHAR(64) PRIMARY KEY,
                staff_id      INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at    TIMESTAMPTZ NOT NULL
            )
            """
        )
        await cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_web_sessions_staff ON web_sessions(staff_id)"
        )
        # A passkey is a credential, not a device, and one person may hold
        # several. user_handle is what the authenticator stores to find the
        # account again: random, one per person, never the staff id.
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS staff_passkeys (
                id            SERIAL PRIMARY KEY,
                staff_id      INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
                credential_id BYTEA NOT NULL UNIQUE,
                public_key    BYTEA NOT NULL,
                sign_count    BIGINT NOT NULL DEFAULT 0,
                user_handle   BYTEA NOT NULL,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_used_at  TIMESTAMPTZ
            )
            """
        )
        await cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_staff_passkeys_staff ON staff_passkeys(staff_id)"
        )
        # The person's handle, drawn once and kept before any device is
        # handed it: two enrolments asked for at once then name one account.
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS staff_passkey_handles (
                staff_id     INTEGER PRIMARY KEY REFERENCES staff(id) ON DELETE CASCADE,
                user_handle  BYTEA NOT NULL,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        # The challenge a passkey ceremony signs, kept hashed and spent once.
        # staff_id is null for the sign-in, where nobody is known yet;
        # session_digest ties a register or unlock challenge to its session.
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS webauthn_challenges (
                challenge_digest  CHAR(64) PRIMARY KEY,
                staff_id          INTEGER REFERENCES staff(id) ON DELETE CASCADE,
                purpose           TEXT NOT NULL,
                session_digest    CHAR(64),
                expires_at        TIMESTAMPTZ NOT NULL,
                used_at           TIMESTAMPTZ,
                CHECK (purpose IN ('register', 'signin', 'unlock'))
            )
            """
        )
        # The enumerations are installed as named constraints, so that adding
        # a value later reaches a running database (see ensure_inbox_schema).
        await cur.execute(
            """
            ALTER TABLE webauthn_challenges
            DROP CONSTRAINT IF EXISTS webauthn_challenges_purpose_check,
            DROP CONSTRAINT IF EXISTS webauthn_challenges_purpose_allowed
            """
        )
        await cur.execute(
            """
            ALTER TABLE webauthn_challenges
            ADD CONSTRAINT webauthn_challenges_purpose_allowed
            CHECK (purpose IN ('register', 'signin', 'unlock'))
            """
        )
        # Sessions opened before passkeys existed keep today's rule: 'link'.
        # Removing a passkey ends the sessions opened with it.
        await cur.execute(
            "ALTER TABLE web_sessions "
            "ADD COLUMN IF NOT EXISTS method TEXT NOT NULL DEFAULT 'link', "
            "ADD COLUMN IF NOT EXISTS passkey_id INTEGER "
            "REFERENCES staff_passkeys(id) ON DELETE CASCADE"
        )
        await cur.execute(
            """
            ALTER TABLE web_sessions
            DROP CONSTRAINT IF EXISTS web_sessions_method_check,
            DROP CONSTRAINT IF EXISTS web_sessions_method_allowed
            """
        )
        await cur.execute(
            """
            ALTER TABLE web_sessions
            ADD CONSTRAINT web_sessions_method_allowed
            CHECK (method IN ('link', 'passkey'))
            """
        )
        await cur.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'web_sessions_passkey_named'
                      AND conrelid = 'web_sessions'::regclass
                ) THEN
                    ALTER TABLE web_sessions ADD CONSTRAINT web_sessions_passkey_named
                        CHECK (method = 'link' OR passkey_id IS NOT NULL);
                END IF;
            END $$
            """
        )
        # The rule a session lives by, fixed when it opens: 'app' (5 h, locks
        # after 15 min) for a passkey on a phone, 'browser' for the rest. Every
        # passkey session open when the column arrives was opened under the
        # app rule, so it becomes 'app', in that run only: afterwards a
        # computer's passkey session is 'browser' and must stay so. The ALTER
        # above already holds this table's lock until the commit, so two
        # startups at once cannot both see the column missing.
        await cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'web_sessions' "
            "AND column_name = 'rule'"
        )
        if await cur.fetchone() is None:
            await cur.execute(
                "ALTER TABLE web_sessions ADD COLUMN rule TEXT NOT NULL DEFAULT 'browser'"
            )
            await cur.execute("UPDATE web_sessions SET rule = 'app' WHERE method = 'passkey'")
        await cur.execute(
            """
            ALTER TABLE web_sessions
            DROP CONSTRAINT IF EXISTS web_sessions_rule_allowed,
            DROP CONSTRAINT IF EXISTS web_sessions_app_rule_passkey
            """
        )
        await cur.execute(
            """
            ALTER TABLE web_sessions
            ADD CONSTRAINT web_sessions_rule_allowed
            CHECK (rule IN ('browser', 'app')),
            ADD CONSTRAINT web_sessions_app_rule_passkey
            CHECK (rule = 'browser' OR method = 'passkey')
            """
        )
        # Where a notice for the installed app goes (push_subscriptions.py).
        # Hangs off the passkey of the session that subscribed: removing the
        # passkey stops that device's notices.
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id          SERIAL PRIMARY KEY,
                staff_id    INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
                passkey_id  INTEGER NOT NULL REFERENCES staff_passkeys(id) ON DELETE CASCADE,
                endpoint    TEXT NOT NULL UNIQUE CHECK (length(endpoint) <= 1024),
                p256dh      BYTEA NOT NULL CHECK (octet_length(p256dh) = 65),
                auth        BYTEA NOT NULL CHECK (octet_length(auth) = 16),
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_ok_at  TIMESTAMPTZ
            )
            """
        )
        await cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_push_subscriptions_staff "
            "ON push_subscriptions(staff_id)"
        )
        await cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_push_subscriptions_passkey "
            "ON push_subscriptions(passkey_id)"
        )
        # Added after the table first shipped, so it is an ALTER on the
        # startup path: init.sql only runs against an empty data directory.
        await cur.execute(
            "ALTER TABLE staff_totp ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMPTZ"
        )
        await cur.execute(
            "ALTER TABLE staff_totp "
            "ADD COLUMN IF NOT EXISTS failures INTEGER NOT NULL DEFAULT 0, "
            "ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ"
        )
        # The first factor. The bot hands one out only to the Telegram account
        # already bound to the staff row, so holding a live one proves control
        # of that account. Stored hashed like everything else that is a key.
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS web_login_links (
                token_digest  CHAR(64) PRIMARY KEY,
                staff_id      INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at    TIMESTAMPTZ NOT NULL,
                used_at       TIMESTAMPTZ
            )
            """
        )
    await conn.commit()


# ── enrolment ────────────────────────────────────────────────────────────────


async def enrol(conn, staff_id: int, *, secret: str,
                commit: bool = True) -> list[str]:
    """Store a secret and mint fresh recovery codes, returning them once.

    Returning them once is the whole contract: they are hashed on the way in,
    so this is the only moment they can be read. Enrolling again replaces
    everything, which is what somebody who lost their phone needs.
    """
    codes = [secrets.token_hex(_RECOVERY_BYTES).upper() for _ in range(RECOVERY_CODES)]
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO staff_totp (staff_id, secret)
            VALUES (%s, %s)
            ON CONFLICT (staff_id) DO UPDATE SET
                secret = EXCLUDED.secret, enrolled_at = NOW(), last_counter = NULL,
                confirmed_at = NULL
            """,
            (staff_id, sealer().seal(secret)),
        )
        await cur.execute(
            "DELETE FROM staff_recovery_codes WHERE staff_id = %s", (staff_id,),
        )
        for code in codes:
            await cur.execute(
                "INSERT INTO staff_recovery_codes (staff_id, code_digest) "
                "VALUES (%s, %s)",
                (staff_id, digest(code)),
            )
    if commit:
        await conn.commit()
    return codes


@dataclass(frozen=True)
class LinkEnrolment:
    """What enrolling through a login link did."""

    staff_id: int
    # None when a confirmed enrolment was already there and was left alone.
    codes: list[str] | None


async def enrol_through_link(conn, token: str, *, secret: str) -> LinkEnrolment | None:
    """Enrol from the web page, or refuse, decided inside one transaction.

    None when the link is dead. Checking "is the link live, is the enrolment
    still unconfirmed" in Python and then writing left a gap: a request could
    pass both checks, wait, and land after a code had confirmed the
    enrolment and spent the link, replacing a confirmed secret with a new one
    shown on its page. That is starting over without /mfa refazer.

    So the link row is locked, which is the row spending it must also lock,
    and the write itself refuses to touch a confirmed enrolment. Replacing a
    confirmed one is /mfa refazer's alone, through forget_enrolment.
    """
    codes = [secrets.token_hex(_RECOVERY_BYTES).upper() for _ in range(RECOVERY_CODES)]
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT staff_id FROM web_login_links "
            "WHERE token_digest = %s AND used_at IS NULL AND expires_at > NOW() "
            "FOR UPDATE",
            (digest(token),),
        )
        row = await cur.fetchone()
        if row is None:
            await conn.rollback()
            return None
        staff_id = row[0]
        await cur.execute(
            """
            INSERT INTO staff_totp (staff_id, secret)
            VALUES (%s, %s)
            ON CONFLICT (staff_id) DO UPDATE SET
                secret = EXCLUDED.secret, enrolled_at = NOW(), last_counter = NULL,
                confirmed_at = NULL, failures = 0, locked_until = NULL
            WHERE staff_totp.confirmed_at IS NULL
            RETURNING staff_id
            """,
            (staff_id, sealer().seal(secret)),
        )
        if await cur.fetchone() is None:
            await conn.rollback()
            return LinkEnrolment(staff_id=staff_id, codes=None)
        await cur.execute(
            "DELETE FROM staff_recovery_codes WHERE staff_id = %s", (staff_id,),
        )
        for code in codes:
            await cur.execute(
                "INSERT INTO staff_recovery_codes (staff_id, code_digest) "
                "VALUES (%s, %s)",
                (staff_id, digest(code)),
            )
    await conn.commit()
    return LinkEnrolment(staff_id=staff_id, codes=codes)


async def enrolment_of(conn, staff_id: int) -> Enrolment | None:
    """What this person enrolled, without the secret."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT t.staff_id, t.enrolled_at, t.last_counter,
                   (SELECT count(*) FROM staff_recovery_codes r
                    WHERE r.staff_id = t.staff_id AND r.used_at IS NULL),
                   t.confirmed_at IS NOT NULL
            FROM staff_totp t WHERE t.staff_id = %s
            """,
            (staff_id,),
        )
        row = await cur.fetchone()
    if not row:
        return None
    return Enrolment(staff_id=row[0], enrolled_at=row[1],
                     last_counter=row[2], recovery_left=int(row[3] or 0),
                     confirmed=bool(row[4]))


async def secret_of(conn, staff_id: int) -> str | None:
    """The secret itself, for the one caller that verifies a code."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT secret FROM staff_totp WHERE staff_id = %s", (staff_id,),
        )
        row = await cur.fetchone()
    return sealer().unseal(row[0]) if row else None


async def secret_for_check(conn, staff_id: int) -> str | None:
    """The secret, with its row locked until the caller commits.

    For checking a code and then recording the step and the confirmation: an
    enrolment written in between would otherwise get the confirmation that
    a code from the previous secret earned. Leaves the transaction open on
    purpose; the caller's next commit releases it.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT secret FROM staff_totp WHERE staff_id = %s FOR UPDATE", (staff_id,),
        )
        row = await cur.fetchone()
    return sealer().unseal(row[0]) if row else None


async def claim_counter(conn, staff_id: int, counter: int,
                        commit: bool = True) -> bool:
    """Accept a code's step, once.

    One conditional UPDATE decides it: the row moves only when the step is
    newer than the one already recorded. A code stays valid for its whole
    window, so without this somebody who read it over a shoulder can walk to
    another machine and use it while it is still good.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE staff_totp SET last_counter = %s "
            "WHERE staff_id = %s AND (last_counter IS NULL OR last_counter < %s)",
            (counter, staff_id, counter),
        )
        accepted = bool(cur.rowcount)
    if commit:
        await conn.commit()
    return accepted


async def confirm_enrolment(conn, staff_id: int, commit: bool = True) -> None:
    """Mark the enrolment real, once its first code has been accepted."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE staff_totp SET confirmed_at = NOW() "
            "WHERE staff_id = %s AND confirmed_at IS NULL",
            (staff_id,),
        )
    if commit:
        await conn.commit()


async def spend_recovery_code(conn, staff_id: int, code: str,
                              commit: bool = True) -> bool:
    """Take one recovery code, once.

    A conditional UPDATE again, so two attempts with the same code cannot both
    win. The row is kept rather than deleted: how many are left, and when each
    was spent, is worth knowing when somebody asks why they have none.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE staff_recovery_codes SET used_at = NOW() "
            "WHERE staff_id = %s AND code_digest = %s AND used_at IS NULL",
            (staff_id, digest(code.strip().upper())),
        )
        spent = bool(cur.rowcount)
    if commit:
        await conn.commit()
    return spent


async def forget_enrolment(conn, staff_id: int, commit: bool = True) -> bool:
    """Drop the enrolment and every session it authorised."""
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM staff_totp WHERE staff_id = %s", (staff_id,))
        removed = bool(cur.rowcount)
        await cur.execute(
            "DELETE FROM staff_recovery_codes WHERE staff_id = %s", (staff_id,),
        )
    await revoke_devices(conn, staff_id, commit=False)
    if commit:
        await conn.commit()
    return removed


# ── attempts: a six-digit code only holds if guessing it is slow ────────────


async def second_factor_locked(conn, staff_id: int) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT locked_until > NOW() FROM staff_totp WHERE staff_id = %s",
            (staff_id,),
        )
        row = await cur.fetchone()
    return bool(row and row[0])


async def record_failure(conn, staff_id: int, *, limit: int, lock_seconds: float,
                         commit: bool = True) -> bool:
    """Count a wrong code, and say whether this one set the lock.

    Counted and decided in one statement, so two wrong codes arriving together
    cannot both read four and both stay under the limit. The count starts
    again once the lock is set, so the next window gets the same allowance
    and no more.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE staff_totp SET
                failures = CASE WHEN failures + 1 >= %(limit)s
                                THEN 0 ELSE failures + 1 END,
                locked_until = CASE WHEN failures + 1 >= %(limit)s
                                    THEN NOW() + make_interval(secs => %(secs)s)
                                    ELSE locked_until END
            WHERE staff_id = %(staff)s
            RETURNING failures = 0 AND locked_until > NOW()
            """,
            {"limit": limit, "secs": float(lock_seconds), "staff": staff_id},
        )
        row = await cur.fetchone()
    if commit:
        await conn.commit()
    return bool(row and row[0])


async def clear_failures(conn, staff_id: int, commit: bool = True) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE staff_totp SET failures = 0 WHERE staff_id = %s AND failures <> 0",
            (staff_id,),
        )
    if commit:
        await conn.commit()


# ── the first factor: a link only the bot hands out ─────────────────────────


async def open_login_link(conn, staff_id: int, *, ttl_seconds: float,
                          commit: bool = True) -> str:
    """Mint a one-time login link for this person and return its raw token.

    Any link they had open is dropped first, so a message forwarded by mistake
    stops working the moment they ask for a new one.
    """
    token = secrets.token_urlsafe(_SESSION_BYTES)
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM web_login_links WHERE staff_id = %s", (staff_id,))
        await cur.execute(
            "INSERT INTO web_login_links (token_digest, staff_id, expires_at) "
            "VALUES (%s, %s, NOW() + make_interval(secs => %s))",
            (digest(token), staff_id, float(ttl_seconds)),
        )
    if commit:
        await conn.commit()
    return token


async def peek_login_link(conn, token: str) -> int | None:
    """Whose link this is, without spending it. For rendering the form."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT staff_id FROM web_login_links "
            "WHERE token_digest = %s AND used_at IS NULL AND expires_at > NOW()",
            (digest(token),),
        )
        row = await cur.fetchone()
    return row[0] if row else None


async def spend_login_link(conn, token: str, commit: bool = True) -> int | None:
    """Take the link, once, and say whose it was.

    Spent before the second factor is checked, so a wrong code burns it. That
    is the safe direction: a link that survives a failed attempt is a link
    against which codes can be tried, and a six-digit code falls to that. The
    cost is that a typo means asking the bot again, which is the right trade
    for a door onto client files.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE web_login_links SET used_at = NOW() "
            "WHERE token_digest = %s AND used_at IS NULL AND expires_at > NOW() "
            "RETURNING staff_id",
            (digest(token),),
        )
        row = await cur.fetchone()
    if commit:
        await conn.commit()
    return row[0] if row else None


# ── sessions ─────────────────────────────────────────────────────────────────


async def open_session(conn, staff_id: int, *, idle_seconds: float,
                       method: Literal["link", "passkey"] = "link",
                       passkey_id: int | None = None,
                       rule: Literal["browser", "app"] = "browser",
                       absolute_seconds: float | None = None,
                       commit: bool = True) -> str:
    """Start a session and return its raw token, which is stored only hashed.

    A link session lives by the browser rule; a passkey session by the
    browser's or the app's, as the device it opened on decides. A browser
    session's end moves with use, so it starts one idle window away. An app
    session's end is its absolute limit, set here and never moved.
    """
    if method == "link" and passkey_id is None and rule == "browser":
        lifetime = float(idle_seconds)
    elif method == "passkey" and passkey_id is not None and rule == "browser":
        lifetime = float(idle_seconds)
    elif method == "passkey" and passkey_id is not None and rule == "app":
        if absolute_seconds is None:
            raise ValueError("an app session needs its limit")
        lifetime = float(absolute_seconds)
    else:
        raise ValueError(
            f"not a session this opens: {method!r} with {passkey_id!r} under {rule!r}")
    token = secrets.token_urlsafe(_SESSION_BYTES)
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO web_sessions "
            "(token_digest, staff_id, expires_at, method, passkey_id, rule) "
            "VALUES (%s, %s, NOW() + make_interval(secs => %s), %s, %s, %s)",
            (digest(token), staff_id, lifetime, method, passkey_id, rule),
        )
    if commit:
        await conn.commit()
    return token


class Touched(NamedTuple):
    """Whose live session this is, how it was opened, the rule it lives by,
    and whether it is locked."""

    staff_id: int
    method: str
    rule: str
    locked: bool


async def touch_session(conn, token: str, *, idle_seconds: float,
                        absolute_seconds: float, app_absolute_seconds: float,
                        lock_seconds: float, activity: bool = True,
                        commit: bool = True) -> Touched | None:
    """Whose session this is, renewing it, or None when it is over.

    The only place the two rules are decided, all in one statement so that no
    code path can renew around them, and on the database's clock:

    - the browser rule (a link session, or a passkey on a computer): 12 h
      from sign-in, ended after 8 h without use, and use moves that idle
      window;
    - the app rule (a passkey on a phone): 5 h from sign-in whatever
      happens, and locked after 15 min without use. Use moves the lock
      clock, but not once locked: only `unlock_session` does that.

    `activity=False` is a look that is not use (the assistant's poll), so a
    page left open does not keep a phone unlocked. It still renews a browser
    session's idle window, as every request did before.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE web_sessions SET
              last_seen_at = CASE WHEN %(activity)s AND (rule = 'browser'
                               OR last_seen_at > NOW() - make_interval(secs => %(lock)s))
                             THEN NOW() ELSE last_seen_at END,
              expires_at = CASE WHEN rule = 'browser'
                           THEN NOW() + make_interval(secs => %(idle)s) ELSE expires_at END
            WHERE token_digest = %(digest)s AND expires_at > NOW()
              AND created_at > NOW() - make_interval(secs => CASE WHEN rule = 'browser'
                                         THEN %(absolute)s ELSE %(app_absolute)s END)
            RETURNING staff_id, method, rule,
              (rule = 'app' AND last_seen_at <= NOW() - make_interval(secs => %(lock)s))
            """,
            {"activity": bool(activity), "lock": float(lock_seconds),
             "idle": float(idle_seconds), "digest": digest(token),
             "absolute": float(absolute_seconds),
             "app_absolute": float(app_absolute_seconds)},
        )
        row = await cur.fetchone()
    if commit:
        await conn.commit()
    return Touched(row[0], row[1], row[2], bool(row[3])) if row else None


async def session_passkey(conn, token: str, staff_id: int) -> int | None:
    """The passkey this person's live session was opened with; None for a
    link session. A lookup only: whether the session may act is decided by
    `touch_session` and its caller. A session's passkey never changes, and a
    removed passkey takes its sessions with it."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT passkey_id FROM web_sessions WHERE token_digest = %s AND staff_id = %s "
            "AND expires_at > NOW()",
            (digest(token), staff_id),
        )
        row = await cur.fetchone()
    return row[0] if row else None


async def unlock_session(conn, token: str, staff_id: int, commit: bool = True) -> bool:
    """Move the lock clock of this person's live app session, after a
    passkey of theirs answered. Nothing else about the session changes: its
    end stays where sign-in put it. A browser session has no lock to move."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE web_sessions SET last_seen_at = NOW() "
            "WHERE token_digest = %s AND staff_id = %s AND rule = 'app' "
            "AND expires_at > NOW()",
            (digest(token), staff_id),
        )
        moved = bool(cur.rowcount)
    if commit:
        await conn.commit()
    return moved


async def close_session(conn, token: str, commit: bool = True) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM web_sessions WHERE token_digest = %s", (digest(token),),
        )
    if commit:
        await conn.commit()


async def close_all_sessions(conn, staff_id: int, commit: bool = True) -> int:
    """Every session this person holds, for a lost laptop or a departure."""
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM web_sessions WHERE staff_id = %s", (staff_id,),
        )
        removed = cur.rowcount or 0
    if commit:
        await conn.commit()
    return removed


# ── passkeys ─────────────────────────────────────────────────────────────────


class Passkey(NamedTuple):
    """One credential, as stored. The creation date is what tells two apart."""

    id: int
    staff_id: int
    credential_id: bytes
    public_key: bytes
    sign_count: int
    created_at: datetime
    last_used_at: datetime | None


_PASSKEY_SELECT = (
    "SELECT id, staff_id, credential_id, public_key, sign_count, created_at, "
    "last_used_at FROM staff_passkeys "
)


def _passkey(row) -> Passkey:
    return Passkey(row[0], row[1], bytes(row[2]), bytes(row[3]), int(row[4]),
                   row[5], row[6])


async def user_handle_for(conn, staff_id: int, commit: bool = True) -> bytes:
    """The handle an authenticator stores for this person, the same every
    time: kept from the first ask, before any device is handed it.

    One per person, so a second device finds the same account. Drawn and
    stored in one statement, so two first enrolments asked for at once
    cannot each hand their device a different one: the second waits on the
    first's row and then reads it. A person whose passkeys predate this
    table keeps the handle those carry. Random and not derived from the
    staff id, because the handle is stored on the device and must reveal
    nothing about who is behind it.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO staff_passkey_handles (staff_id, user_handle) "
            "VALUES (%s, COALESCE((SELECT user_handle FROM staff_passkeys "
            "WHERE staff_id = %s ORDER BY id LIMIT 1), %s)) "
            "ON CONFLICT (staff_id) DO NOTHING",
            (staff_id, staff_id, secrets.token_bytes(32)),
        )
        await cur.execute(
            "SELECT user_handle FROM staff_passkey_handles WHERE staff_id = %s",
            (staff_id,),
        )
        row = await cur.fetchone()
    if commit:
        await conn.commit()
    if row is None:
        raise RuntimeError(f"no passkey handle stored for staff {staff_id}")
    return bytes(row[0])


async def add_passkey(conn, staff_id: int, *, credential_id: bytes,
                      public_key: bytes, sign_count: int, user_handle: bytes,
                      limit: int = MAX_PASSKEYS) -> int | None:
    """Store a passkey and return its id, or None when the person has `limit`.

    Counted and inserted under a lock on the person: a snapshot alone lets two
    enrolments both see four and both write, ending at six. The lock is the
    transaction's, so it is released by the commit or rollback below, which is
    why this owns its transaction (as enrol_through_link does) and takes no
    `commit` argument.

    The handle already on the person's rows wins over the one passed in. The
    routes pass `user_handle_for`'s, which is the same; this keeps one per
    person whatever a caller passes. A credential id that is already stored raises, which
    excludeCredentials is there to prevent.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext('staff_passkeys'), %s)",
            (staff_id,),
        )
        await cur.execute(
            "SELECT count(*) FROM staff_passkeys WHERE staff_id = %s", (staff_id,),
        )
        row = await cur.fetchone()
        if row and row[0] >= limit:
            await conn.rollback()
            return None
        await cur.execute(
            "SELECT user_handle FROM staff_passkeys WHERE staff_id = %s "
            "ORDER BY id LIMIT 1",
            (staff_id,),
        )
        existing = await cur.fetchone()
        await cur.execute(
            "INSERT INTO staff_passkeys "
            "(staff_id, credential_id, public_key, sign_count, user_handle) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (staff_id, credential_id, public_key, sign_count,
             bytes(existing[0]) if existing else user_handle),
        )
        new = await cur.fetchone()
    await conn.commit()
    return new[0] if new else None


async def passkeys_of(conn, staff_id: int) -> list[Passkey]:
    async with conn.cursor() as cur:
        await cur.execute(
            _PASSKEY_SELECT + "WHERE staff_id = %s ORDER BY id",
            (staff_id,),
        )
        return [_passkey(row) for row in await cur.fetchall()]


async def passkey_by_credential(conn, credential_id: bytes) -> Passkey | None:
    async with conn.cursor() as cur:
        await cur.execute(
            _PASSKEY_SELECT + "WHERE credential_id = %s",
            (credential_id,),
        )
        row = await cur.fetchone()
    return _passkey(row) if row else None


async def record_use(conn, passkey_id: int, sign_count: int, *, previous: int,
                     commit: bool = True) -> bool:
    """Store a verified answer's count, only if the stored one is still the
    `previous` it was verified against; say whether it was.

    Verification reads the count and this writes it, so two answers can be
    checked against the same count at once. Compared and set in one
    statement: the second to write waits on the first's row lock, finds the
    count moved, and is refused, and a late answer can never lower it.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE staff_passkeys SET sign_count = %s, last_used_at = NOW() "
            "WHERE id = %s AND sign_count = %s",
            (sign_count, passkey_id, previous),
        )
        recorded = bool(cur.rowcount)
    if commit:
        await conn.commit()
    return recorded


async def remove_passkey(conn, staff_id: int, passkey_id: int,
                         commit: bool = True) -> bool:
    """Remove one of this person's passkeys, and with it the sessions opened
    with it (the foreign key cascades). Another person's id removes nothing."""
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM staff_passkeys WHERE id = %s AND staff_id = %s",
            (passkey_id, staff_id),
        )
        removed = bool(cur.rowcount)
    if commit:
        await conn.commit()
    return removed


# ── passkey challenges ──────────────────────────────────────────────────────

ChallengePurpose = Literal["register", "signin", "unlock"]


class SpentChallenge(NamedTuple):
    """Who a spent challenge was issued for; None for a sign-in."""

    staff_id: int | None


def _challenge_digest(challenge: bytes) -> str:
    return hashlib.sha256(challenge).hexdigest()


async def issue_challenge(conn, purpose: ChallengePurpose, *,
                          staff_id: int | None = None,
                          session_token: str | None = None,
                          ttl_seconds: float = 120,
                          live_cap: int = 100) -> bytes | None:
    """A fresh challenge, returned once and stored only hashed.

    The sign-in challenge is asked for by somebody not yet known, so anyone
    can fill the table with it. At most `live_cap` of them are live at once;
    over that this returns None. Dead sign-in rows (spent or expired) are
    deleted first, so the table stays near the cap. Counted and inserted under
    one lock for the reason add_passkey gives, in a transaction of its own.

    A register or unlock challenge without a person and a session would match
    any caller that also names none, so it is refused here, loudly.
    """
    if purpose != "signin" and (staff_id is None or session_token is None):
        raise ValueError(f"a {purpose} challenge needs the person and the session")
    challenge = secrets.token_bytes(32)
    async with conn.cursor() as cur:
        if purpose == "signin":
            await cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext('webauthn_signin'))"
            )
            # Anyone can ask for these, so the dead ones go before the count:
            # otherwise they pile up until the daily sweep and every request
            # counts them, here, under the lock.
            await cur.execute(
                "DELETE FROM webauthn_challenges WHERE purpose = 'signin' "
                "AND (used_at IS NOT NULL OR expires_at <= NOW())"
            )
            await cur.execute(
                "SELECT count(*) FROM webauthn_challenges WHERE purpose = 'signin'"
            )
            row = await cur.fetchone()
            if row and row[0] >= live_cap:
                # Committed, not rolled back: the sweep stands, the lock goes.
                await conn.commit()
                return None
        await cur.execute(
            "INSERT INTO webauthn_challenges "
            "(challenge_digest, staff_id, purpose, session_digest, expires_at) "
            "VALUES (%s, %s, %s, %s, NOW() + make_interval(secs => %s))",
            (_challenge_digest(challenge), staff_id, purpose,
             digest(session_token) if session_token is not None else None,
             float(ttl_seconds)),
        )
    await conn.commit()
    return challenge


async def spend_challenge(conn, challenge: bytes, purpose: ChallengePurpose, *,
                          session_token: str | None = None,
                          commit: bool = True) -> SpentChallenge | None:
    """Take the challenge, once, for the purpose and session it was issued to.

    One conditional UPDATE decides all of it, like the login links. A
    challenge answered from another session, or for another purpose, is
    refused and left unspent, so the person it belongs to can still answer.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE webauthn_challenges SET used_at = NOW() "
            "WHERE challenge_digest = %s AND used_at IS NULL AND expires_at > NOW() "
            "AND purpose = %s AND session_digest IS NOT DISTINCT FROM %s "
            "RETURNING staff_id",
            (_challenge_digest(challenge), purpose,
             digest(session_token) if session_token is not None else None),
        )
        row = await cur.fetchone()
    if commit:
        await conn.commit()
    return SpentChallenge(staff_id=row[0]) if row else None


async def enrolment_window_open(conn, session_token: str, *,
                                window_seconds: float = 600) -> int | None:
    """Whose session this is, when it may still enrol a passkey.

    Only a session opened by link (the full sign-in), still live, and
    opened less than the window ago. Decided in the statement: a passkey
    session cannot enrol another passkey, and a stolen cookie cannot enrol its
    thief's device once the minutes have passed.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT staff_id FROM web_sessions "
            "WHERE token_digest = %s AND method = 'link' AND expires_at > NOW() "
            "AND created_at > NOW() - make_interval(secs => %s)",
            (digest(session_token), float(window_seconds)),
        )
        row = await cur.fetchone()
    return row[0] if row else None


async def revoke_devices(conn, staff_id: int, commit: bool = True) -> None:
    """End this person's web access on every device: their push
    subscriptions, their passkeys, then every session they hold.

    The subscriptions and the sessions opened with a passkey would go with it
    by the cascade; they are deleted by person here all the same, so that
    what this removes does not depend on how each row was bound. Link
    sessions do not hang off a passkey at all.
    """
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM push_subscriptions WHERE staff_id = %s", (staff_id,))
        await cur.execute("DELETE FROM staff_passkeys WHERE staff_id = %s", (staff_id,))
        await cur.execute("DELETE FROM web_sessions WHERE staff_id = %s", (staff_id,))
    if commit:
        await conn.commit()


async def purge_expired_sessions(conn, commit: bool = True) -> int:
    """Swept by the daily maintenance pass: dead sessions, spent links and
    spent or expired passkey challenges."""
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM web_sessions WHERE expires_at < NOW()")
        removed = cur.rowcount or 0
        await cur.execute(
            "DELETE FROM web_login_links WHERE expires_at < NOW() OR used_at IS NOT NULL"
        )
        await cur.execute(
            "DELETE FROM webauthn_challenges WHERE expires_at < NOW() OR used_at IS NOT NULL"
        )
    if commit:
        await conn.commit()
    return removed


__all__ = [
    "ChallengePurpose", "Enrolment", "MAX_PASSKEYS", "Passkey", "RECOVERY_CODES",
    "SealError", "Session", "SpentChallenge", "add_passkey", "claim_counter",
    "clear_failures", "record_failure", "second_factor_locked",
    "close_all_sessions", "close_session", "confirm_enrolment", "digest",
    "enrol", "enrol_through_link", "enrolment_of", "LinkEnrolment",
    "secret_for_check",
    "open_login_link", "peek_login_link", "spend_login_link",
    "ensure_web_access_schema", "forget_enrolment", "open_session",
    "purge_expired_sessions", "sealer", "secret_of", "spend_recovery_code",
    "touch_session", "Touched", "unlock_session", "enrolment_window_open", "issue_challenge",
    "passkey_by_credential", "passkeys_of", "record_use", "remove_passkey",
    "revoke_devices", "session_passkey", "spend_challenge", "user_handle_for",
]
