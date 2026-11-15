"""The web login, driven through its routes against a real Postgres.

Each piece has its own tests: the TOTP arithmetic, the SQL, the shared check.
This file is the seam between them, which is where a login breaks in ways no
unit test sees: a link spent by looking at it, a secret shown on a GET that
Telegram's crawler would read, a recovery code accepted before the app ever
worked, a cookie that a sibling subdomain could overwrite.

The secret and the recovery codes are read off the page the way a person
would read them, so the test enrols exactly as somebody at the firm would.

Skipped without TEST_DATABASE_URL, like test_web_access_sql.py, and run in CI
where the database is provided.
"""
import asyncio
import contextlib
import os
import re
import uuid

from typing import Literal, LiteralString

import pytest
from psycopg import sql
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import totp
from core.config import settings
from core.models import StaffMember
from core.repositories import web_access
from core.services import second_factor
from core.web import staff_web


# A signed-in, unlocked session is served at /web/: shown the page when it
# has something to say (the Face ID offer), else sent where the work is. A
# refused one is answered (401) or sent to unlock (to Face ID).
_WORK = ("/web/assistente/#escrever", "/web/explorar/", "/web/arquivos/")


def _served(response) -> bool:
    if response.status_code == 200:
        return "Olá, " in response.text
    return response.status_code == 303 and response.headers["location"] in _WORK

URL = os.environ.get("TEST_DATABASE_URL")
# Unique per test process. Two runs against one database, a second terminal
# or another agent, would otherwise drop and rebuild each other's schema
# mid-test: a login test then met a secret it never enrolled.
SCHEMA = f"webflow_{uuid.uuid4().hex[:12]}"


def _in_schema(statement: LiteralString) -> sql.Composed:
    """A statement naming this run's schema, quoted as an identifier."""
    return sql.SQL(statement).format(schema=sql.Identifier(SCHEMA))

pytestmark = pytest.mark.skipif(
    not URL, reason="needs a real Postgres: set TEST_DATABASE_URL",
)

_PEOPLE = {
    1: StaffMember(id=1, chat_id="100", name="Paulo", role="owner"),
    4: StaffMember(id=4, chat_id="400", name="Rui", role="advogado"),
}


class _Clock:
    """Stands in for the time module inside core.totp: a fixed moment."""

    def time(self) -> float:
        return 1_790_000_015.0


async def _connect():
    import psycopg

    assert URL
    conn = await psycopg.AsyncConnection.connect(URL)
    async with conn.cursor() as cur:
        await cur.execute(_in_schema("SET search_path TO {schema}"))
    await conn.commit()
    return conn


class _Pool:
    """A fresh connection per request. TestClient runs each request on its
    own loop, and a psycopg connection belongs to the loop that opened it."""

    @contextlib.asynccontextmanager
    async def connection(self):
        conn = await _connect()
        try:
            yield conn
        finally:
            await conn.close()


def _db(fn):
    async def main():
        conn = await _connect()
        try:
            return await fn(conn)
        finally:
            await conn.close()
    return asyncio.run(main())


@pytest.fixture
def web(monkeypatch):
    import psycopg

    async def build():
        assert URL
        async with await psycopg.AsyncConnection.connect(URL) as conn:
            async with conn.cursor() as cur:
                await cur.execute(_in_schema("DROP SCHEMA IF EXISTS {schema} CASCADE"))
                await cur.execute(_in_schema("CREATE SCHEMA {schema}"))
                await cur.execute(_in_schema("SET search_path TO {schema}"))
                await cur.execute("CREATE TABLE staff (id INTEGER PRIMARY KEY, name TEXT)")
                await cur.execute("INSERT INTO staff VALUES (1, 'Paulo'), (4, 'Rui')")
            await conn.commit()
            await web_access.ensure_web_access_schema(conn)

    async def drop():
        assert URL
        async with await psycopg.AsyncConnection.connect(URL) as conn:
            async with conn.cursor() as cur:
                await cur.execute(_in_schema("DROP SCHEMA IF EXISTS {schema} CASCADE"))
            await conn.commit()

    asyncio.run(build())

    people = dict(_PEOPLE)
    told = []

    async def get_pool():
        return _Pool()

    async def get_staff_by_id(_conn, staff_id):
        return people.get(staff_id)

    async def list_staff(_conn, roles=None):
        return [p for p in people.values() if p.role in (roles or ())]

    async def send_message(chat_id, text, **_kwargs):
        told.append((str(chat_id), text))
        return 1

    monkeypatch.setattr(settings, "web_totp_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "web_enabled", True)
    # The codes follow a clock the test owns. The machine's clock can be
    # stepped by a time sync mid-test, and a code meant for the next step
    # then lands on one already used.
    monkeypatch.setattr(totp, "time", _Clock())
    monkeypatch.setattr(staff_web, "get_pool", get_pool)
    monkeypatch.setattr(staff_web, "get_staff_by_id", get_staff_by_id)
    monkeypatch.setattr(second_factor, "get_staff_by_id", get_staff_by_id)
    monkeypatch.setattr(second_factor, "list_staff", list_staff)
    monkeypatch.setattr(second_factor.telegram, "send_message", send_message)

    app = FastAPI()
    app.include_router(staff_web.router)
    staff_web.install(app)
    # What every browser in use sends with its own forms. The tests that need
    # a browser which sends nothing take it off for one request.
    client = TestClient(app, base_url="https://testserver", follow_redirects=False,
                        headers={"Sec-Fetch-Site": "same-origin"})
    client.people = people  # type: ignore[attr-defined]
    client.told = told  # type: ignore[attr-defined]
    try:
        yield client
    finally:
        client.close()
        asyncio.run(drop())


def _link(staff_id: int = 4) -> str:
    return _db(lambda conn: web_access.open_login_link(conn, staff_id, ttl_seconds=300))


def _enrol(web, token: str) -> tuple[str, list[str]]:
    """Press the button and read the secret and codes off the page."""
    page = web.post(f"/web/login/{token}/enrol").text
    grouped = re.search(r"font-size:1\.15rem[^>]*>([A-Z2-7 ]+)</code>", page)
    assert grouped, "the secret is not on the enrolment page"
    codes = re.findall(r"<li><code>([^<]+)</code></li>", page)
    return grouped.group(1).replace(" ", ""), codes


def _code(secret: str, steps_ahead: int = 0) -> str:
    """A code the app would show now, or a step later, which the drift window
    accepts. A later step is how a test signs in twice in one step without
    tripping the replay guard."""
    return totp.code_for_counter(secret, totp.counter_at() + steps_ahead)


def _signed_in(web, secret: str, steps_ahead: int = 0, staff_id: int = 4):
    token = _link(staff_id)
    return web.post(f"/web/login/{token}", data={"code": _code(secret, steps_ahead)})


def _enrolled(web) -> tuple[str, list[str]]:
    token = _link()
    secret, codes = _enrol(web, token)
    response = web.post(f"/web/login/{token}", data={"code": _code(secret)})
    assert response.status_code == 303
    web.cookies.clear()
    return secret, codes


# ── the link ─────────────────────────────────────────────────────────────────


def test_an_unknown_link_is_refused(web):
    assert web.get("/web/login/nao-existe").status_code == 404


def test_every_route_is_closed_while_the_pages_are_off(web, monkeypatch):
    token = _link()
    monkeypatch.setattr(settings, "web_enabled", False)

    assert web.get(f"/web/login/{token}").status_code == 404
    assert web.post(f"/web/login/{token}/enrol").status_code == 404
    assert web.post(f"/web/login/{token}", data={"code": "123456"}).status_code == 404
    assert web.get("/web/").status_code == 404


def test_opening_the_link_does_not_spend_it(web):
    """Link previews and prefetchers make GETs. If a GET spent the link, the
    person would find it dead before they had opened it."""
    token = _link()

    web.get(f"/web/login/{token}")
    web.get(f"/web/login/{token}")

    assert web.get(f"/web/login/{token}").status_code == 200


def test_the_pages_carry_the_shared_security_headers(web):
    response = web.get(f"/web/login/{_link()}")

    assert "no-store" in response.headers["cache-control"]
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"


# ── enrolment ────────────────────────────────────────────────────────────────


def test_the_first_visit_offers_enrolment_without_showing_a_secret(web):
    """The GET is what a crawler sees. The secret must not be on it."""
    page = web.get(f"/web/login/{_link()}").text

    assert "Configurar agora" in page
    assert "otpauth://" not in page
    assert "font-size:1.15rem" not in page


def test_enrolment_shows_the_secret_and_eight_codes_once(web):
    token = _link()

    secret, codes = _enrol(web, token)

    assert len(secret) >= 32
    assert len(codes) == web_access.RECOVERY_CODES


def test_enrolling_does_not_spend_the_link(web):
    """The link is spent by the first code, which proves the secret reached
    the app. Spending it earlier would leave the person enrolled on a page
    they can no longer confirm from."""
    token = _link()
    _enrol(web, token)

    assert _db(lambda conn: web_access.peek_login_link(conn, token)) == 4


def test_the_first_code_confirms_and_signs_in(web):
    token = _link()
    secret, _ = _enrol(web, token)

    response = web.post(f"/web/login/{token}", data={"code": _code(secret)})

    assert response.status_code == 303
    assert response.headers["location"] == "/web/"
    enrolment = _db(lambda conn: web_access.enrolment_of(conn, 4))
    assert enrolment is not None and enrolment.confirmed


def test_a_recovery_code_cannot_stand_in_before_the_app_has_worked(web):
    token = _link()
    _, codes = _enrol(web, token)

    response = web.post(f"/web/login/{token}", data={"code": codes[0]})

    assert response.status_code == 401
    enrolment = _db(lambda conn: web_access.enrolment_of(conn, 4))
    assert enrolment is not None and not enrolment.confirmed


def test_a_confirmed_enrolment_is_not_replaced_from_a_login_link(web):
    """Starting over is /mfa in the bot, where it asks for a code. A second
    visit to a login link must not be a way around that."""
    secret, _ = _enrolled(web)

    page = web.post(f"/web/login/{_link()}/enrol").text

    assert "font-size:1.15rem" not in page, "a new secret was shown"
    assert _db(lambda conn: web_access.secret_of(conn, 4)) == secret


def test_a_confirmed_person_is_asked_for_a_code_not_offered_enrolment(web):
    _enrolled(web)

    page = web.get(f"/web/login/{_link()}").text

    assert "Configurar agora" not in page
    assert "name=code" in page


def test_an_enrolment_never_confirmed_is_offered_again(web):
    """Somebody who closed the tab before saving the secret has nothing that
    can produce a code. Asking them for one would lock them out for good."""
    _enrol(web, _link())

    page = web.get(f"/web/login/{_link()}").text

    assert "Configurar agora" in page


# ── signing in ───────────────────────────────────────────────────────────────


def test_the_session_cookie_is_host_bound_and_out_of_scripts_reach(web):
    """`__Host-` stops a sibling subdomain, which this domain has, from
    planting or overwriting the cookie."""
    secret, _ = _enrolled(web)

    response = _signed_in(web, secret, steps_ahead=1)

    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-session=")
    lowered = cookie.casefold()
    assert "httponly" in lowered and "secure" in lowered
    assert "samesite=strict" in lowered and "path=/" in lowered
    assert "domain=" not in lowered


def test_a_signed_in_person_is_served(web):
    secret, _ = _enrolled(web)
    _signed_in(web, secret, steps_ahead=1)

    assert _served(web.get("/web/"))


def _opened_from_the_icon(web, monkeypatch, staff_id: int):
    """Enrolled and signed in as this person, then the manifest's start_url."""
    monkeypatch.setattr(settings, "web_assistant", "pilot")
    monkeypatch.setattr(settings, "web_assistant_staff", "1")
    token = _link(staff_id)
    secret, _ = _enrol(web, token)
    web.post(f"/web/login/{token}", data={"code": _code(secret)})
    return web.get("/web/?app=1")


def test_app_entry_lands_on_the_assistant_for_whoever_has_it(web, monkeypatch):
    response = _opened_from_the_icon(web, monkeypatch, 1)

    assert response.status_code == 303
    assert response.headers["location"] == "/web/assistente/#escrever"


def test_app_entry_shows_home_to_anyone_else(web, monkeypatch):
    response = _opened_from_the_icon(web, monkeypatch, 4)

    assert _served(response)


def test_a_wrong_code_burns_the_link(web):
    """A link that survived a wrong code would be a place to try codes."""
    secret, _ = _enrolled(web)
    token = _link()

    wrong = web.post(f"/web/login/{token}", data={"code": "000000"})
    right = web.post(f"/web/login/{token}", data={"code": _code(secret, 1)})

    assert wrong.status_code == 401
    assert right.status_code == 404


def test_the_same_code_does_not_sign_in_twice(web):
    secret, _ = _enrolled(web)
    # Computed once: recomputing could land on the next step if the clock
    # turned between the two posts, and then the test would pass for the
    # wrong reason, or fail for none.
    code = _code(secret, 1)

    first = web.post(f"/web/login/{_link()}", data={"code": code})
    again = web.post(f"/web/login/{_link()}", data={"code": code})

    assert first.status_code == 303
    assert again.status_code == 401


def test_a_recovery_code_signs_in_once(web):
    _, codes = _enrolled(web)

    first = web.post(f"/web/login/{_link()}", data={"code": codes[0]})
    again = web.post(f"/web/login/{_link()}", data={"code": codes[0]})

    assert first.status_code == 303
    assert again.status_code == 401


def test_an_empty_submit_is_not_counted_as_a_guess(web):
    """Not a guess, so it does not bring the lock nearer."""
    _enrolled(web)

    response = web.post(f"/web/login/{_link()}", data={"code": "  "})

    assert response.status_code == 401

    async def failures(conn):
        async with conn.cursor() as cur:
            await cur.execute("SELECT failures FROM staff_totp WHERE staff_id = 4")
            return (await cur.fetchone())[0]

    assert _db(failures) == 0


def test_the_fifth_wrong_code_locks_even_a_right_one_out(web):
    secret, _ = _enrolled(web)

    verdicts = [
        web.post(f"/web/login/{_link()}", data={"code": "000000"}).status_code
        for _ in range(second_factor.MAX_FAILURES)
    ]
    locked = _signed_in(web, secret, steps_ahead=1)

    assert verdicts[:-1] == [401] * (second_factor.MAX_FAILURES - 1)
    assert verdicts[-1] == 429
    assert locked.status_code == 429
    assert any(chat == "100" for chat, _ in web.told), "the owner was not told"


# ── the session afterwards ───────────────────────────────────────────────────


def test_no_cookie_no_page(web):
    assert web.get("/web/").status_code == 401


def test_a_deactivated_person_is_out_on_the_next_click(web):
    """The session carries who, the staff list says whether. Re-read each
    request, so revoking access does not wait for the session to age out."""
    secret, _ = _enrolled(web)
    _signed_in(web, secret, steps_ahead=1)
    token = web.cookies.get(staff_web.COOKIE)

    del web.people[4]
    response = web.get("/web/")

    assert response.status_code == 401
    alive = _db(lambda conn: web_access.touch_session(
        conn, token, idle_seconds=3600, absolute_seconds=7200,
        app_absolute_seconds=3600, lock_seconds=900))
    assert alive is None, "the worthless session was left in place"


def test_logout_needs_the_form_token(web):
    secret, _ = _enrolled(web)
    _signed_in(web, secret, steps_ahead=1)

    refused = web.post("/web/logout", data={"csrf": "forjado"})
    # Not ASCII: compare_digest would raise on it as text. Refused, not a 500.
    accented = web.post("/web/logout", data={"csrf": "é"})

    assert refused.status_code == 403
    assert accented.status_code == 403
    assert _served(web.get("/web/")), "a forged logout ended the session"


def test_logout_ends_the_session_for_good(web):
    secret, _ = _enrolled(web)
    _signed_in(web, secret, steps_ahead=1)
    token = web.cookies.get(staff_web.COOKIE)
    assert token

    web.post("/web/logout", data={"csrf": staff_web.csrf_for(token)})
    web.cookies.clear()

    replayed = web.get("/web/", headers={"cookie": f"{staff_web.COOKIE}={token}"})

    assert replayed.status_code == 401, "the old cookie still works"


# ── found in review: races and posts from elsewhere ──────────────────────────


def test_an_enrolment_in_flight_cannot_replace_one_confirmed_meanwhile(web, monkeypatch):
    """A request that passed the looks, waited, and arrived after a code had
    confirmed the enrolment and spent the link, used to replace the
    confirmed secret and show a new one. Starting over is /mfa refazer's."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    token = _link()
    secret, _ = _enrol(web, token)
    reached, release = threading.Event(), threading.Event()
    real = staff_web.get_staff_by_id

    async def paused(conn, staff_id):
        reached.set()
        assert await asyncio.to_thread(release.wait, 10)
        return await real(conn, staff_id)

    monkeypatch.setattr(staff_web, "get_staff_by_id", paused)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(web.post, f"/web/login/{token}/enrol")
        try:
            assert reached.wait(10)
            assert web.post(f"/web/login/{token}",
                            data={"code": _code(secret)}).status_code == 303
        finally:
            release.set()
        late = pending.result(10)

    assert "font-size:1.15rem" not in late.text, "a new secret was shown"
    assert _db(lambda conn: web_access.secret_of(conn, 4)) == secret
    enrolment = _db(lambda conn: web_access.enrolment_of(conn, 4))
    assert enrolment is not None and enrolment.confirmed


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
    {"Origin": "https://attacker.example"},
])
def test_a_login_posted_from_another_site_is_refused(web, headers):
    """Otherwise a page elsewhere could post its author's own link and code
    and sign a colleague's browser into the author's account."""
    token = _link()
    secret, _ = _enrol(web, token)

    response = web.post(f"/web/login/{token}", data={"code": _code(secret)},
                        headers=headers)

    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    assert _db(lambda conn: web_access.peek_login_link(conn, token)) == 4, (
        "a hostile post burned the link")


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "same-origin", "Origin": "null"},
    {"Sec-Fetch-Site": "same-origin", "Origin": "https://testserver"},
])
def test_a_login_posted_from_this_site_works(web, headers):
    """These pages send no referrer, so a browser sends `Origin: null` for
    their own forms. That must not be mistaken for a hostile post."""
    token = _link()
    secret, _ = _enrol(web, token)

    response = web.post(f"/web/login/{token}", data={"code": _code(secret)},
                        headers=headers)

    assert response.status_code == 303


def test_an_enrolment_posted_from_another_site_shows_no_secret(web):
    response = web.post(f"/web/login/{_link()}/enrol",
                        headers={"Sec-Fetch-Site": "cross-site"})

    assert response.status_code == 403
    assert "font-size:1.15rem" not in response.text
    assert _db(lambda conn: web_access.enrolment_of(conn, 4)) is None


def test_a_logout_posted_from_another_site_is_refused(web):
    secret, _ = _enrolled(web)
    _signed_in(web, secret, steps_ahead=1)
    token = web.cookies.get(staff_web.COOKIE)
    assert token

    response = web.post("/web/logout", data={"csrf": staff_web.csrf_for(token)},
                        headers={"Sec-Fetch-Site": "cross-site"})

    assert response.status_code == 403
    assert _served(web.get("/web/"))


@contextlib.contextmanager
def _an_old_browser(web):
    """A browser that sends no `Sec-Fetch-Site`, as Safari did before 16.4."""
    del web.headers["Sec-Fetch-Site"]
    try:
        yield
    finally:
        web.headers["Sec-Fetch-Site"] = "same-origin"


def test_a_login_without_fetch_metadata_is_refused_and_keeps_the_link(web):
    """Such a browser would leave only `Origin: null` to check, which a hostile
    page can produce too. Refused before the link is spent, so the person can
    still open it in a current browser."""
    token = _link()
    secret, _ = _enrol(web, token)

    with _an_old_browser(web):
        response = web.post(f"/web/login/{token}", data={"code": _code(secret)},
                            headers={"Origin": "null"})

    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    assert "navegador" in response.text, "the person is not told what to do"
    assert _db(lambda conn: web_access.peek_login_link(conn, token)) == 4


def test_an_enrolment_without_fetch_metadata_shows_no_secret(web):
    with _an_old_browser(web):
        response = web.post(f"/web/login/{_link()}/enrol")

    assert response.status_code == 403
    assert "font-size:1.15rem" not in response.text
    assert _db(lambda conn: web_access.enrolment_of(conn, 4)) is None


def test_an_old_browser_can_still_sign_out(web):
    """Logout carries the session's form token, which another site cannot
    know, so the header is not needed there and leaving must always work."""
    secret, _ = _enrolled(web)
    _signed_in(web, secret, steps_ahead=1)
    token = web.cookies.get(staff_web.COOKIE)
    assert token

    with _an_old_browser(web):
        response = web.post("/web/logout", data={"csrf": staff_web.csrf_for(token)})

    assert response.status_code == 200
    web.cookies.clear()
    replayed = web.get("/web/", headers={"cookie": f"{staff_web.COOKIE}={token}"})
    assert replayed.status_code == 401, "the session outlived the logout"


def test_the_key_and_the_address_read_as_they_must_be_typed(web):
    """Seen recording the tutorial: the key broke mid-group ("U" / "UVU"),
    and the otpauth address sat in an uppercase caption, so it read as
    "OTPAUTH://TOTP/...SECRET=..." while the app needs it as written."""
    page = web.post(f"/web/login/{_link()}/enrol").text

    key = re.search(r"<code style='([^']*)'>[A-Z2-7 ]+</code>", page)
    assert key and "break-all" not in key[1], "a group of four must not be split"
    address = page[page.index("Se o aplicativo aceitar"):]
    opening = page[:page.index("Se o aplicativo aceitar")].rsplit("<p", 1)[1]
    assert "class=sub" not in opening, "the caption style uppercases the address"
    assert "otpauth://totp/" in address


# ── the app session: the lock and the app entry ─────────────────────────────

_NAVIGATE = {"Sec-Fetch-Mode": "navigate"}
_SCRIPT = {"Sec-Fetch-Mode": "cors"}
_IPHONE = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
                         "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"}
_WINDOWS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Sec-CH-UA-Mobile": "?0"}
_MAC = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15"}


@pytest.fixture
def app_pilot(monkeypatch):
    """Face ID switched on for Rui."""
    monkeypatch.setattr(settings, "web_app", "pilot")
    monkeypatch.setattr(settings, "web_app_staff", "4")


def _passkey_session(web, staff_id: int = 4, *, locked: bool = False,
                     ended: bool = False, rule: Literal["browser", "app"] = "app") -> str:
    """A session as the Face ID sign-in opens it (on a phone unless `rule`
    says a computer), put in the client's jar once, the way a sign-in puts
    it there."""
    async def opened(conn):
        pid = await web_access.add_passkey(
            conn, staff_id, credential_id=os.urandom(16), public_key=b"key",
            sign_count=0, user_handle=os.urandom(32))
        assert pid is not None
        token = await web_access.open_session(
            conn, staff_id, idle_seconds=settings.web_session_idle_seconds,
            method="passkey", passkey_id=pid, rule=rule,
            absolute_seconds=settings.web_app_session_seconds)
        async with conn.cursor() as cur:
            if locked:
                await cur.execute(
                    "UPDATE web_sessions SET last_seen_at = NOW() - make_interval(secs => %s)",
                    (settings.web_app_lock_seconds + 60,))
            if ended:
                await cur.execute(
                    "UPDATE web_sessions SET created_at = NOW() - make_interval(secs => %s)",
                    (settings.web_app_session_seconds + 1,))
        await conn.commit()
        return token

    token = _db(opened)
    web.cookies.set(staff_web.COOKIE, token)
    return token


def _session_rows(token: str) -> int:
    async def count(conn):
        async with conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM web_sessions WHERE token_digest = %s",
                              (web_access.digest(token),))
            return (await cur.fetchone())[0]
    return _db(count)


def _touches_the_cookie(response) -> bool:
    """Whether the answer sets or deletes the session cookie."""
    return any(header.startswith(f"{staff_web.COOKIE}=")
               for header in response.headers.get_list("set-cookie"))


def _face_id(back: str) -> str:
    from urllib.parse import quote
    return f"/web/faceid/?volta={quote(back, safe='')}"


def test_app_entry_refuses_a_link_session_without_signing_out(web, app_pilot):
    """On a phone the app reaches Face ID, while the same person's Safari
    tab, holding the same cookie, keeps working."""
    secret, _ = _enrolled(web)
    _signed_in(web, secret, steps_ahead=1)
    token = web.cookies.get(staff_web.COOKIE)
    assert token

    response = web.get("/web/?app=1", headers={**_NAVIGATE, **_IPHONE})

    assert response.status_code == 303
    assert response.headers["location"] == _face_id("/web/?app=1")
    assert not _touches_the_cookie(response), "the app entry signed the browser out"
    assert _session_rows(token) == 1, "the app entry closed the session"
    assert _served(web.get("/web/", headers=_IPHONE))


def test_app_entry_serves_a_link_session_from_a_computer(web, app_pilot):
    """On a computer the installed app is the browser's own profile, so the
    session opened with the code is served there as in a tab."""
    secret, _ = _enrolled(web)
    _signed_in(web, secret, steps_ahead=1)

    for device in (_WINDOWS, _MAC):
        response = web.get("/web/?app=1", headers={**_NAVIGATE, **device})

        assert response.status_code == 200
        assert "Rui" in response.text


def test_app_entry_refuses_a_browser_passkey_session_from_a_phone(web, app_pilot):
    """A computer's passkey session copied into a phone's app is refused
    there, by its User-Agent or by its mobile hint alone, and the same
    cookie is still served without the marker."""
    token = _passkey_session(web, rule="browser")

    by_name = web.get("/web/?app=1", headers={**_NAVIGATE, **_IPHONE})
    by_hint = web.get("/web/?app=1", headers={**_NAVIGATE, **_WINDOWS,
                                              "Sec-CH-UA-Mobile": "?1"})

    for response in (by_name, by_hint):
        assert response.status_code == 303
        assert response.headers["location"] == _face_id("/web/?app=1")
        assert not _touches_the_cookie(response)
    assert _session_rows(token) == 1
    served = web.get("/web/", headers={**_NAVIGATE, **_IPHONE})
    assert _served(served)
    on_the_computer = web.get("/web/?app=1", headers={**_NAVIGATE, **_WINDOWS})
    assert _served(on_the_computer)


def test_app_entry_serves_a_passkey_session(web, app_pilot):
    _passkey_session(web)

    response = web.get("/web/?app=1", headers={**_NAVIGATE, **_IPHONE})

    assert _served(response)


def test_app_entry_without_a_cookie_reaches_face_id(web, app_pilot):
    navigated = web.get("/web/?app=1", headers=_NAVIGATE)
    scripted = web.get("/web/?app=1", headers=_SCRIPT)

    assert navigated.status_code == 303
    assert navigated.headers["location"] == _face_id("/web/?app=1")
    assert scripted.status_code == 401 and scripted.text == "app"
    assert not _touches_the_cookie(navigated) and not _touches_the_cookie(scripted)


def test_app_entry_with_an_ended_session_reaches_face_id(web, app_pilot):
    """Every open after 5 h asks for Face ID, not for a Telegram link."""
    _passkey_session(web, ended=True)

    response = web.get("/web/?app=1", headers=_NAVIGATE)

    assert response.status_code == 303
    assert response.headers["location"] == _face_id("/web/?app=1")
    assert not _touches_the_cookie(response)


def test_app_entry_while_off_is_todays_signed_out_page(web):
    assert settings.web_app == "off"

    response = web.get("/web/?app=1", headers=_NAVIGATE)

    assert response.status_code == 401
    assert "Você não está conectado" in response.text
    secret, _ = _enrolled(web)
    _signed_in(web, secret, steps_ahead=1)
    assert _served(web.get("/web/?app=1", headers=_NAVIGATE))


def test_app_entry_outside_the_pilot_keeps_a_link_session(web, monkeypatch):
    """Face ID is not offered to Rui here, so the app he installed keeps
    opening on his link session, as it did before."""
    monkeypatch.setattr(settings, "web_app", "pilot")
    monkeypatch.setattr(settings, "web_app_staff", "1")
    secret, _ = _enrolled(web)
    _signed_in(web, secret, steps_ahead=1)

    response = web.get("/web/?app=1", headers={**_NAVIGATE, **_IPHONE})

    assert _served(response)


def test_a_locked_session_gets_the_face_id_page_on_navigation(web, app_pilot):
    token = _passkey_session(web, locked=True)

    response = web.get("/web/", headers=_NAVIGATE)

    assert response.status_code == 303
    assert response.headers["location"] == _face_id("/web/")
    assert "Rui" not in response.text
    assert not _touches_the_cookie(response)
    assert _session_rows(token) == 1


def test_a_locked_session_gets_401_locked_from_a_script(web, app_pilot):
    _passkey_session(web, locked=True)

    response = web.get("/web/", headers=_SCRIPT)

    assert response.status_code == 401
    assert response.text == "locked"
    assert response.headers["content-type"].startswith("text/plain")
    assert "no-store" in response.headers["cache-control"]
    assert not _touches_the_cookie(response)


def test_cookie_lifetime_follows_the_rule(web, monkeypatch):
    """The cookie never outlives the session it carries: 12 h under the
    browser rule (a link session, or a passkey on a computer), 5 h under the
    app's, each read from its setting."""
    secret, codes = _enrolled(web)

    first = _signed_in(web, secret, steps_ahead=1)
    monkeypatch.setattr(settings, "web_session_absolute_seconds", 7200.0)
    second = web.post(f"/web/login/{_link()}", data={"code": codes[0]})

    assert "max-age=43200" in first.headers["set-cookie"].casefold()
    assert "max-age=7200" in second.headers["set-cookie"].casefold()
    assert staff_web.cookie_seconds("browser") == 7200
    assert staff_web.cookie_seconds("app") == 5 * 3600
    monkeypatch.setattr(settings, "web_app_session_seconds", 3600.0)
    assert staff_web.cookie_seconds("app") == 3600
    assert staff_web.cookie_seconds("browser") == 7200


def test_a_missing_handler_fails_closed(web, app_pilot):
    """An app built without `install` must not fall through to a page."""
    bare = FastAPI()
    bare.include_router(staff_web.router)
    client = TestClient(bare, base_url="https://testserver", follow_redirects=False,
                        raise_server_exceptions=False)
    try:
        _passkey_session(client, locked=True)
        locked = client.get("/web/", headers=_NAVIGATE)
        client.cookies.clear()
        entry = client.get("/web/?app=1", headers=_NAVIGATE)
    finally:
        client.close()

    for response in (locked, entry):
        assert response.status_code == 500
        assert "Rui" not in response.text and "<html" not in response.text.casefold()
