"""The web access SQL, run against a real Postgres.

Every other repository test in this project uses a double, and for most SQL
that is acceptable. Here it is not, because **the query is the security
property**: a replayed code is refused by `last_counter < %s`, a session left
open on a shared machine dies by a clock checked inside the UPDATE, a recovery
code works once because of a conditional write. A double that re-implements
those conditions in Python tests the double.

So these run against a real database when one is given, and are skipped when
not. To run them:

    docker run -d --rm --name pgtest -e POSTGRES_PASSWORD=t -p 55432:5432 \\
        pgvector/pgvector:pg16
    TEST_DATABASE_URL=postgresql://postgres:t@127.0.0.1:55432/postgres \\
        pytest tests/test_web_access_sql.py
    docker stop pgtest

CI provides one (see the postgres service in ci.yml), and the guard test
below fails if it ever stops doing so.
"""
import asyncio
import hashlib
import os
import uuid

from typing import Any, Literal, LiteralString, TypedDict

import pytest
from psycopg import sql
from cryptography.fernet import Fernet

from core.config import settings
from core.repositories import web_access

URL = os.environ.get("TEST_DATABASE_URL")
# Unique per test process. Two runs against one database, a second terminal
# or another agent, would otherwise drop and rebuild each other's schema
# mid-test: a login test then met a secret it never enrolled.
SCHEMA = f"webtest_{uuid.uuid4().hex[:12]}"


def _in_schema(statement: LiteralString) -> sql.Composed:
    """A statement naming this run's schema, quoted as an identifier."""
    return sql.SQL(statement).format(schema=sql.Identifier(SCHEMA))

_needs_database = pytest.mark.skipif(
    not URL, reason="needs a real Postgres: set TEST_DATABASE_URL",
)


def test_ci_provides_the_database_these_tests_need():
    """Not skipped where it matters, deliberately: this is the guard.

    Every other test here skips without a database, and a skip reads as
    green. If the Postgres service is ever dropped from CI, eighteen tests of
    security-critical SQL would stop running and nothing would say so.

    It enforces where the database is *promised*, which `ci.yml` states with
    REQUIRE_TEST_DATABASE. A first version keyed on GITHUB_ACTIONS instead,
    which is true in every workflow, and failed the feature-branch one: that
    workflow has no database on purpose, to avoid paying for the same pytest
    twice. The proxy was not the thing. The promise is.
    """
    if os.environ.get("REQUIRE_TEST_DATABASE") != "1":
        pytest.skip("this workflow does not promise a database")
    assert URL, (
        "TEST_DATABASE_URL is not set in CI, so the web access SQL tests are "
        "being skipped. Restore the postgres service in .github/workflows/ci.yml."
    )


def _run(coroutine_factory):
    """One fresh schema per test, dropped afterwards, so tests cannot lean on
    each other's rows."""
    import psycopg

    async def main():
        # Reached only past the skip marker, which pyright cannot see.
        assert URL, "a database test ran without TEST_DATABASE_URL"
        async with await psycopg.AsyncConnection.connect(URL) as conn:
            async with conn.cursor() as cur:
                await cur.execute(_in_schema("DROP SCHEMA IF EXISTS {schema} CASCADE"))
                await cur.execute(_in_schema("CREATE SCHEMA {schema}"))
                await cur.execute(_in_schema("SET search_path TO {schema}"))
                await cur.execute(
                    "CREATE TABLE staff (id SERIAL PRIMARY KEY, name TEXT)"
                )
                await cur.execute("INSERT INTO staff (name) VALUES ('Rui'), ('Ana')")
            await conn.commit()
            await web_access.ensure_web_access_schema(conn)
            try:
                return await coroutine_factory(conn)
            finally:
                async with conn.cursor() as cur:
                    await cur.execute(_in_schema("DROP SCHEMA IF EXISTS {schema} CASCADE"))
                await conn.commit()

    return asyncio.run(main())


class _AppRules(TypedDict):
    app_absolute_seconds: float
    lock_seconds: float


# The app session's two clocks, as configured: 5 h absolute, locked after 15 min.
_APP: _AppRules = {"app_absolute_seconds": 5 * 3600, "lock_seconds": 15 * 60}


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(settings, "web_totp_key", Fernet.generate_key().decode())


# ── what is stored ───────────────────────────────────────────────────────────


@_needs_database
def test_the_secret_is_stored_sealed_and_comes_back():
    async def body(conn):
        await web_access.enrol(conn, 1, secret="JBSWY3DPEHPK3PXP")
        async with conn.cursor() as cur:
            await cur.execute("SELECT secret FROM staff_totp WHERE staff_id = 1")
            stored = bytes((await cur.fetchone())[0])
        return stored, await web_access.secret_of(conn, 1)

    stored, back = _run(body)

    assert b"JBSWY3DPEHPK3PXP" not in stored
    assert back == "JBSWY3DPEHPK3PXP"


@_needs_database
def test_recovery_codes_are_stored_only_as_hashes():
    """Returned once, readable never again. That is the whole contract."""
    async def body(conn):
        codes = await web_access.enrol(conn, 1, secret="JBSWY3DPEHPK3PXP")
        async with conn.cursor() as cur:
            await cur.execute("SELECT code_digest FROM staff_recovery_codes")
            stored = {row[0] for row in await cur.fetchall()}
        return codes, stored

    codes, stored = _run(body)

    assert len(codes) == web_access.RECOVERY_CODES
    assert not set(codes) & stored
    assert stored == {web_access.digest(code) for code in codes}


# ── a code is accepted once ──────────────────────────────────────────────────


@_needs_database
def test_a_step_is_accepted_once_and_never_again():
    """The replay property. A code stays valid for its whole window, so
    without this somebody who read it over a shoulder can use it too."""
    async def body(conn):
        await web_access.enrol(conn, 1, secret="JBSWY3DPEHPK3PXP")
        first = await web_access.claim_counter(conn, 1, 1000)
        again = await web_access.claim_counter(conn, 1, 1000)
        return first, again

    first, again = _run(body)

    assert first is True
    assert again is False


@_needs_database
def test_an_older_step_is_refused_after_a_newer_one():
    """A code from the drift window's earlier step, used after the current
    one, is a replay in disguise."""
    async def body(conn):
        await web_access.enrol(conn, 1, secret="JBSWY3DPEHPK3PXP")
        await web_access.claim_counter(conn, 1, 1001)
        return await web_access.claim_counter(conn, 1, 1000)

    assert _run(body) is False


@_needs_database
def test_one_persons_step_does_not_spend_anothers():
    async def body(conn):
        await web_access.enrol(conn, 1, secret="JBSWY3DPEHPK3PXP")
        await web_access.enrol(conn, 2, secret="JBSWY3DPEHPK3PXP")
        await web_access.claim_counter(conn, 1, 1000)
        return await web_access.claim_counter(conn, 2, 1000)

    assert _run(body) is True


@_needs_database
def test_enrolling_again_forgets_the_last_step():
    """A new phone is a new secret; the old step means nothing for it."""
    async def body(conn):
        await web_access.enrol(conn, 1, secret="JBSWY3DPEHPK3PXP")
        await web_access.claim_counter(conn, 1, 5000)
        await web_access.enrol(conn, 1, secret="KRSXG5CTMVRXEZLU")
        return await web_access.claim_counter(conn, 1, 10)

    assert _run(body) is True


# ── recovery codes ───────────────────────────────────────────────────────────


@_needs_database
def test_a_recovery_code_works_exactly_once():
    async def body(conn):
        codes = await web_access.enrol(conn, 1, secret="JBSWY3DPEHPK3PXP")
        first = await web_access.spend_recovery_code(conn, 1, codes[0])
        again = await web_access.spend_recovery_code(conn, 1, codes[0])
        enrolment = await web_access.enrolment_of(conn, 1)
        assert enrolment is not None
        left = enrolment.recovery_left
        return first, again, left

    first, again, left = _run(body)

    assert first is True
    assert again is False
    assert left == web_access.RECOVERY_CODES - 1


@_needs_database
def test_a_recovery_code_typed_in_lowercase_with_spaces_still_works():
    async def body(conn):
        codes = await web_access.enrol(conn, 1, secret="JBSWY3DPEHPK3PXP")
        return await web_access.spend_recovery_code(conn, 1, f"  {codes[0].lower()} ")

    assert _run(body) is True


@_needs_database
def test_somebody_elses_recovery_code_does_not_work():
    async def body(conn):
        codes = await web_access.enrol(conn, 1, secret="JBSWY3DPEHPK3PXP")
        await web_access.enrol(conn, 2, secret="KRSXG5CTMVRXEZLU")
        return await web_access.spend_recovery_code(conn, 2, codes[0])

    assert _run(body) is False


@_needs_database
def test_enrolling_again_voids_the_old_recovery_codes():
    """A lost phone and a lost sheet of codes are often the same event."""
    async def body(conn):
        old = await web_access.enrol(conn, 1, secret="JBSWY3DPEHPK3PXP")
        await web_access.enrol(conn, 1, secret="KRSXG5CTMVRXEZLU")
        return await web_access.spend_recovery_code(conn, 1, old[0])

    assert _run(body) is False


# ── sessions ─────────────────────────────────────────────────────────────────


@_needs_database
def test_a_fresh_session_is_recognised_and_renewed():
    async def body(conn):
        token = await web_access.open_session(conn, 1, idle_seconds=3600)
        return await web_access.touch_session(
            conn, token, idle_seconds=3600, absolute_seconds=7200, **_APP,
        )

    assert _run(body) == (1, "link", "browser", False)


@_needs_database
def test_an_idle_session_is_over():
    async def body(conn):
        token = await web_access.open_session(conn, 1, idle_seconds=3600)
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE web_sessions SET expires_at = NOW() - interval '1 second'"
            )
        await conn.commit()
        return await web_access.touch_session(
            conn, token, idle_seconds=3600, absolute_seconds=7200, **_APP,
        )

    assert _run(body) is None


@_needs_database
def test_renewal_cannot_outlive_the_absolute_limit():
    """The tab left open on a shared machine. Use keeps the idle window moving
    forever; the absolute one does not move, and it is checked in the same
    statement so no code path can renew around it."""
    async def body(conn):
        token = await web_access.open_session(conn, 1, idle_seconds=3600)
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE web_sessions SET created_at = NOW() - interval '13 hours', "
                "expires_at = NOW() + interval '1 hour'"
            )
        await conn.commit()
        return await web_access.touch_session(
            conn, token, idle_seconds=8 * 3600, absolute_seconds=12 * 3600, **_APP,
        )

    assert _run(body) is None


@_needs_database
def test_the_token_is_stored_only_hashed():
    """A leaked database must not hand over a live session."""
    async def body(conn):
        token = await web_access.open_session(conn, 1, idle_seconds=3600)
        async with conn.cursor() as cur:
            await cur.execute("SELECT token_digest FROM web_sessions")
            stored = (await cur.fetchone())[0]
        return token, stored

    token, stored = _run(body)

    assert stored != token and token not in stored
    assert stored == web_access.digest(token)


@_needs_database
def test_an_unknown_token_is_nobody():
    async def body(conn):
        await web_access.open_session(conn, 1, idle_seconds=3600)
        return await web_access.touch_session(
            conn, "not-a-real-token", idle_seconds=3600, absolute_seconds=7200, **_APP,
        )

    assert _run(body) is None


@_needs_database
def test_closing_every_session_of_a_person_ends_them_all():
    """A lost laptop, or somebody leaving."""
    async def body(conn):
        a = await web_access.open_session(conn, 1, idle_seconds=3600)
        b = await web_access.open_session(conn, 1, idle_seconds=3600)
        other = await web_access.open_session(conn, 2, idle_seconds=3600)
        closed = await web_access.close_all_sessions(conn, 1)
        still = [
            await web_access.touch_session(conn, t, idle_seconds=3600,
                                           absolute_seconds=7200, **_APP)
            for t in (a, b, other)
        ]
        return closed, [t and t.staff_id for t in still]

    closed, still = _run(body)

    assert closed == 2
    assert still == [None, None, 2], "only that person's sessions end"


@_needs_database
def test_forgetting_an_enrolment_also_ends_its_sessions():
    """A session authorised by a factor that no longer exists should not
    outlive it."""
    async def body(conn):
        await web_access.enrol(conn, 1, secret="JBSWY3DPEHPK3PXP")
        token = await web_access.open_session(conn, 1, idle_seconds=3600)
        await web_access.forget_enrolment(conn, 1)
        return await web_access.touch_session(
            conn, token, idle_seconds=3600, absolute_seconds=7200, **_APP,
        )

    assert _run(body) is None


@_needs_database
def test_deleting_the_staff_row_takes_everything_with_it():
    """The cascade is the erasure path for LGPD: nothing of a person's access
    survives their row."""
    async def body(conn):
        await web_access.enrol(conn, 1, secret="JBSWY3DPEHPK3PXP")
        await web_access.open_session(conn, 1, idle_seconds=3600)
        await web_access.open_login_link(conn, 1, ttl_seconds=300)
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM staff WHERE id = 1")
            counts = []
            for table in ("staff_totp", "staff_recovery_codes", "web_sessions",
                          "web_login_links"):
                await cur.execute(f"SELECT count(*) FROM {table}")
                counts.append((await cur.fetchone())[0])
        await conn.commit()
        return counts

    assert _run(body) == [0, 0, 0, 0]


# ── the app session: locked after 15 minutes, over after 5 hours ────────────

_COLUMNS = frozenset({"created_at", "last_seen_at", "expires_at"})


async def _passkey_session(conn, staff_id: int = 1, n: int = 0, *,
                           rule: Literal["browser", "app"] = "app") -> str:
    """A passkey session as the sign-in opens it: on a phone by default."""
    pid = await _add(conn, staff_id, n)
    assert pid is not None
    return await web_access.open_session(
        conn, staff_id, idle_seconds=8 * 3600, method="passkey", passkey_id=pid,
        rule=rule, absolute_seconds=_APP["app_absolute_seconds"])


async def _touch(conn, token: str, *, activity: bool = True):
    return await web_access.touch_session(
        conn, token, idle_seconds=8 * 3600, absolute_seconds=12 * 3600,
        activity=activity, **_APP)


async def _ago(conn, token: str, column: str, seconds: float) -> None:
    """Set a session's clock column to that long before the database's now."""
    assert column in _COLUMNS
    async with conn.cursor() as cur:
        await cur.execute(
            sql.SQL("UPDATE web_sessions SET {c} = NOW() - make_interval(secs => %s) "
                    "WHERE token_digest = %s").format(c=sql.Identifier(column)),
            (float(seconds), web_access.digest(token)))
    await conn.commit()


async def _earlier(conn, token: str, column: str, seconds: float) -> None:
    """Move a session's clock column back by that long: time passing."""
    assert column in _COLUMNS
    async with conn.cursor() as cur:
        await cur.execute(
            sql.SQL("UPDATE web_sessions SET {c} = {c} - make_interval(secs => %s) "
                    "WHERE token_digest = %s").format(c=sql.Identifier(column)),
            (float(seconds), web_access.digest(token)))
    await conn.commit()


async def _clocks(conn, token: str):
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT created_at, last_seen_at, expires_at FROM web_sessions "
            "WHERE token_digest = %s", (web_access.digest(token),))
        return await cur.fetchone()


@_needs_database
def test_a_passkey_session_is_opened_with_its_own_limit():
    async def body(conn):
        token = await _passkey_session(conn)
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT method, passkey_id IS NOT NULL, "
                "expires_at - created_at FROM web_sessions WHERE token_digest = %s",
                (web_access.digest(token),))
            row = await cur.fetchone()
        refused = []
        for passkey_id, absolute in ((1, None), (None, 60.0)):
            try:
                await web_access.open_session(conn, 1, idle_seconds=60, method="passkey",
                                              passkey_id=passkey_id, rule="app",
                                              absolute_seconds=absolute)
            except ValueError:
                refused.append(True)
        return row, refused, await _count(conn, "web_sessions")

    (method, named, lifetime), refused, rows = _run(body)

    assert method == "passkey" and named
    assert abs(lifetime.total_seconds() - 5 * 3600) < 5
    assert refused == [True, True] and rows == 1


@_needs_database
def test_passkey_session_locks_after_fifteen_minutes():
    async def body(conn):
        token = await _passkey_session(conn)
        fresh = await _touch(conn, token)
        await _ago(conn, token, "last_seen_at", 15 * 60 - 5)
        inside = await _touch(conn, token)
        await _ago(conn, token, "last_seen_at", 15 * 60 + 1)
        return fresh, inside, await _touch(conn, token)

    fresh, inside, past = _run(body)

    assert fresh == (1, "passkey", "app", False)
    assert inside == (1, "passkey", "app", False)
    assert past == (1, "passkey", "app", True)


@_needs_database
def test_passkey_session_ends_at_five_hours_despite_use():
    """Used every ten minutes, so never locked; the 5 h still do not move."""
    async def body(conn):
        token = await _passkey_session(conn)
        opened_until = (await _clocks(conn, token))[2]
        used = []
        for _ in range(29):
            await _earlier(conn, token, "created_at", 600)
            await _earlier(conn, token, "last_seen_at", 600)
            used.append(await _touch(conn, token))
        still_until = (await _clocks(conn, token))[2]
        await _earlier(conn, token, "created_at", 600)
        await _earlier(conn, token, "last_seen_at", 600)
        return used, opened_until == still_until, await _touch(conn, token)

    used, fixed, ended = _run(body)

    assert used == [(1, "passkey", "app", False)] * 29, "used within 4 h 50 min, never locked"
    assert fixed, "use moved the passkey session's end"
    assert ended is None


@_needs_database
def test_link_session_rules_are_unchanged():
    """8 h without use, 12 h absolute, and no lock however long it sits."""
    async def body(conn):
        old = await web_access.open_session(conn, 1, idle_seconds=8 * 3600)
        await _ago(conn, old, "created_at", 11 * 3600 + 50 * 60)
        await _ago(conn, old, "last_seen_at", 7 * 3600 + 59 * 60)
        used = await _touch(conn, old)
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT expires_at > NOW() + interval '7 hours 59 minutes', "
                "last_seen_at > NOW() - interval '1 minute' FROM web_sessions "
                "WHERE token_digest = %s", (web_access.digest(old),))
            renewed = await cur.fetchone()
        too_old = await web_access.open_session(conn, 1, idle_seconds=8 * 3600)
        await _ago(conn, too_old, "created_at", 12 * 3600 + 1)
        idle = await web_access.open_session(conn, 1, idle_seconds=8 * 3600)
        await _ago(conn, idle, "expires_at", 1)
        return used, tuple(renewed), await _touch(conn, too_old), await _touch(conn, idle)

    used, renewed, too_old, idle = _run(body)

    assert used == (1, "link", "browser", False), (
        "past the app's 5 h and 15 min, a link session lives")
    assert renewed == (True, True)
    assert too_old is None and idle is None


@_needs_database
def test_a_look_without_activity_does_not_move_the_lock_clock():
    """The assistant's poll: a page left open must not keep a phone unlocked."""
    async def body(conn):
        token = await _passkey_session(conn)
        await _ago(conn, token, "last_seen_at", 14 * 60)
        look = await _touch(conn, token, activity=False)
        await _earlier(conn, token, "last_seen_at", 2 * 60)
        return look, await _touch(conn, token, activity=False)

    look, later = _run(body)

    assert look == (1, "passkey", "app", False)
    assert later == (1, "passkey", "app", True)


@_needs_database
def test_a_locked_session_is_not_renewed_by_use():
    async def body(conn):
        token = await _passkey_session(conn)
        await _ago(conn, token, "last_seen_at", 16 * 60)
        before = await _clocks(conn, token)
        first, again = await _touch(conn, token), await _touch(conn, token)
        return first, again, before == await _clocks(conn, token)

    first, again, untouched = _run(body)

    assert first == again == (1, "passkey", "app", True)
    assert untouched, "a locked session's clocks moved"


@_needs_database
def test_unlock_needs_the_same_person():
    async def body(conn):
        mine = await _passkey_session(conn, 1, 0)
        await _ago(conn, mine, "last_seen_at", 16 * 60)
        before = await _clocks(conn, mine)
        by_other = await web_access.unlock_session(conn, mine, 2)
        still = await _touch(conn, mine)
        by_self = await web_access.unlock_session(conn, mine, 1)
        after = await _clocks(conn, mine)
        link = await web_access.open_session(conn, 1, idle_seconds=3600)
        ended = await _passkey_session(conn, 1, 1)
        await _ago(conn, ended, "expires_at", 1)
        return (by_other, still, by_self, await _touch(conn, mine), before, after,
                await web_access.unlock_session(conn, link, 1),
                await web_access.unlock_session(conn, ended, 1),
                await web_access.unlock_session(conn, "not-a-session", 1))

    (by_other, still, by_self, now, before, after, link, ended, unknown) = _run(body)

    assert by_other is False and still == (1, "passkey", "app", True)
    assert by_self is True and now == (1, "passkey", "app", False)
    assert after[0] == before[0] and after[2] == before[2], "only last_seen_at moves"
    assert after[1] > before[1]
    assert (link, ended, unknown) == (False, False, False)


@_needs_database
def test_lock_ignores_client_time(monkeypatch):
    """The lock is the database's clock against the row: nothing a caller
    holds, its own clock or its time zone, moves it either way."""
    import time

    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() - 24 * 3600)

    async def body(conn):
        fresh = await _passkey_session(conn, 1, 0)
        stale = await _passkey_session(conn, 1, 1)
        await _ago(conn, stale, "last_seen_at", 16 * 60)
        seen = []
        for zone in ("Etc/GMT-14", "Etc/GMT+12"):
            async with conn.cursor() as cur:
                await cur.execute(sql.SQL("SET TIME ZONE {}").format(sql.Literal(zone)))
            await conn.commit()
            seen.append((await _touch(conn, fresh), await _touch(conn, stale)))
        return seen

    for fresh, stale in _run(body):
        assert fresh == (1, "passkey", "app", False)
        assert stale == (1, "passkey", "app", True)


# ── the rule follows the device: a passkey on a computer is a browser ──────


async def _rule_and_lifetime(conn, token: str):
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT rule, expires_at - created_at FROM web_sessions "
            "WHERE token_digest = %s", (web_access.digest(token),))
        rule, lifetime = await cur.fetchone()
    return rule, lifetime.total_seconds()


@_needs_database
def test_computer_passkey_session_follows_the_browser_rule():
    """8 h without use, 12 h at most, and never locked however long it sits:
    a link session's rule, on a passkey."""
    async def body(conn):
        token = await _passkey_session(conn, rule="browser")
        opened = await _rule_and_lifetime(conn, token)
        await _ago(conn, token, "last_seen_at", 16 * 60)
        looked = await _touch(conn, token, activity=False)
        await _ago(conn, token, "created_at", 11 * 3600 + 50 * 60)
        await _ago(conn, token, "last_seen_at", 7 * 3600 + 59 * 60)
        used = await _touch(conn, token)
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT expires_at > NOW() + interval '7 hours 59 minutes', "
                "last_seen_at > NOW() - interval '1 minute' FROM web_sessions "
                "WHERE token_digest = %s", (web_access.digest(token),))
            renewed = await cur.fetchone()
        too_old = await _passkey_session(conn, n=1, rule="browser")
        await _ago(conn, too_old, "created_at", 12 * 3600 + 1)
        idle = await _passkey_session(conn, n=2, rule="browser")
        await _ago(conn, idle, "last_seen_at", 8 * 3600 + 1)
        await _ago(conn, idle, "expires_at", 1)
        return (opened, looked, used, tuple(renewed), await _touch(conn, too_old),
                await _touch(conn, idle))

    (rule, lifetime), looked, used, renewed, too_old, idle = _run(body)

    assert rule == "browser"
    assert abs(lifetime - 8 * 3600) < 5, "a browser session starts one idle window away"
    assert looked == (1, "passkey", "browser", False), "16 min without use, not locked"
    assert used == (1, "passkey", "browser", False), "past 5 h and 15 min, still open"
    assert renewed == (True, True)
    assert too_old is None and idle is None


@_needs_database
def test_phone_passkey_session_unchanged():
    """5 h from sign-in whatever happens, and locked after 15 minutes."""
    async def body(conn):
        token = await _passkey_session(conn)
        opened = await _rule_and_lifetime(conn, token)
        fresh = await _touch(conn, token)
        await _ago(conn, token, "last_seen_at", 15 * 60 + 1)
        locked = await _touch(conn, token)
        used = await _passkey_session(conn, n=1)
        await _ago(conn, used, "created_at", 5 * 3600 + 1)
        return opened, fresh, locked, await _touch(conn, used)

    (rule, lifetime), fresh, locked, ended = _run(body)

    assert rule == "app" and abs(lifetime - 5 * 3600) < 5
    assert fresh == (1, "passkey", "app", False)
    assert locked == (1, "passkey", "app", True)
    assert ended is None


@_needs_database
def test_app_rule_needs_a_passkey():
    """Neither the code nor the table lets a link session carry the app's
    rule, nor any rule that is not one of the two."""
    import psycopg

    async def body(conn):
        pid = await _add(conn, 1, 0)
        assert pid is not None
        odd: Any = "kiosk"
        refused_in_code = []
        for kwargs in ({"rule": "app", "absolute_seconds": 60.0},
                       {"method": "passkey", "passkey_id": pid, "rule": odd},
                       {"method": "passkey", "passkey_id": pid, "rule": "app"}):
            try:
                await web_access.open_session(conn, 1, idle_seconds=60, **kwargs)
            except ValueError:
                refused_in_code.append(True)
        refused_in_table = []
        for method, rule, passkey_id in (("link", "app", None),
                                         ("passkey", "kiosk", pid)):
            try:
                await _session_row(conn, f"{method}-{rule}", staff_id=1, method=method,
                                   passkey_id=passkey_id, rule=rule)
            except psycopg.errors.CheckViolation:
                await conn.rollback()
                refused_in_table.append(True)
        return refused_in_code, refused_in_table, await _count(conn, "web_sessions")

    assert _run(body) == ([True, True, True], [True, True], 0)


@_needs_database
def test_rule_migration_runs_once():
    """The passkey sessions open when the column arrives were all opened
    under the app rule, so they become 'app'; once the column exists, a
    restart must leave alone a computer's passkey session opened since."""
    async def body(conn):
        pid = await _add(conn, 1, 0)
        async with conn.cursor() as cur:
            await cur.execute("ALTER TABLE web_sessions DROP COLUMN rule")
            for token, method, passkey_id in (("phone", "passkey", pid),
                                              ("link", "link", None)):
                await cur.execute(
                    "INSERT INTO web_sessions (token_digest, staff_id, expires_at, "
                    "method, passkey_id) VALUES (%s, 1, NOW() + interval '1 hour', %s, %s)",
                    (web_access.digest(token), method, passkey_id))
        await conn.commit()

        async def rules() -> dict[str, str]:
            async with conn.cursor() as cur:
                await cur.execute("SELECT token_digest, rule FROM web_sessions")
                return dict(await cur.fetchall())

        await web_access.ensure_web_access_schema(conn)
        first = await rules()
        computer = await _passkey_session(conn, n=1, rule="browser")
        await web_access.ensure_web_access_schema(conn)
        second = await rules()
        await _ago(conn, "phone", "last_seen_at", 16 * 60)
        return first, second, computer, await _touch(conn, "phone")

    first, second, computer, phone = _run(body)

    by_name = {web_access.digest("phone"): "phone", web_access.digest("link"): "link",
               web_access.digest(computer): "computer"}
    assert {by_name[k]: v for k, v in first.items()} == {"phone": "app", "link": "browser"}
    assert {by_name[k]: v for k, v in second.items()} == {
        "phone": "app", "link": "browser", "computer": "browser"}
    assert phone == (1, "passkey", "app", True), "the migrated session still locks"


@_needs_database
def test_unlock_ignores_browser_sessions():
    """A computer's passkey session has no lock, so unlocking it moves
    nothing, however long it sat."""
    async def body(conn):
        token = await _passkey_session(conn, rule="browser")
        await _ago(conn, token, "last_seen_at", 16 * 60)
        before = await _clocks(conn, token)
        moved = await web_access.unlock_session(conn, token, 1)
        return moved, before == await _clocks(conn, token)

    moved, untouched = _run(body)

    assert moved is False
    assert untouched, "an unlock moved a browser session's clocks"


# ── confirmation: an enrolment counts once a code from it has worked ────────


@_needs_database
def test_an_enrolment_starts_unconfirmed_and_confirming_sticks():
    async def body(conn):
        await web_access.enrol(conn, 1, secret="JBSWY3DPEHPK3PXP")
        before = await web_access.enrolment_of(conn, 1)
        await web_access.confirm_enrolment(conn, 1)
        after = await web_access.enrolment_of(conn, 1)
        assert before is not None and after is not None
        return before.confirmed, after.confirmed

    assert _run(body) == (False, True)


@_needs_database
def test_enrolling_again_needs_confirming_again():
    """A new secret has not been shown to work yet, whatever the old one did."""
    async def body(conn):
        await web_access.enrol(conn, 1, secret="JBSWY3DPEHPK3PXP")
        await web_access.confirm_enrolment(conn, 1)
        await web_access.enrol(conn, 1, secret="KRSXG5CTMVRXEZLU")
        enrolment = await web_access.enrolment_of(conn, 1)
        assert enrolment is not None
        return enrolment.confirmed

    assert _run(body) is False


# ── the login link: once, briefly, and only the newest ──────────────────────


@_needs_database
def test_a_login_link_is_spent_once():
    async def body(conn):
        token = await web_access.open_login_link(conn, 1, ttl_seconds=300)
        return (await web_access.spend_login_link(conn, token),
                await web_access.spend_login_link(conn, token))

    assert _run(body) == (1, None)


@_needs_database
def test_looking_at_a_link_does_not_spend_it():
    """The form is rendered by a GET. If that spent the link, anything that
    fetches URLs ahead of the person would lock them out."""
    async def body(conn):
        token = await web_access.open_login_link(conn, 1, ttl_seconds=300)
        return (await web_access.peek_login_link(conn, token),
                await web_access.peek_login_link(conn, token),
                await web_access.spend_login_link(conn, token))

    assert _run(body) == (1, 1, 1)


@_needs_database
def test_a_spent_link_is_not_even_seen():
    async def body(conn):
        token = await web_access.open_login_link(conn, 1, ttl_seconds=300)
        await web_access.spend_login_link(conn, token)
        return await web_access.peek_login_link(conn, token)

    assert _run(body) is None


@_needs_database
def test_an_expired_link_does_nothing():
    async def body(conn):
        token = await web_access.open_login_link(conn, 1, ttl_seconds=300)
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE web_login_links SET expires_at = NOW() - interval '1 second'"
            )
        await conn.commit()
        return (await web_access.peek_login_link(conn, token),
                await web_access.spend_login_link(conn, token))

    assert _run(body) == (None, None)


@_needs_database
def test_asking_for_a_new_link_voids_the_old_one():
    """A link forwarded by mistake dies the moment its owner asks again."""
    async def body(conn):
        old = await web_access.open_login_link(conn, 1, ttl_seconds=300)
        new = await web_access.open_login_link(conn, 1, ttl_seconds=300)
        return (await web_access.spend_login_link(conn, old),
                await web_access.spend_login_link(conn, new))

    assert _run(body) == (None, 1)


@_needs_database
def test_one_persons_new_link_leaves_anothers_alone():
    async def body(conn):
        theirs = await web_access.open_login_link(conn, 2, ttl_seconds=300)
        await web_access.open_login_link(conn, 1, ttl_seconds=300)
        return await web_access.spend_login_link(conn, theirs)

    assert _run(body) == 2


@_needs_database
def test_the_link_token_is_stored_only_hashed():
    async def body(conn):
        token = await web_access.open_login_link(conn, 1, ttl_seconds=300)
        async with conn.cursor() as cur:
            await cur.execute("SELECT token_digest FROM web_login_links")
            stored = (await cur.fetchone())[0]
        return token, stored

    token, stored = _run(body)

    assert stored == web_access.digest(token) and token not in stored


@_needs_database
def test_the_daily_sweep_removes_dead_sessions_and_spent_links_only():
    async def body(conn):
        live = await web_access.open_session(conn, 1, idle_seconds=3600)
        dead = await web_access.open_session(conn, 2, idle_seconds=3600)
        spent = await web_access.open_login_link(conn, 1, ttl_seconds=300)
        await web_access.spend_login_link(conn, spent)
        waiting = await web_access.open_login_link(conn, 2, ttl_seconds=300)
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE web_sessions SET expires_at = NOW() - interval '1 second' "
                "WHERE token_digest = %s", (web_access.digest(dead),),
            )
        await conn.commit()
        removed = await web_access.purge_expired_sessions(conn)
        return (removed,
                await web_access.touch_session(conn, live, idle_seconds=3600,
                                               absolute_seconds=7200, **_APP),
                await web_access.peek_login_link(conn, waiting))

    assert _run(body) == (1, (1, "link", "browser", False), 2)


# ── the attempt lock ─────────────────────────────────────────────────────────


def _fail(conn, staff_id=1):
    return web_access.record_failure(conn, staff_id, limit=5, lock_seconds=900)


@_needs_database
def test_the_fifth_wrong_code_sets_the_lock_and_not_before():
    async def body(conn):
        await web_access.enrol(conn, 1, secret="JBSWY3DPEHPK3PXP")
        results = []
        for _ in range(5):
            results.append((await _fail(conn), await web_access.second_factor_locked(conn, 1)))
        return results

    results = _run(body)

    assert results[:4] == [(False, False)] * 4
    assert results[4] == (True, True)


@_needs_database
def test_a_right_code_clears_the_count():
    """Four typos on Monday and one on Tuesday is not five in a row."""
    async def body(conn):
        await web_access.enrol(conn, 1, secret="JBSWY3DPEHPK3PXP")
        for _ in range(4):
            await _fail(conn)
        await web_access.clear_failures(conn, 1)
        return await _fail(conn), await web_access.second_factor_locked(conn, 1)

    assert _run(body) == (False, False)


@_needs_database
def test_the_lock_ends_when_its_time_is_up():
    async def body(conn):
        await web_access.enrol(conn, 1, secret="JBSWY3DPEHPK3PXP")
        for _ in range(5):
            await _fail(conn)
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE staff_totp SET locked_until = NOW() - interval '1 second'"
            )
        await conn.commit()
        return await web_access.second_factor_locked(conn, 1)

    assert _run(body) is False


@_needs_database
def test_one_persons_lock_is_theirs_alone():
    async def body(conn):
        await web_access.enrol(conn, 1, secret="JBSWY3DPEHPK3PXP")
        await web_access.enrol(conn, 2, secret="KRSXG5CTMVRXEZLU")
        for _ in range(5):
            await _fail(conn, 1)
        return (await web_access.second_factor_locked(conn, 1),
                await web_access.second_factor_locked(conn, 2))

    assert _run(body) == (True, False)


@_needs_database
def test_wrong_codes_arriving_together_are_all_counted():
    """Counted in the UPDATE itself. Read, add and write back in Python, and
    five parallel guesses would each read zero and each write one."""
    import psycopg

    async def body(conn):
        await web_access.enrol(conn, 1, secret="JBSWY3DPEHPK3PXP")
        assert URL
        others = [await psycopg.AsyncConnection.connect(URL) for _ in range(5)]
        try:
            for other in others:
                async with other.cursor() as cur:
                    await cur.execute(_in_schema("SET search_path TO {schema}"))
                await other.commit()
            await asyncio.gather(*(_fail(other) for other in others))
        finally:
            for other in others:
                await other.close()
        return await web_access.second_factor_locked(conn, 1)

    assert _run(body) is True


# ── enrolling through a link ─────────────────────────────────────────────────


@_needs_database
def test_an_enrolment_through_a_spent_link_is_refused():
    """A wrong code spends the link and leaves the enrolment unconfirmed. A
    request that was already past the page's own look must not then write a
    new secret through that dead link: it is checked again in the write."""
    async def body(conn):
        token = await web_access.open_login_link(conn, 1, ttl_seconds=300)
        first = await web_access.enrol_through_link(conn, token, secret="JBSWY3DPEHPK3PXP")
        await web_access.spend_login_link(conn, token)
        late = await web_access.enrol_through_link(conn, token, secret="GEZDGNBVGY3TQOJQ")
        return first, late, await web_access.secret_of(conn, 1)

    first, late, secret = _run(body)

    assert first is not None and first.codes
    assert late is None
    assert secret == "JBSWY3DPEHPK3PXP", "a dead link replaced the secret"


class _Paused:
    """A connection that runs `between` right after the statement reading the
    link, while that statement's transaction is still open."""

    def __init__(self, conn, between):
        self._conn, self._between = conn, between

    def cursor(self):
        return _PausedCursor(self._conn.cursor(), self._between)

    async def commit(self):
        await self._conn.commit()

    async def rollback(self):
        await self._conn.rollback()


class _PausedCursor:
    def __init__(self, cursor, between):
        self._cursor, self._between = cursor, between

    async def __aenter__(self):
        await self._cursor.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._cursor.__aexit__(*exc)

    async def execute(self, query, params=None):
        await self._cursor.execute(query, params)
        if "FROM web_login_links" in str(query):
            await self._between()

    async def fetchone(self):
        return await self._cursor.fetchone()


@_needs_database
def test_spending_a_link_waits_for_an_enrolment_through_it():
    """The link row is locked from the moment the enrolment reads it. A code
    posted meanwhile spends the link only once the enrolment has finished,
    so it is checked against the secret that was actually shown, and the
    enrolment never lands through a link already spent."""
    import psycopg

    async def body(conn):
        token = await web_access.open_login_link(conn, 1, ttl_seconds=300)
        assert URL
        other = await psycopg.AsyncConnection.connect(URL)
        seen = []
        try:
            async with other.cursor() as cur:
                await cur.execute(_in_schema("SET search_path TO {schema}"))
                await cur.execute("SET lock_timeout = '300ms'")
            await other.commit()

            async def spend_meanwhile():
                try:
                    seen.append(await web_access.spend_login_link(other, token))
                except psycopg.errors.LockNotAvailable:
                    await other.rollback()
                    seen.append("waited")

            enrolled = await web_access.enrol_through_link(
                _Paused(conn, spend_meanwhile), token, secret="JBSWY3DPEHPK3PXP")
            after = await web_access.spend_login_link(other, token)
        finally:
            await other.close()
        return seen, enrolled, after

    seen, enrolled, after = _run(body)

    assert seen == ["waited"], "the link was spent while an enrolment held it"
    assert enrolled is not None and enrolled.codes
    assert after == 1, "the lock outlived the enrolment"


# ── passkeys and their challenges ────────────────────────────────────────────


def _cred(n: int) -> dict:
    """The fields of a passkey that differ between two of them."""
    return {"credential_id": bytes([n]) * 16, "public_key": b"key" + bytes([n]),
            "sign_count": 0}


async def _add(conn, staff_id: int, n: int, *, handle: bytes | None = None, **extra):
    handle = handle or await web_access.user_handle_for(conn, staff_id)
    return await web_access.add_passkey(
        conn, staff_id, user_handle=handle, **{**_cred(n), **extra})


async def _count(conn, table: LiteralString, where: LiteralString = "TRUE") -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            sql.SQL("SELECT count(*) FROM {table} WHERE ").format(
                table=sql.Identifier(table)) + sql.SQL(where))
        return (await cur.fetchone())[0]


async def _session_row(conn, token: str, *, staff_id: int, method: str,
                       passkey_id: int | None = None, rule: str = "browser") -> None:
    """A session as the passkey sign-in will open it, written directly: this
    task stores the columns, the sign-in that fills them comes later."""
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO web_sessions (token_digest, staff_id, expires_at, method, "
            "passkey_id, rule) VALUES (%s, %s, NOW() + interval '1 hour', %s, %s, %s)",
            (web_access.digest(token), staff_id, method, passkey_id, rule),
        )
    await conn.commit()


async def _other_connection():
    """A second connection in this run's schema, for two requests at once."""
    import psycopg

    assert URL
    other = await psycopg.AsyncConnection.connect(URL)
    async with other.cursor() as cur:
        await cur.execute(_in_schema("SET search_path TO {schema}"))
    await other.commit()
    return other


@_needs_database
def test_sixth_passkey_is_refused():
    async def body(conn):
        ids = [await _add(conn, 1, n) for n in range(5)]
        sixth = await _add(conn, 1, 5)
        elsewhere = await _add(conn, 2, 6)
        return ids, sixth, elsewhere, await _count(conn, "staff_passkeys", "staff_id = 1")

    ids, sixth, elsewhere, kept = _run(body)

    assert all(isinstance(i, int) for i in ids)
    assert sixth is None and kept == 5
    assert isinstance(elsewhere, int), "the limit is per person"


@_needs_database
def test_user_handle_is_reused_and_never_the_staff_id():
    async def body(conn):
        first = await web_access.user_handle_for(conn, 1)
        await _add(conn, 1, 0, handle=first)
        again = await web_access.user_handle_for(conn, 1)
        other = await web_access.user_handle_for(conn, 2)
        return first, again, other

    first, again, other = _run(body)

    assert again == first, "one handle per person, from the first passkey on"
    assert len(first) == 32 and first != other
    assert first not in (b"1", (1).to_bytes(32, "big"), (1).to_bytes(4, "big"))


@_needs_database
def test_a_second_passkey_takes_the_handle_the_first_one_stored():
    """Two first enrolments could each have drawn a handle before either had
    stored one. The write, under the person's lock, settles it."""
    async def body(conn):
        await _add(conn, 1, 0, handle=b"a" * 32)
        await _add(conn, 1, 1, handle=b"b" * 32)
        async with conn.cursor() as cur:
            await cur.execute("SELECT DISTINCT user_handle FROM staff_passkeys")
            return [bytes(r[0]) for r in await cur.fetchall()]

    assert _run(body) == [b"a" * 32]


@_needs_database
def test_a_passkey_is_found_by_credential_and_its_use_is_recorded():
    async def body(conn):
        pid = await _add(conn, 1, 3)
        assert pid is not None
        before = await web_access.passkey_by_credential(conn, _cred(3)["credential_id"])
        assert await web_access.record_use(conn, pid, 7, previous=0)
        after = (await web_access.passkeys_of(conn, 1))[0]
        return (before, after, pid,
                await web_access.passkey_by_credential(conn, b"nobody"),
                await web_access.passkeys_of(conn, 2))

    before, after, pid, nobody, none_for_ana = _run(body)

    assert before is not None and before.id == pid and before.staff_id == 1
    assert before.public_key == b"key\x03" and before.last_used_at is None
    assert after.sign_count == 7 and after.last_used_at is not None
    assert nobody is None and none_for_ana == []


@_needs_database
def test_a_challenge_is_stored_only_hashed():
    async def body(conn):
        challenge = await web_access.issue_challenge(conn, "signin")
        async with conn.cursor() as cur:
            await cur.execute("SELECT challenge_digest FROM webauthn_challenges")
            return challenge, (await cur.fetchone())[0]

    challenge, stored = _run(body)

    assert challenge is not None and len(challenge) == 32
    assert challenge.hex() not in stored and stored != challenge.hex()


@_needs_database
def test_a_challenge_is_spent_once():
    async def body(conn):
        challenge = await web_access.issue_challenge(
            conn, "register", staff_id=1, session_token="tok")
        assert challenge is not None
        first = await web_access.spend_challenge(
            conn, challenge, "register", session_token="tok")
        second = await web_access.spend_challenge(
            conn, challenge, "register", session_token="tok")
        return first, second

    first, second = _run(body)

    assert first == web_access.SpentChallenge(staff_id=1)
    assert second is None


@_needs_database
def test_the_signin_challenge_names_nobody_when_spent():
    async def body(conn):
        challenge = await web_access.issue_challenge(conn, "signin")
        assert challenge is not None
        return await web_access.spend_challenge(conn, challenge, "signin")

    assert _run(body) == web_access.SpentChallenge(staff_id=None)


@_needs_database
def test_an_expired_challenge_is_refused():
    async def body(conn):
        challenge = await web_access.issue_challenge(conn, "signin")
        assert challenge is not None
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE webauthn_challenges SET expires_at = NOW() - interval '1 second'")
        await conn.commit()
        return await web_access.spend_challenge(conn, challenge, "signin")

    assert _run(body) is None


@_needs_database
def test_a_challenge_answered_from_another_session_is_refused():
    async def body(conn):
        challenge = await web_access.issue_challenge(
            conn, "unlock", staff_id=1, session_token="mine")
        assert challenge is not None
        stolen = await web_access.spend_challenge(
            conn, challenge, "unlock", session_token="theirs")
        anonymous = await web_access.spend_challenge(conn, challenge, "unlock")
        rightful = await web_access.spend_challenge(
            conn, challenge, "unlock", session_token="mine")
        return stolen, anonymous, rightful

    stolen, anonymous, rightful = _run(body)

    assert stolen is None and anonymous is None
    assert rightful == web_access.SpentChallenge(staff_id=1), (
        "a refused answer must not have spent it")


@_needs_database
def test_a_challenge_of_another_purpose_is_refused():
    async def body(conn):
        challenge = await web_access.issue_challenge(
            conn, "unlock", staff_id=1, session_token="tok")
        assert challenge is not None
        wrong = await web_access.spend_challenge(
            conn, challenge, "register", session_token="tok")
        right = await web_access.spend_challenge(
            conn, challenge, "unlock", session_token="tok")
        return wrong, right

    wrong, right = _run(body)

    assert wrong is None and right is not None


@_needs_database
def test_a_register_or_unlock_challenge_needs_its_person_and_session():
    async def body(conn):
        refused = []
        for purpose, call in (
            ("register", web_access.issue_challenge(conn, "register")),
            ("unlock", web_access.issue_challenge(conn, "unlock", staff_id=1)),
            ("register", web_access.issue_challenge(conn, "register", session_token="t")),
        ):
            try:
                await call
            except ValueError:
                refused.append(purpose)
        return refused, await _count(conn, "webauthn_challenges")

    assert _run(body) == (["register", "unlock", "register"], 0)


@_needs_database
def test_signin_challenges_are_capped():
    async def body(conn):
        issued = [await web_access.issue_challenge(conn, "signin") for _ in range(100)]
        over = await web_access.issue_challenge(conn, "signin")
        rows = await _count(conn, "webauthn_challenges")
        # Other purposes are not part of this count, and neither is a spent one.
        register = await web_access.issue_challenge(
            conn, "register", staff_id=1, session_token="tok")
        await web_access.spend_challenge(conn, issued[0] or b"", "signin")
        room = await web_access.issue_challenge(conn, "signin")
        return issued, over, rows, register, room

    issued, over, rows, register, room = _run(body)

    assert all(c is not None for c in issued)
    assert over is None and rows == 100
    assert register is not None and room is not None


@_needs_database
def test_dead_signin_challenges_are_cleared_before_counting():
    """Unauthenticated requests make these rows, and every one counts the
    table under a global lock: dead rows must not wait for the daily sweep."""
    async def body(conn):
        async with conn.cursor() as cur:
            for n in range(30):
                await cur.execute(
                    "INSERT INTO webauthn_challenges (challenge_digest, purpose, "
                    "expires_at, used_at) VALUES (%s, 'signin', "
                    "NOW() - interval '1 minute', NULL)", (f"{n:064d}",))
            for n in range(30, 60):
                await cur.execute(
                    "INSERT INTO webauthn_challenges (challenge_digest, purpose, "
                    "expires_at, used_at) VALUES (%s, 'signin', "
                    "NOW() + interval '1 minute', NOW())", (f"{n:064d}",))
            await cur.execute(
                "INSERT INTO webauthn_challenges (challenge_digest, staff_id, purpose, "
                "session_digest, expires_at) VALUES (%s, 1, 'register', %s, "
                "NOW() - interval '1 minute')", ("f" * 64, "e" * 64))
        await conn.commit()
        fresh = await web_access.issue_challenge(conn, "signin")
        return (fresh is not None, await _count(conn, "webauthn_challenges"),
                await _count(conn, "webauthn_challenges", "purpose = 'register'"))

    assert _run(body) == (True, 2, 1), "only the new row and the other purpose remain"


@_needs_database
def test_concurrent_enrolments_cannot_pass_five():
    """A snapshot alone does not stop it: both count four, both insert."""
    async def body(conn):
        for n in range(4):
            assert await _add(conn, 1, n) is not None
        handle = await web_access.user_handle_for(conn, 1)
        other = await _other_connection()
        try:
            results = await asyncio.gather(
                web_access.add_passkey(conn, 1, user_handle=handle, **_cred(10)),
                web_access.add_passkey(other, 1, user_handle=handle, **_cred(11)),
            )
        finally:
            await other.close()
        return results, await _count(conn, "staff_passkeys", "staff_id = 1")

    results, kept = _run(body)

    assert sorted(r is None for r in results) == [False, True]
    assert kept == 5


@_needs_database
def test_two_first_enrolments_draw_one_handle():
    """A person with no passkey yet, asked for twice at once: both ceremonies
    must hand the device the same handle, or the second passkey is filed
    under another account than the one stored for it."""
    async def body(conn):
        other = await _other_connection()
        try:
            handles = await asyncio.gather(web_access.user_handle_for(conn, 1),
                                           web_access.user_handle_for(other, 1))
        finally:
            await other.close()
        pid = await _add(conn, 1, 0)
        async with conn.cursor() as cur:
            await cur.execute("SELECT user_handle FROM staff_passkeys WHERE id = %s", (pid,))
            stored = bytes((await cur.fetchone())[0])
        return handles, stored, await web_access.user_handle_for(conn, 1)

    handles, stored, later = _run(body)

    assert handles[0] == handles[1]
    assert len(handles[0]) == 32
    assert stored == handles[0] == later


@_needs_database
def test_a_use_is_recorded_only_against_the_count_it_was_checked_against():
    """Two answers verified against the same stored count (5), each bringing
    6: whichever writes second finds the count moved and is refused, and the
    count is never lowered by an answer that arrives late."""
    async def body(conn):
        pid = await _add(conn, 1, 0, sign_count=5)
        assert pid is not None
        other = await _other_connection()
        try:
            first = await web_access.record_use(conn, pid, 6, previous=5, commit=False)
            # The second waits on the first's row lock, then sees its write.
            waiting = asyncio.create_task(
                web_access.record_use(other, pid, 6, previous=5))
            await asyncio.sleep(0.2)
            await conn.commit()
            second = await waiting
            late = await web_access.record_use(other, pid, 5, previous=5)
        finally:
            await other.close()
        return first, second, late, (await web_access.passkeys_of(conn, 1))[0].sign_count

    first, second, late, count = _run(body)

    assert (first, second, late) == (True, False, False)
    assert count == 6


@_needs_database
def test_concurrent_signin_challenges_cannot_pass_the_cap():
    async def body(conn):
        for _ in range(99):
            assert await web_access.issue_challenge(conn, "signin") is not None
        other = await _other_connection()
        try:
            results = await asyncio.gather(
                web_access.issue_challenge(conn, "signin"),
                web_access.issue_challenge(other, "signin"),
            )
        finally:
            await other.close()
        return results, await _count(conn, "webauthn_challenges")

    results, rows = _run(body)

    assert sorted(r is None for r in results) == [False, True]
    assert rows == 100


@_needs_database
def test_enrolment_window_closes_after_ten_minutes():
    async def body(conn):
        token = await web_access.open_session(conn, 1, idle_seconds=3600)
        fresh = await web_access.enrolment_window_open(conn, token)
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE web_sessions SET created_at = NOW() - interval '590 seconds'")
        await conn.commit()
        inside = await web_access.enrolment_window_open(conn, token)
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE web_sessions SET created_at = NOW() - interval '601 seconds'")
        await conn.commit()
        return (fresh, inside, await web_access.enrolment_window_open(conn, token),
                await web_access.enrolment_window_open(conn, token, window_seconds=700),
                await web_access.enrolment_window_open(conn, "not-a-session"))

    assert _run(body) == (1, 1, None, 1, None)


@_needs_database
def test_enrolment_window_is_shut_to_a_passkey_session():
    async def body(conn):
        pid = await _add(conn, 1, 0)
        await _session_row(conn, "by-passkey", staff_id=1, method="passkey", passkey_id=pid)
        token = await web_access.open_session(conn, 1, idle_seconds=3600)
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE web_sessions SET expires_at = NOW() - interval '1 second' "
                "WHERE token_digest = %s", (web_access.digest(token),))
        await conn.commit()
        return (await web_access.enrolment_window_open(conn, "by-passkey"),
                await web_access.enrolment_window_open(conn, token))

    assert _run(body) == (None, None), "a passkey session, and a link session that expired"


@_needs_database
def test_a_passkey_session_must_name_its_passkey():
    import psycopg

    async def body(conn):
        try:
            await _session_row(conn, "orphan", staff_id=1, method="passkey")
        except psycopg.errors.CheckViolation:
            await conn.rollback()
            refused = True
        else:
            refused = False
        try:
            await _session_row(conn, "odd", staff_id=1, method="telepathy")
        except psycopg.errors.CheckViolation:
            await conn.rollback()
            odd = True
        else:
            odd = False
        return refused, odd, await _count(conn, "web_sessions")

    assert _run(body) == (True, True, 0)


@_needs_database
def test_removing_a_passkey_ends_its_sessions():
    async def body(conn):
        a, b = await _add(conn, 1, 0), await _add(conn, 1, 1)
        assert a is not None and b is not None
        await _session_row(conn, "on-a", staff_id=1, method="passkey", passkey_id=a)
        await _session_row(conn, "on-b", staff_id=1, method="passkey", passkey_id=b)
        link = await web_access.open_session(conn, 1, idle_seconds=3600)
        removed = await web_access.remove_passkey(conn, 1, a)
        alive = {}
        for name, token in (("a", "on-a"), ("b", "on-b"), ("link", link)):
            alive[name] = await web_access.touch_session(
                conn, token, idle_seconds=3600, absolute_seconds=7200, **_APP)
        return (removed, {k: t and t.staff_id for k, t in alive.items()},
                [p.id for p in await web_access.passkeys_of(conn, 1)], b)

    removed, alive, left, b = _run(body)

    assert removed is True
    assert alive == {"a": None, "b": 1, "link": 1}
    assert left == [b]


@_needs_database
def test_remove_passkey_touches_only_ones_own():
    async def body(conn):
        pid = await _add(conn, 1, 0)
        assert pid is not None
        await _session_row(conn, "mine", staff_id=1, method="passkey", passkey_id=pid)
        refused = await web_access.remove_passkey(conn, 2, pid)
        return (refused, await _count(conn, "staff_passkeys"),
                await _count(conn, "web_sessions"),
                await web_access.remove_passkey(conn, 1, 9999))

    assert _run(body) == (False, 1, 1, False)


@_needs_database
def test_revoke_devices_removes_passkeys_and_sessions():
    async def body(conn):
        for who, base in ((1, 0), (2, 5)):
            pid = await _add(conn, who, base)
            await _session_row(conn, f"pk{who}", staff_id=who, method="passkey",
                               passkey_id=pid)
            await web_access.open_session(conn, who, idle_seconds=3600)
        await web_access.revoke_devices(conn, 1)
        return (await _count(conn, "staff_passkeys", "staff_id = 1"),
                await _count(conn, "web_sessions", "staff_id = 1"),
                await _count(conn, "staff_passkeys", "staff_id = 2"),
                await _count(conn, "web_sessions", "staff_id = 2"))

    assert _run(body) == (0, 0, 1, 2)


@_needs_database
def test_purge_sweeps_dead_challenges():
    async def body(conn):
        live = await web_access.issue_challenge(conn, "signin")
        spent = await web_access.issue_challenge(conn, "signin")
        expired = await web_access.issue_challenge(conn, "signin")
        assert live and spent and expired
        await web_access.spend_challenge(conn, spent, "signin")
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE webauthn_challenges SET expires_at = NOW() - interval '1 second' "
                "WHERE challenge_digest = %s",
                (hashlib.sha256(expired).hexdigest(),))
        await conn.commit()
        await web_access.purge_expired_sessions(conn)
        return (await _count(conn, "webauthn_challenges"),
                await web_access.spend_challenge(conn, live, "signin"))

    count, still = _run(body)

    assert count == 1 and still is not None


@_needs_database
def test_the_new_tables_and_columns_reach_an_old_database():
    """init.sql runs only on an empty data directory (lesson 17): a database
    whose web_sessions predates the passkeys gets them from the startup path,
    and a restart runs it again."""
    import psycopg

    async def body(conn):
        async with conn.cursor() as cur:
            await cur.execute("DROP TABLE push_subscriptions")
            await cur.execute("DROP TABLE webauthn_challenges")
            await cur.execute("ALTER TABLE web_sessions DROP COLUMN passkey_id, "
                              "DROP COLUMN method, DROP COLUMN rule")
            await cur.execute("DROP TABLE staff_passkeys")
            await cur.execute(
                "INSERT INTO web_sessions (token_digest, staff_id, expires_at) "
                "VALUES (%s, 1, NOW() + interval '1 hour')", (web_access.digest("old"),))
        await conn.commit()
        await web_access.ensure_web_access_schema(conn)
        await web_access.ensure_web_access_schema(conn)
        async with conn.cursor() as cur:
            await cur.execute("SELECT method, passkey_id FROM web_sessions")
            old = await cur.fetchone()
        try:
            await _session_row(conn, "orphan", staff_id=1, method="passkey")
            refused = False
        except psycopg.errors.CheckViolation:
            await conn.rollback()
            refused = True
        pid = await _add(conn, 1, 0)
        await web_access.issue_challenge(conn, "signin")
        async with conn.cursor() as cur:
            await cur.execute("SELECT to_regclass('push_subscriptions') IS NOT NULL")
            subscriptions = await cur.fetchone()
        return old, refused, pid is not None, subscriptions

    assert _run(body) == (("link", None), True, True, (True,))
