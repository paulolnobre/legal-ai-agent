"""Face ID: enrolling a passkey, signing in with it, unlocking with it.

Driven through the routes against a real Postgres, with a software passkey
(tests/fixtures/webauthn) answering the options the server hands out. Every
answer goes through the real py_webauthn verification: nothing here stubs
the check that decides.

The link sign-in is the real one too (link, then an authenticator code), so
enrolment meets exactly the session a person at the firm would have.

Skipped without TEST_DATABASE_URL, like test_staff_web_login.py.
"""
import asyncio
import contextlib
import html
import os
import re
import uuid
from typing import Any, LiteralString
from urllib.parse import quote

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import sql

from core import totp
from core.config import settings
from core.models import StaffMember
from core.repositories import web_access
from core.services import second_factor
from core.web import passkeys, staff_web
from tests.fixtures.webauthn.authenticator import Authenticator, b64


# A signed-in, unlocked session is served at /web/: shown the page when it
# has something to say (the Face ID offer), else sent where the work is. A
# refused one is answered (401) or sent to unlock (to Face ID).
_WORK = ("/web/assistente/#escrever", "/web/explorar/", "/web/arquivos/")


def _served(response) -> bool:
    if response.status_code == 200:
        return "Olá, " in response.text
    return response.status_code == 303 and response.headers["location"] in _WORK

URL = os.environ.get("TEST_DATABASE_URL")
SCHEMA = f"passkeys_{uuid.uuid4().hex[:12]}"
ORIGIN = "https://testserver"

pytestmark = pytest.mark.skipif(
    not URL, reason="needs a real Postgres: set TEST_DATABASE_URL",
)

_PEOPLE = {
    1: StaffMember(id=1, chat_id="100", name="Paulo", role="owner"),
    4: StaffMember(id=4, chat_id="400", name="Rui", role="advogado"),
}

FACE_ID_REFUSED = ("O Face ID não confirmou. Tente de novo, ou entre pelo link do "
                   "/web no Telegram.")
WINDOW_PASSED = ("Para ativar o Face ID, entre de novo pelo link do /web e ative "
                 "logo em seguida.")
TOO_MANY = "Este acesso já tem 5 aparelhos com Face ID. Remova um em Aparelhos para ativar este."
TOLD = ("Face ID ativado em um aparelho novo para o seu acesso ao painel. Se não foi "
        "você, mande /web encerrar agora.")
COMPUTER_TOLD = ("Chave de acesso ativada em um computador para o seu acesso ao painel. "
                 "Se não foi você, mande /web encerrar agora.")
# The same messages on a computer, which has a key and no Face ID.
KEY_REFUSED = ("A chave de acesso não confirmou. Tente de novo, ou entre pelo link do "
               "/web no Telegram.")
KEY_WINDOW_PASSED = ("Para ativar a chave de acesso, entre de novo pelo link do /web e "
                     "ative logo em seguida.")
KEY_TOO_MANY = "Este acesso já tem 5 chaves de acesso. Remova uma em Aparelhos para ativar esta."
DOCK = ("Neste app do Safari, entre com a chave de acesso. O código funciona no Chrome, "
        "no Edge ou numa aba do Safari.")

_NAVIGATE = {"Sec-Fetch-Mode": "navigate"}
_IPHONE = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
                         "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"}
_WINDOWS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Sec-CH-UA-Mobile": "?0"}


def _in_schema(statement: LiteralString) -> sql.Composed:
    return sql.SQL(statement).format(schema=sql.Identifier(SCHEMA))


class _Clock:
    """Stands in for the time module inside core.totp. Each code is taken a
    step later than the one before, so the replay guard never sees a step
    twice and nothing depends on the machine's clock."""

    now = 1_790_000_015.0

    def time(self) -> float:
        return self.now


async def _connect():
    import psycopg

    assert URL
    conn = await psycopg.AsyncConnection.connect(URL)
    async with conn.cursor() as cur:
        await cur.execute(_in_schema("SET search_path TO {schema}"))
    await conn.commit()
    return conn


class _Pool:
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


def _query(statement: LiteralString, params: tuple = ()):
    async def run(conn):
        async with conn.cursor() as cur:
            await cur.execute(statement, params)
            rows = await cur.fetchall() if cur.description else None
        await conn.commit()
        return rows
    return _db(run)


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
    told: list[tuple[str, str]] = []

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
    monkeypatch.setattr(settings, "web_base_url", ORIGIN)
    monkeypatch.setattr(settings, "web_app", "pilot")
    monkeypatch.setattr(settings, "web_app_staff", "1,4")
    monkeypatch.setattr(totp, "time", _Clock())
    for module in (staff_web, passkeys):
        monkeypatch.setattr(module, "get_pool", get_pool)
        monkeypatch.setattr(module, "get_staff_by_id", get_staff_by_id)
    monkeypatch.setattr(second_factor, "get_staff_by_id", get_staff_by_id)
    monkeypatch.setattr(second_factor, "list_staff", list_staff)
    monkeypatch.setattr(passkeys.telegram, "send_message", send_message)

    app = FastAPI()
    app.include_router(staff_web.router)
    app.include_router(passkeys.router)
    staff_web.install(app)
    # A phone unless a test says otherwise: these are Face ID's tests first,
    # and the copy follows the User-Agent.
    client = TestClient(app, base_url=ORIGIN, follow_redirects=False,
                        headers={"Sec-Fetch-Site": "same-origin", **_IPHONE})
    client.people = people  # type: ignore[attr-defined]
    client.told = told  # type: ignore[attr-defined]
    try:
        yield client
    finally:
        client.close()
        asyncio.run(drop())


# ── the link sign-in, as a person does it ───────────────────────────────────

_SECRETS: dict[int, str] = {}


@pytest.fixture(autouse=True)
def _fresh_secrets():
    _SECRETS.clear()
    yield


def _code(secret: str) -> str:
    _Clock.now += totp.STEP_SECONDS
    return totp.code_for_counter(secret, totp.counter_at())


def _link(staff_id: int) -> str:
    return _db(lambda conn: web_access.open_login_link(conn, staff_id, ttl_seconds=300))


def _link_session(web, staff_id: int = 4) -> str:
    """Sign in by link and authenticator code, enrolling the authenticator
    the first time; returns the session token."""
    token = _link(staff_id)
    if staff_id not in _SECRETS:
        page = web.post(f"/web/login/{token}/enrol").text
        grouped = re.search(r"font-size:1\.15rem[^>]*>([A-Z2-7 ]+)</code>", page)
        assert grouped
        _SECRETS[staff_id] = grouped.group(1).replace(" ", "")
    response = web.post(f"/web/login/{token}", data={"code": _code(_SECRETS[staff_id])})
    assert response.status_code == 303, response.text
    session = web.cookies.get(staff_web.COOKIE)
    assert session
    return session


def _csrf(web) -> str:
    return staff_web.csrf_for(web.cookies.get(staff_web.COOKIE) or "")


# ── the ceremonies ───────────────────────────────────────────────────────────


def _enrol(web, device: Authenticator, *, headers: dict | None = None, **answer):
    options = web.post("/web/faceid/ativar/opcoes", json={"csrf": _csrf(web)},
                       headers=headers)
    assert options.status_code == 200, options.text
    credential = device.create(options.json(), **answer)
    return web.post("/web/faceid/ativar", json={"csrf": _csrf(web), "credencial": credential},
                    headers=headers)


def _enrolled_device(web, staff_id: int = 4, *, sign_count: int = 0) -> Authenticator:
    """A device enrolled by the person through the routes; leaves the client
    signed out, the way a phone is before its first Face ID sign-in."""
    _link_session(web, staff_id)
    device = Authenticator(ORIGIN, sign_count=sign_count)
    response = _enrol(web, device)
    assert response.status_code == 200, response.text
    web.cookies.clear()
    return device


_UNSENT = object()


def _sign_in(web, device: Authenticator, *, volta: str | None = None,
             toque: object = _UNSENT, headers: dict | None = None, **answer):
    """The sign-in ceremony; `toque` is the page's touch report, sent as
    given (None is JSON null), and left out unless named."""
    options = web.post("/web/faceid/entrar/opcoes", json={}, headers=headers)
    assert options.status_code == 200, options.text
    body: dict = {"credencial": device.get(options.json(), **answer)}
    if volta is not None:
        body["volta"] = volta
    if toque is not _UNSENT:
        body["toque"] = toque
    return web.post("/web/faceid/entrar", json=body, headers=headers)


def _lock(token: str) -> None:
    _query("UPDATE web_sessions SET last_seen_at = NOW() - make_interval(secs => %s) "
           "WHERE token_digest = %s",
           (settings.web_app_lock_seconds + 60, web_access.digest(token)))


def _unlock(web, device: Authenticator, *, volta: str | None = None,
            headers: dict | None = None, **answer):
    options = web.post("/web/faceid/desbloquear/opcoes", json={"csrf": _csrf(web)},
                       headers=headers)
    assert options.status_code == 200, options.text
    body: dict = {"csrf": _csrf(web), "credencial": device.get(options.json(), **answer)}
    if volta is not None:
        body["volta"] = volta
    return web.post("/web/faceid/desbloquear", json=body, headers=headers)


def _stored(staff_id: int = 4) -> list[web_access.Passkey]:
    return _db(lambda conn: web_access.passkeys_of(conn, staff_id))


def _session_row(token: str):
    rows = _query("SELECT method, passkey_id FROM web_sessions WHERE token_digest = %s",
                  (web_access.digest(token),))
    return rows[0] if rows else None


def _rule(token: str) -> str:
    rows = _query("SELECT rule FROM web_sessions WHERE token_digest = %s",
                  (web_access.digest(token),))
    assert rows
    return rows[0][0]


def _read(response) -> str:
    """The page as a person reads it: tags gone, entities decoded."""
    return html.unescape(re.sub(r"<[^>]+>", "", response.text))


def _is_locked(token: str) -> bool:
    rows = _query("SELECT last_seen_at <= NOW() - make_interval(secs => %s) "
                  "FROM web_sessions WHERE token_digest = %s",
                  (settings.web_app_lock_seconds, web_access.digest(token)))
    assert rows
    return bool(rows[0][0])


# ── enrolment ────────────────────────────────────────────────────────────────


def test_enrol_options_ask_for_a_discoverable_verified_passkey(web):
    _link_session(web)
    existing = Authenticator(ORIGIN)
    assert _enrol(web, existing).status_code == 200

    options = web.post("/web/faceid/ativar/opcoes", json={"csrf": _csrf(web)}).json()

    assert options["rp"]["id"] == "testserver"
    assert options["authenticatorSelection"]["residentKey"] == "required"
    assert options["authenticatorSelection"]["userVerification"] == "required"
    assert options["attestation"] == "none"
    assert [c["id"] for c in options["excludeCredentials"]] == [b64(existing.credential_id)]


def test_enrol_accepts_the_recorded_registration_and_tells_telegram(web):
    _link_session(web)
    device = Authenticator(ORIGIN)

    response = _enrol(web, device, headers=_IPHONE)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "location" not in response.headers
    assert response.json() == {"ir": "/web/?aviso=faceid"}
    stored = _stored()
    assert [p.credential_id for p in stored] == [device.credential_id]
    assert stored[0].sign_count == 0
    assert web.told == [("400", TOLD)]  # type: ignore[attr-defined]


def test_enrol_refused_outside_the_window(web):
    token = _link_session(web)
    options = web.post("/web/faceid/ativar/opcoes", json={"csrf": _csrf(web)})
    assert options.status_code == 200
    _query("UPDATE web_sessions SET created_at = NOW() - make_interval(secs => %s) "
           "WHERE token_digest = %s",
           (settings.web_passkey_enrol_seconds + 60, web_access.digest(token)))

    late_options = web.post("/web/faceid/ativar/opcoes", json={"csrf": _csrf(web)})
    late = web.post("/web/faceid/ativar", json={
        "csrf": _csrf(web), "credencial": Authenticator(ORIGIN).create(options.json())})

    for response in (late_options, late):
        assert response.status_code == 403
        assert response.json() == {"erro": WINDOW_PASSED}
    assert _stored() == []
    assert web.told == []  # type: ignore[attr-defined]


def test_enrol_refused_from_a_passkey_session(web):
    device = _enrolled_device(web)
    assert _sign_in(web, device).status_code == 200

    response = web.post("/web/faceid/ativar/opcoes", json={"csrf": _csrf(web)})

    assert response.status_code == 403
    assert response.json() == {"erro": WINDOW_PASSED}


def test_enrol_refused_without_the_form_token(web):
    _link_session(web)

    missing = web.post("/web/faceid/ativar/opcoes", json={})
    wrong = web.post("/web/faceid/ativar/opcoes", json={"csrf": "x" * 40})

    assert missing.status_code == 403 and wrong.status_code == 403
    assert missing.json() == {"erro": FACE_ID_REFUSED}


def test_enrol_refused_for_origin_mismatch(web):
    _link_session(web)

    response = _enrol(web, Authenticator(ORIGIN), origin="https://evil.test")

    assert response.status_code == 400
    assert response.json() == {"erro": FACE_ID_REFUSED}
    assert _stored() == []
    assert web.told == []  # type: ignore[attr-defined]


def test_enrol_refused_for_rp_id_mismatch(web):
    _link_session(web)

    response = _enrol(web, Authenticator(ORIGIN), rp_id="evil.test")

    assert response.status_code == 400
    assert response.json() == {"erro": FACE_ID_REFUSED}
    assert _stored() == []
    assert web.told == []  # type: ignore[attr-defined]


def test_enrol_refused_without_user_verification(web):
    _link_session(web)

    response = _enrol(web, Authenticator(ORIGIN), uv=False)

    assert response.status_code == 400
    assert response.json() == {"erro": FACE_ID_REFUSED}
    assert _stored() == []
    assert web.told == []  # type: ignore[attr-defined]


def test_enrol_refused_for_a_challenge_answered_twice(web):
    _link_session(web)
    options = web.post("/web/faceid/ativar/opcoes", json={"csrf": _csrf(web)}).json()
    first = Authenticator(ORIGIN).create(options)
    second = Authenticator(ORIGIN).create(options)

    assert web.post("/web/faceid/ativar",
                    json={"csrf": _csrf(web), "credencial": first}).status_code == 200
    replay = web.post("/web/faceid/ativar", json={"csrf": _csrf(web), "credencial": second})

    assert replay.status_code == 400
    assert len(_stored()) == 1


def test_enrol_refused_for_a_sixth_passkey(web):
    _link_session(web)
    for _ in range(web_access.MAX_PASSKEYS):
        assert _enrol(web, Authenticator(ORIGIN)).status_code == 200

    options = web.post("/web/faceid/ativar/opcoes", json={"csrf": _csrf(web)})

    assert options.status_code == 409
    assert options.json() == {"erro": TOO_MANY, "aparelhos": "/web/aparelhos/"}
    assert len(_stored()) == web_access.MAX_PASSKEYS


# ── sign-in ──────────────────────────────────────────────────────────────────


def test_signin_opens_a_passkey_session_with_a_five_hour_cookie(web):
    device = _enrolled_device(web)

    response = _sign_in(web, device)

    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{staff_web.COOKIE}=")
    assert "max-age=18000" in cookie.casefold()
    assert "httponly" in cookie.casefold() and "secure" in cookie.casefold()
    token = web.cookies.get(staff_web.COOKIE)
    assert token
    assert _session_row(token) == ("passkey", _stored()[0].id)
    served = web.get("/web/?app=1", headers={**_NAVIGATE, **_IPHONE})
    assert _served(served)


def test_ceremonies_answer_json_with_the_destination(web):
    device = _enrolled_device(web)

    plain = _sign_in(web, device)
    web.cookies.clear()
    back = _sign_in(web, device, volta="/web/assistente/#escrever")
    token = web.cookies.get(staff_web.COOKIE)
    assert token
    _lock(token)
    unlocked = _unlock(web, device, volta="/web/explorar/")

    assert plain.json() == {"ir": "/web/?app=1"}
    assert back.json() == {"ir": "/web/assistente/#escrever"}
    assert unlocked.json() == {"ir": "/web/explorar/"}
    for response in (plain, back, unlocked):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert "location" not in response.headers
        assert "no-store" in response.headers["cache-control"]


@pytest.mark.parametrize("volta", ["//evil", "https://evil", "/web/../admin"])
def test_volta_cannot_leave_the_panel(web, volta):
    device = _enrolled_device(web)

    signed_in = _sign_in(web, device, volta=volta)
    token = web.cookies.get(staff_web.COOKIE)
    assert token
    _lock(token)
    unlocked = _unlock(web, device, volta=volta)
    page = web.get(f"/web/faceid/?volta={quote(volta, safe='')}")

    assert signed_in.json() == {"ir": "/web/"}
    assert unlocked.json() == {"ir": "/web/"}
    assert "evil" not in page.text and "admin" not in page.text


def test_signin_refused_without_user_verification(web):
    device = _enrolled_device(web)

    response = _sign_in(web, device, uv=False)

    assert response.status_code == 400
    assert response.json() == {"erro": FACE_ID_REFUSED}
    assert "set-cookie" not in response.headers
    assert _stored()[0].last_used_at is None


def test_signin_refused_for_another_origin_or_rp_id(web):
    device = _enrolled_device(web)

    answers: list[dict[str, Any]] = [{"origin": "https://evil.test"}, {"rp_id": "evil.test"}]
    for answer in answers:
        response = _sign_in(web, device, **answer)
        assert response.status_code == 400
        assert "set-cookie" not in response.headers


def test_signin_refused_for_an_assertion_answered_twice(web):
    device = _enrolled_device(web)
    options = web.post("/web/faceid/entrar/opcoes", json={}).json()
    assertion = device.get(options)

    assert web.post("/web/faceid/entrar", json={"credencial": assertion}).status_code == 200
    web.cookies.clear()
    replay = web.post("/web/faceid/entrar", json={"credencial": assertion})

    assert replay.status_code == 400
    assert "set-cookie" not in replay.headers


def test_signin_refused_for_an_unknown_credential(web):
    _enrolled_device(web)

    response = _sign_in(web, Authenticator(ORIGIN))

    assert response.status_code == 400
    assert response.json() == {"erro": FACE_ID_REFUSED}


def test_signin_refused_for_an_inactive_person(web):
    device = _enrolled_device(web)
    web.people.pop(4)  # type: ignore[attr-defined]

    response = _sign_in(web, device)

    assert response.status_code == 403
    assert response.json() == {"erro": FACE_ID_REFUSED}
    assert "set-cookie" not in response.headers
    assert _query("SELECT count(*) FROM web_sessions WHERE method = 'passkey'") == [(0,)]


def test_signin_refused_outside_the_switch(web, monkeypatch):
    device = _enrolled_device(web)
    monkeypatch.setattr(settings, "web_app_staff", "1")

    response = _sign_in(web, device)

    assert response.status_code == 403
    assert response.json() == {"erro": FACE_ID_REFUSED}
    assert "set-cookie" not in response.headers


def test_sign_count_zero_then_zero_is_accepted(web):
    device = _enrolled_device(web, sign_count=0)

    first = _sign_in(web, device)
    web.cookies.clear()
    second = _sign_in(web, device)

    assert first.status_code == 200 and second.status_code == 200


def test_sign_count_five_then_five_is_refused(web):
    device = _enrolled_device(web, sign_count=5)

    response = _sign_in(web, device)

    assert response.status_code == 400
    assert "set-cookie" not in response.headers


def test_sign_count_five_then_zero_is_refused(web):
    device = _enrolled_device(web, sign_count=5)
    device.sign_count = 0

    response = _sign_in(web, device)

    assert response.status_code == 400
    assert "set-cookie" not in response.headers


def test_sign_count_that_moves_is_accepted_and_stored(web):
    device = _enrolled_device(web, sign_count=5)
    device.sign_count = 6

    assert _sign_in(web, device).status_code == 200
    assert _stored()[0].sign_count == 6
    assert _stored()[0].last_used_at is not None


def test_signin_failures_share_the_five_attempt_lock(web):
    """Five refused Face ID answers lock the person: then the right passkey
    is refused, and so is a right authenticator code."""
    device = _enrolled_device(web)

    for _ in range(second_factor.MAX_FAILURES):
        assert _sign_in(web, device, uv=False).status_code == 400
    right = _sign_in(web, device)
    token = _link(4)
    code = web.post(f"/web/login/{token}", data={"code": _code(_SECRETS[4])})

    assert right.status_code == 429
    assert right.json() == {"erro": FACE_ID_REFUSED}
    assert "set-cookie" not in right.headers
    assert code.status_code == 429


def test_a_refused_code_counts_toward_the_face_id_lock(web):
    device = _enrolled_device(web)

    for _ in range(second_factor.MAX_FAILURES - 1):
        token = _link(4)
        assert web.post(f"/web/login/{token}", data={"code": "000000"}).status_code == 401
    assert _sign_in(web, device, uv=False).status_code == 400

    assert _sign_in(web, device).status_code == 429


def test_a_good_sign_in_clears_the_failure_count(web):
    device = _enrolled_device(web)

    for _ in range(second_factor.MAX_FAILURES - 1):
        assert _sign_in(web, device, uv=False).status_code == 400
    assert _sign_in(web, device).status_code == 200
    web.cookies.clear()
    assert _sign_in(web, device, uv=False).status_code == 400

    assert _sign_in(web, device).status_code == 200


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
    {"Sec-Fetch-Site": "same-origin", "Origin": "https://evil.test"},
], ids=["cross-site", "same-site", "foreign-origin"])
def test_signin_from_another_site_is_refused(web, headers):
    device = _enrolled_device(web)
    options = web.post("/web/faceid/entrar/opcoes", json={}).json()
    assertion = device.get(options)

    asked = web.post("/web/faceid/entrar/opcoes", json={}, headers=headers)
    answered = web.post("/web/faceid/entrar", json={"credencial": assertion}, headers=headers)

    for response in (asked, answered):
        assert response.status_code == 403
        assert response.json() == {"erro": FACE_ID_REFUSED}
        assert "set-cookie" not in response.headers
    # Refused before the challenge was spent: the real page can still answer.
    assert web.post("/web/faceid/entrar", json={"credencial": assertion}).status_code == 200


def test_signin_without_fetch_metadata_is_refused(web):
    device = _enrolled_device(web)
    options = web.post("/web/faceid/entrar/opcoes", json={}).json()
    web.headers.pop("Sec-Fetch-Site")

    response = web.post("/web/faceid/entrar", json={"credencial": device.get(options)})

    assert response.status_code == 403
    assert "set-cookie" not in response.headers


def test_an_oversized_body_is_refused(web):
    device = _enrolled_device(web)
    options = web.post("/web/faceid/entrar/opcoes", json={}).json()
    assertion = device.get(options)
    assertion["padding"] = "x" * (16 * 1024)

    response = web.post("/web/faceid/entrar", json={"credencial": assertion})

    assert response.status_code == 400
    assert "set-cookie" not in response.headers


def test_removing_the_passkey_ends_the_session_opened_through_the_route(web):
    device = _enrolled_device(web)
    assert _sign_in(web, device).status_code == 200
    assert _served(web.get("/web/"))

    passkey_id = _stored()[0].id
    removed = _db(lambda conn: web_access.remove_passkey(conn, 4, passkey_id))

    assert removed
    response = web.get("/web/")
    assert response.status_code == 401
    assert "Você não está conectado" in response.text


# ── unlock ───────────────────────────────────────────────────────────────────


def test_unlock_moves_the_lock_and_serves_again(web):
    device = _enrolled_device(web)
    assert _sign_in(web, device).status_code == 200
    token = web.cookies.get(staff_web.COOKIE)
    assert token
    _lock(token)
    assert web.get("/web/", headers=_NAVIGATE).status_code == 303

    response = _unlock(web, device)

    assert response.status_code == 200
    assert response.json() == {"ir": "/web/"}
    assert not _is_locked(token)
    assert _served(web.get("/web/"))


def test_unlock_refused_without_user_verification(web):
    device = _enrolled_device(web)
    assert _sign_in(web, device).status_code == 200
    token = web.cookies.get(staff_web.COOKIE)
    assert token
    _lock(token)

    response = _unlock(web, device, uv=False)

    assert response.status_code == 400
    assert response.json() == {"erro": FACE_ID_REFUSED}
    assert _is_locked(token)


def test_unlock_with_another_persons_passkey_is_refused(web):
    pauls = _enrolled_device(web, staff_id=1)
    ruis = _enrolled_device(web, staff_id=4)
    assert _sign_in(web, ruis).status_code == 200
    token = web.cookies.get(staff_web.COOKIE)
    assert token
    _lock(token)

    response = _unlock(web, pauls)

    assert response.status_code == 403
    assert response.json() == {"erro": FACE_ID_REFUSED}
    assert _is_locked(token)


def test_unlock_needs_the_form_token(web):
    device = _enrolled_device(web)
    assert _sign_in(web, device).status_code == 200
    token = web.cookies.get(staff_web.COOKIE)
    assert token
    _lock(token)
    options = web.post("/web/faceid/desbloquear/opcoes", json={"csrf": _csrf(web)}).json()

    no_options = web.post("/web/faceid/desbloquear/opcoes", json={})
    no_token = web.post("/web/faceid/desbloquear",
                        json={"credencial": device.get(options)})

    assert no_options.status_code == 403 and no_token.status_code == 403
    assert _is_locked(token)


def test_unlock_refused_for_a_link_session(web):
    _link_session(web)

    response = web.post("/web/faceid/desbloquear/opcoes", json={"csrf": _csrf(web)})

    assert response.status_code == 403
    assert response.json() == {"erro": FACE_ID_REFUSED}


def test_unlock_refused_after_the_session_ended(web):
    device = _enrolled_device(web)
    assert _sign_in(web, device).status_code == 200
    token = web.cookies.get(staff_web.COOKIE)
    assert token
    _query("UPDATE web_sessions SET created_at = NOW() - make_interval(secs => %s)",
           (settings.web_app_session_seconds + 1,))

    response = web.post("/web/faceid/desbloquear/opcoes", json={"csrf": _csrf(web)})

    assert response.status_code == 401
    assert response.json() == {"erro": FACE_ID_REFUSED}


# ── the rule follows the device ─────────────────────────────────────────────



def _computer_session(web, device: Authenticator) -> str:
    """A session as a passkey sign-in on a computer opens it: the browser
    rule. Put in the client's jar the way a sign-in puts it there."""
    passkey = next(p for p in _stored() if p.credential_id == device.credential_id)

    async def opened(conn):
        return await web_access.open_session(
            conn, 4, idle_seconds=settings.web_session_idle_seconds,
            method="passkey", passkey_id=passkey.id, rule="browser")

    token = _db(opened)
    web.cookies.set(staff_web.COOKIE, token)
    return token


def test_both_unlock_routes_refuse_a_computer_passkey_session(web):
    """A computer's session never locks, so it has nothing to unlock: neither
    the options nor the answer are let through, and nothing is spent."""
    device = _enrolled_device(web)
    token = _computer_session(web, device)
    _query("UPDATE web_sessions SET last_seen_at = NOW() - make_interval(secs => %s)",
           (settings.web_app_lock_seconds + 60,))
    before = _query("SELECT last_seen_at FROM web_sessions WHERE token_digest = %s",
                    (web_access.digest(token),))
    challenge = _db(lambda conn: web_access.issue_challenge(
        conn, "unlock", staff_id=4, session_token=token))
    assert challenge is not None
    answer = device.get({"challenge": b64(challenge), "rpId": "testserver"})

    options = web.post("/web/faceid/desbloquear/opcoes", json={"csrf": _csrf(web)})
    finish = web.post("/web/faceid/desbloquear",
                      json={"csrf": _csrf(web), "credencial": answer})

    assert options.status_code == 403 and options.json() == {"erro": FACE_ID_REFUSED}
    assert finish.status_code == 403 and finish.json() == {"erro": FACE_ID_REFUSED}
    assert _stored()[0].sign_count == 0, "the refused answer was counted as a use"
    assert _query("SELECT last_seen_at FROM web_sessions WHERE token_digest = %s",
                  (web_access.digest(token),)) == before
    assert _db(lambda conn: web_access.spend_challenge(
        conn, challenge, "unlock", session_token=token)) is not None, (
        "the refused answer spent its challenge")
    assert _served(web.get("/web/", headers=_WINDOWS)), "and it is not locked"


def test_a_later_computer_ua_does_not_loosen_a_phone_session(web):
    """The rule is stored when the session opens. Asking again with a
    computer's User-Agent changes nothing: it still locks after 15 minutes
    and still ends at 5 hours."""
    device = _enrolled_device(web)
    web.headers.update(_IPHONE)
    assert _sign_in(web, device).status_code == 200
    for header in _IPHONE:
        del web.headers[header]
    token = web.cookies.get(staff_web.COOKIE)
    assert token
    assert _served(web.get("/web/", headers=_WINDOWS))

    _lock(token)
    locked = web.get("/web/", headers={**_NAVIGATE, **_WINDOWS})
    entry = web.get("/web/?app=1", headers={**_NAVIGATE, **_WINDOWS})
    _query("UPDATE web_sessions SET last_seen_at = NOW(), "
           "created_at = NOW() - make_interval(secs => %s) WHERE token_digest = %s",
           (settings.web_app_session_seconds + 1, web_access.digest(token)))
    ended = web.get("/web/", headers={**_NAVIGATE, **_WINDOWS})

    assert locked.status_code == 303
    assert locked.headers["location"].startswith("/web/faceid/")
    assert entry.status_code == 303
    assert entry.headers["location"].startswith("/web/faceid/")
    assert ended.status_code == 401 and "Rui" not in ended.text


_MAC = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                      "(KHTML, like Gecko) Version/17.5 Safari/605.1.15"}


@pytest.mark.parametrize("toque", [0, 10])
def test_signin_from_a_computer_opens_a_browser_rule_session(web, toque):
    """A computer's passkey session lives by the browser's rule: 12 h
    cookie, and 15 minutes without use do not lock it."""
    device = _enrolled_device(web)

    response = _sign_in(web, device, toque=toque, headers=_WINDOWS)

    assert response.status_code == 200
    assert "max-age=43200" in response.headers["set-cookie"].casefold()
    token = web.cookies.get(staff_web.COOKIE)
    assert token
    assert _session_row(token) == ("passkey", _stored()[0].id)
    assert _rule(token) == "browser"
    _lock(token)
    served = web.get("/web/?app=1", headers={**_NAVIGATE, **_WINDOWS})
    assert _served(served)


def test_signin_from_a_phone_in_desktop_mode_opens_an_app_session(web):
    """An iPhone asking for the desktop site sends a Mac's User-Agent; its
    touch points give it away, and it keeps the app's rule."""
    device = _enrolled_device(web)

    response = _sign_in(web, device, toque=5, headers=_MAC)

    assert response.status_code == 200
    assert "max-age=18000" in response.headers["set-cookie"].casefold()
    token = web.cookies.get(staff_web.COOKIE)
    assert token
    assert _rule(token) == "app"


@pytest.mark.parametrize("agent", [_MAC, _WINDOWS], ids=["mac", "windows"])
def test_signin_without_a_touch_report_opens_an_app_session(web, agent):
    device = _enrolled_device(web)

    response = _sign_in(web, device, headers=agent)

    assert response.status_code == 200
    assert "max-age=18000" in response.headers["set-cookie"].casefold()
    token = web.cookies.get(staff_web.COOKIE)
    assert token
    assert _rule(token) == "app"


@pytest.mark.parametrize("agent", [_MAC, _WINDOWS], ids=["mac", "windows"])
@pytest.mark.parametrize("toque", ["0; x", -1, 99, False, True, "0", 0.5, None],
                         ids=["text", "negative", "too-many", "false", "true", "digit",
                              "fraction", "null"])
def test_signin_with_a_bad_touch_report_counts_as_missing(web, agent, toque):
    """Anything but a whole number from 0 to 32 is no report, and no report
    is a phone. JSON's true and false are bools, which Python counts as
    ints: refused too. Every one of these, read as a number, would make a
    Windows sign-in a computer's."""
    device = _enrolled_device(web)

    response = _sign_in(web, device, toque=toque, headers=agent)

    assert response.status_code == 200
    token = web.cookies.get(staff_web.COOKIE)
    assert token
    assert _rule(token) == "app"


# ── the pages ────────────────────────────────────────────────────────────────


def test_the_face_id_page_offers_sign_in_and_the_first_use_steps(web):
    response = web.get("/web/faceid/?volta=%2Fweb%2Fassistente%2F", headers=_IPHONE)

    assert response.status_code == 200
    assert response.headers["content-security-policy"] == staff_web.FACE_ID_POLICY
    assert "data-faceid=entrar" in response.text
    assert "data-volta='/web/assistente/'" in response.text
    assert "<script type=module src='/web/passkey.js'></script>" in response.text
    for step in ("/web", "Ativar Face ID neste aparelho", "Adicionar à Tela de Início",
                 "Entrar com Face ID"):
        assert step in response.text
    assert "desbloquear" not in response.text
    assert "entrar-computador" not in response.text
    assert "chave de acesso" not in response.text


def test_faceid_page_has_a_computer_form(web):
    """On a computer the key signs in from a tab and from the installed app
    alike; Safari's Dock app is told where the code works."""
    response = web.get("/web/faceid/?volta=%2Fweb%2F%3Fapp%3D1", headers=_WINDOWS)
    read = _read(response)

    assert response.status_code == 200
    assert response.headers["content-security-policy"] == staff_web.FACE_ID_POLICY
    assert "<script type=module src='/web/passkey.js'></script>" in response.text
    assert re.search(r"<button type=button data-faceid=entrar-computador "
                     r"data-volta='/web/\?app=1' data-recusa='[^']*'>Entrar com a chave de acesso"
                     r"</button>",
                     response.text)
    assert "data-faceid=entrar " not in response.text
    assert "ou peça um link com /web no Telegram" in read
    assert re.search(r"<p class=note data-dock hidden>[^<]*</p>", response.text)
    assert DOCK in read
    for step in ("Primeira vez neste computador?",
                 "No Telegram, mande /web e entre pelo link com o código do autenticador.",
                 "Na página inicial, clique em Ativar a chave de acesso deste computador.",
                 "no Chrome ou no Edge, clique no ícone de instalar na barra de endereço; "
                 "no Safari, use Arquivo > Adicionar ao Dock.",
                 "Abra o painel pelo ícone e entre com a chave de acesso, ou com o código "
                 "no Chrome ou no Edge."):
        assert step in read, step
    assert "Face ID" not in read and "Tela de Início" not in read
    assert "desbloquear" not in response.text


def test_the_face_id_page_offers_unlock_to_a_locked_session(web):
    device = _enrolled_device(web)
    assert _sign_in(web, device).status_code == 200
    token = web.cookies.get(staff_web.COOKIE)
    assert token
    _lock(token)

    response = web.get("/web/faceid/?volta=%2Fweb%2F")

    assert response.status_code == 200
    assert "data-faceid=desbloquear" in response.text
    assert f"data-csrf='{_csrf(web)}'" in response.text
    assert "data-faceid=entrar" not in response.text
    assert _is_locked(token), "looking at the page unlocked the session"


def test_the_script_is_served_as_a_script(web):
    response = web.get("/web/passkey.js")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "innerHTML" not in response.text
    assert "display-mode: standalone" in response.text
    for copy in (FACE_ID_REFUSED,):
        assert copy in response.text
    assert "toque: navigator.maxTouchPoints" in response.text
    assert '"entrar-computador"' in response.text and "data-dock" in response.text


def test_the_face_id_policy_adds_only_a_script_and_its_requests():
    assert staff_web.FACE_ID_POLICY == (
        staff_web.HEADERS["Content-Security-Policy"]
        + "; script-src 'self'; connect-src 'self'")
    assert "script-src" not in staff_web.HEADERS["Content-Security-Policy"]


def test_home_offers_face_id_inside_the_window_only(web):
    token = _link_session(web)

    offered = web.get("/web/")
    _query("UPDATE web_sessions SET created_at = NOW() - make_interval(secs => %s) "
           "WHERE token_digest = %s",
           (settings.web_passkey_enrol_seconds + 60, web_access.digest(token)))
    later = web.get("/web/")

    assert "data-faceid=ativar" in offered.text
    assert f"data-csrf='{_csrf(web)}'" in offered.text
    assert offered.headers["content-security-policy"] == staff_web.FACE_ID_POLICY
    assert _served(later) and later.status_code == 303, "past the window, nothing to say here"


def test_home_offers_face_id_only_to_whoever_has_it(web, monkeypatch):
    monkeypatch.setattr(settings, "web_app_staff", "1")
    _link_session(web)

    response = web.get("/web/")

    assert "data-faceid" not in response.text and "<script" not in response.text


def test_home_offers_nothing_to_a_passkey_session(web):
    device = _enrolled_device(web)
    assert _sign_in(web, device).status_code == 200

    response = web.get("/web/")

    assert _served(response) and response.status_code == 303
    assert "data-faceid" not in response.text


def test_home_says_face_id_was_turned_on(web):
    _link_session(web)

    response = web.get("/web/?aviso=faceid", headers=_IPHONE)

    assert "Face ID ativado neste aparelho" in response.text
    assert "data-faceid" not in response.text


def test_computer_enrolment_success_page_has_computer_steps(web):
    _link_session(web)

    computer = _read(web.get("/web/?aviso=faceid", headers=_WINDOWS))
    phone = _read(web.get("/web/?aviso=faceid", headers=_IPHONE))

    assert "Chave de acesso ativada neste computador" in computer
    assert "Tela de Início" not in computer and "Face ID" not in computer
    for step in ("Chrome", "Edge", "ícone de instalar", "Arquivo > Adicionar ao Dock",
                 "Entrar com a chave de acesso"):
        assert step in computer, step
    assert ("Face ID ativado neste aparelho. No iPhone, toque em Compartilhar e em "
            "Adicionar à Tela de Início, e abra o painel pelo ícone com Entrar com "
            "Face ID.") in phone
    assert "computador" not in phone and "Dock" not in phone


def test_home_offers_the_computer_key_on_a_computer(web):
    _link_session(web)

    computer = web.get("/web/", headers=_WINDOWS)
    phone = web.get("/web/", headers=_IPHONE)

    assert "data-faceid=ativar " in computer.text
    assert "Ativar a chave de acesso deste computador" in computer.text
    assert "Face ID" not in _read(computer)
    assert computer.headers["content-security-policy"] == staff_web.FACE_ID_POLICY
    assert "data-faceid=ativar " in phone.text
    assert "Ativar Face ID neste aparelho" in phone.text
    assert "chave de acesso" not in phone.text


def test_computer_enrolment_notice(web):
    _link_session(web)

    response = _enrol(web, Authenticator(ORIGIN), headers=_WINDOWS)

    assert response.status_code == 200
    assert response.json() == {"ir": "/web/?aviso=faceid"}
    assert web.told == [("400", COMPUTER_TOLD)]  # type: ignore[attr-defined]


_ROUTES = [
    ("get", "/web/faceid/"), ("get", "/web/passkey.js"),
    ("get", "/web/sessao"), ("get", "/web/aparelhos/"),
    ("post", "/web/aparelhos/remover"),
    ("post", "/web/faceid/ativar/opcoes"), ("post", "/web/faceid/ativar"),
    ("post", "/web/faceid/entrar/opcoes"), ("post", "/web/faceid/entrar"),
    ("post", "/web/faceid/desbloquear/opcoes"), ("post", "/web/faceid/desbloquear"),
]


def test_every_route_answers_404_while_off(web, monkeypatch):
    _link_session(web)
    monkeypatch.setattr(settings, "web_app", "off")

    for method, path in _ROUTES:
        response = getattr(web, method)(path, **({"json": {"csrf": _csrf(web)}}
                                                 if method == "post" else {}))
        assert response.status_code == 404, path
    home = web.get("/web/")
    assert "data-faceid" not in home.text and "<script" not in home.text

    monkeypatch.setattr(settings, "web_app", "on")
    monkeypatch.setattr(settings, "web_enabled", False)
    for method, path in _ROUTES:
        assert getattr(web, method)(path).status_code == 404, path


# ── what the authenticator holds, and two answers at once ───────────────────


def _stored_handles(staff_id: int = 4) -> list[bytes]:
    rows = _query("SELECT user_handle FROM staff_passkeys WHERE staff_id = %s ORDER BY id",
                  (staff_id,))
    return [bytes(row[0]) for row in rows or []]


def test_the_stored_handle_is_the_one_the_authenticator_received(web):
    """user.id in the options is what the device files the passkey under;
    the row must carry the same, from the first credential on, or every
    later enrolment names another account to the device."""
    _link_session(web)
    first, second = Authenticator(ORIGIN), Authenticator(ORIGIN)

    assert _enrol(web, first).status_code == 200
    assert _enrol(web, second).status_code == 200

    assert first.user_handle is not None
    assert second.user_handle == first.user_handle
    assert _stored_handles() == [first.user_handle, second.user_handle]


def test_two_answers_with_the_same_counter_open_one_session(web, monkeypatch):
    """Two assertions of one credential, each with its own challenge and the
    same counter (6, against a stored 5). The second runs whole while the
    first sits between reading the credential and writing its use: exactly
    one may open a session, and the stored count is never lowered."""
    import anyio

    device = _enrolled_device(web, sign_count=5)
    device.sign_count = 6
    first = device.get(web.post("/web/faceid/entrar/opcoes", json={}).json())
    second = device.get(web.post("/web/faceid/entrar/opcoes", json={}).json())
    other = TestClient(web.app, base_url=ORIGIN, follow_redirects=False,
                       headers={"Sec-Fetch-Site": "same-origin"})
    real = web_access.passkey_by_credential
    raced: list = []
    started: list[bool] = []

    async def read_then_let_the_other_finish(conn, credential_id):
        found = await real(conn, credential_id)
        if not started:
            started.append(True)
            raced.append(await anyio.to_thread.run_sync(
                lambda: other.post("/web/faceid/entrar", json={"credencial": second})))
        return found

    monkeypatch.setattr(web_access, "passkey_by_credential", read_then_let_the_other_finish)
    try:
        answered = web.post("/web/faceid/entrar", json={"credencial": first})
    finally:
        other.close()

    statuses = sorted([answered.status_code, raced[0].status_code])
    assert statuses == [200, 400], statuses
    assert _query("SELECT count(*) FROM web_sessions WHERE method = 'passkey'") == [(1,)]
    assert _stored()[0].sign_count == 6


# Returning to a page and removing a credential use the real session clock
# and the real foreign-key cascade, not a stub of either decision.
def test_sessao_reports_locked_and_ended_without_moving_the_clock(web):
    device = _enrolled_device(web)
    assert _sign_in(web, device).status_code == 200
    token = web.cookies.get(staff_web.COOKIE)
    before = _query("SELECT last_seen_at FROM web_sessions WHERE token_digest = %s",
                    (web_access.digest(token),))
    assert web.get('/web/sessao').status_code == 204
    assert _query("SELECT last_seen_at FROM web_sessions WHERE token_digest = %s",
                  (web_access.digest(token),)) == before
    _lock(token)
    locked = web.get('/web/sessao', headers=_NAVIGATE)
    assert locked.status_code == 401 and locked.text == 'locked'
    assert 'location' not in locked.headers and 'set-cookie' not in locked.headers
    _query("UPDATE web_sessions SET created_at = NOW() - interval '6 hours' "
           "WHERE token_digest = %s", (web_access.digest(token),))
    ended = web.get('/web/sessao')
    assert ended.status_code == 401 and ended.text == 'ended'
    assert 'set-cookie' not in ended.headers


def test_sessao_requires_an_active_pilot_member(web, monkeypatch):
    _link_session(web)
    monkeypatch.setattr(settings, 'web_app_staff', '1')
    assert web.get('/web/sessao').status_code == 404
    monkeypatch.setattr(settings, 'web_app_staff', '1,4')
    web.people.pop(4)
    assert web.get('/web/sessao').text == 'ended'


def test_aparelhos_lists_only_ones_own(web):
    _enrolled_device(web, 4)
    their_id = _stored(4)[0].id
    _link_session(web, 1)
    assert _enrol(web, Authenticator(ORIGIN)).status_code == 200
    own_id = _stored(1)[0].id
    response = web.get('/web/aparelhos/')
    assert response.status_code == 200
    assert f"name=chave value='{own_id}'" in response.text
    assert f"name=chave value='{their_id}'" not in response.text
    assert 'Chave de acesso criada em ' in response.text
    assert 'Último uso' in response.text or 'Ainda não usada' in response.text
    assert '/web encerrar' in response.text and 'iCloud' in response.text
    assert re.findall(r"<script[^>]*src='([^']+)'", response.text) == ["/web/navigation.js"]


def test_aparelhos_says_what_sair_ends(web):
    _link_session(web)

    response = web.get('/web/aparelhos/')

    assert response.status_code == 200
    assert ("Sair encerra só a sessão deste navegador. /web encerrar, no Telegram, "
            "encerra todas.") in _read(response)


@pytest.mark.parametrize('csrf,site', [('', 'same-origin'), ('wrong', 'same-origin'),
                                      ('valid', 'cross-site')])
def test_remove_needs_csrf_and_same_origin(web, csrf, site):
    _link_session(web)
    assert _enrol(web, Authenticator(ORIGIN)).status_code == 200
    pid = _stored()[0].id
    response = web.post('/web/aparelhos/remover', data={
        'csrf': _csrf(web) if csrf == 'valid' else csrf, 'chave': str(pid),
    }, headers={'Sec-Fetch-Site': site})
    assert response.status_code == 403
    assert len(_stored()) == 1


def test_remove_cannot_touch_another_persons_key(web):
    _enrolled_device(web, 4)
    pid = _stored(4)[0].id
    _link_session(web, 1)
    response = web.post('/web/aparelhos/remover',
                        data={'csrf': _csrf(web), 'chave': str(pid)})
    assert response.status_code == 303
    assert len(_stored(4)) == 1


def test_remove_ends_the_passkey_session_that_removed_it(web):
    device = _enrolled_device(web)
    assert _sign_in(web, device).status_code == 200
    pid = _stored()[0].id
    response = web.post('/web/aparelhos/remover',
                        data={'csrf': _csrf(web), 'chave': str(pid)})
    assert response.status_code == 303
    assert not _stored()
    assert web.get('/web/sessao').text == 'ended'


def test_locked_session_cannot_list_or_remove_devices(web):
    device = _enrolled_device(web)
    assert _sign_in(web, device).status_code == 200
    _lock(web.cookies.get(staff_web.COOKIE))
    assert web.get('/web/aparelhos/').text == 'locked'
    response = web.post('/web/aparelhos/remover',
                        data={'csrf': _csrf(web), 'chave': str(_stored()[0].id)})
    assert response.status_code == 401 and response.text == 'locked'
    assert len(_stored()) == 1


def test_face_id_lock_tells_the_person_and_the_owners_once(web):
    device = _enrolled_device(web)
    web.told.clear()
    for _ in range(second_factor.MAX_FAILURES - 1):
        assert _sign_in(web, device, uv=False).status_code == 400
    assert not web.told
    assert _sign_in(web, device, uv=False).status_code == 400
    assert {chat for chat, _ in web.told} == {'100', '400'}
    assert all('Face ID' in text and 'códigos' not in text for _, text in web.told)
    assert _sign_in(web, device).status_code == 429
    assert len(web.told) == 2


# ── the computer's words ────────────────────────────────────────────────────
# Every message a ceremony can show, on a computer and on a phone: the
# computer speaks of its key and never of Face ID, the phone as before.

_SAID = {
    "computer": {"refused": KEY_REFUSED, "window": KEY_WINDOW_PASSED, "many": KEY_TOO_MANY},
    "phone": {"refused": FACE_ID_REFUSED, "window": WINDOW_PASSED, "many": TOO_MANY},
}
_DEVICES = [pytest.param(_WINDOWS, "computer", id="computer"),
            pytest.param(_IPHONE, "phone", id="phone")]


def _no_face_id_on_a_computer(kind: str, text: str) -> None:
    if kind == "computer":
        assert "Face ID" not in text


@pytest.mark.parametrize("agent,kind", _DEVICES)
def test_a_refused_sign_in_speaks_of_the_device(web, agent, kind):
    device = _enrolled_device(web)

    response = _sign_in(web, device, uv=False, headers=agent)

    assert response.status_code == 400
    assert response.json() == {"erro": _SAID[kind]["refused"]}
    _no_face_id_on_a_computer(kind, response.text)


@pytest.mark.parametrize("agent,kind", _DEVICES)
def test_a_refused_unlock_speaks_of_the_device(web, agent, kind):
    device = _enrolled_device(web)
    assert _sign_in(web, device).status_code == 200
    token = web.cookies.get(staff_web.COOKIE)
    assert token
    _lock(token)

    response = _unlock(web, device, uv=False, headers=agent)

    assert response.status_code == 400
    assert response.json() == {"erro": _SAID[kind]["refused"]}
    _no_face_id_on_a_computer(kind, response.text)


@pytest.mark.parametrize("agent,kind", _DEVICES)
def test_a_passed_enrolment_window_speaks_of_the_device(web, agent, kind):
    token = _link_session(web)
    _query("UPDATE web_sessions SET created_at = NOW() - make_interval(secs => %s) "
           "WHERE token_digest = %s",
           (settings.web_passkey_enrol_seconds + 60, web_access.digest(token)))

    response = web.post("/web/faceid/ativar/opcoes", json={"csrf": _csrf(web)},
                        headers=agent)

    assert response.status_code == 403
    assert response.json() == {"erro": _SAID[kind]["window"]}
    _no_face_id_on_a_computer(kind, response.text)


@pytest.mark.parametrize("agent,kind", _DEVICES)
def test_a_sixth_passkey_speaks_of_the_device(web, agent, kind):
    _link_session(web)
    for _ in range(web_access.MAX_PASSKEYS):
        assert _enrol(web, Authenticator(ORIGIN)).status_code == 200

    response = web.post("/web/faceid/ativar/opcoes", json={"csrf": _csrf(web)},
                        headers=agent)

    assert response.status_code == 409
    assert response.json() == {"erro": _SAID[kind]["many"], "aparelhos": "/web/aparelhos/"}
    _no_face_id_on_a_computer(kind, response.text)


@pytest.mark.parametrize("agent,kind", _DEVICES)
def test_a_cancelled_ceremony_speaks_of_the_device(web, agent, kind):
    """A cancel never reaches the server: the script says it, with the
    words the page hands it on every ceremony button."""
    refusal = f"data-recusa='{html.escape(_SAID[kind]['refused'], quote=True)}'"
    _link_session(web)
    offer = web.get("/web/", headers=agent)
    web.cookies.clear()
    sign_in = web.get("/web/faceid/", headers=agent)
    device = _enrolled_device(web)
    assert _sign_in(web, device).status_code == 200
    token = web.cookies.get(staff_web.COOKIE)
    assert token
    _lock(token)
    unlock = web.get("/web/faceid/", headers=agent)

    for page in (offer, sign_in, unlock):
        assert page.status_code == 200
        buttons = re.findall(r"<button type=button data-faceid=[^>]*>", page.text)
        assert len(buttons) == 1 and refusal in buttons[0], page.text
        _no_face_id_on_a_computer(kind, _read(page))
    script = web.get("/web/passkey.js").text
    assert "button.dataset.recusa" in script


@pytest.mark.parametrize("agent,kind", _DEVICES)
def test_the_unlock_page_speaks_of_the_device(web, agent, kind):
    device = _enrolled_device(web)
    assert _sign_in(web, device).status_code == 200
    token = web.cookies.get(staff_web.COOKIE)
    assert token
    _lock(token)

    read = _read(web.get("/web/faceid/?volta=%2Fweb%2F", headers=agent))

    if kind == "computer":
        for copy in ("Painel bloqueado",
                     "Confirme com a chave de acesso para continuar de onde parou.",
                     "Desbloquear com a chave de acesso",
                     "Sem a chave de acesso aqui? Peça um novo link no Telegram com /web."):
            assert copy in read, copy
        assert "Face ID" not in read
    else:
        for copy in ("Painel bloqueado", "Confirme com o Face ID para continuar de onde parou.",
                     "Desbloquear com Face ID",
                     "Sem Face ID neste aparelho? Peça um novo link no Telegram com /web."):
            assert copy in read, copy
        assert "chave de acesso" not in read


# ── a way back in after the session ends ────────────────────────────────────
# Safari's Dock app cannot open a Telegram link: once its session ends, the
# page that says so is its only way to the key.

_WAY_BACK = {"computer": "<a href='/web/faceid/'>Entrar com a chave de acesso</a>",
             "phone": "<a href='/web/faceid/'>Entrar com Face ID</a>"}


def _expire(token: str) -> None:
    _query("UPDATE web_sessions SET expires_at = NOW() - interval '1 second' "
           "WHERE token_digest = %s", (web_access.digest(token),))


@pytest.mark.parametrize("agent,kind", _DEVICES)
def test_an_ended_session_offers_the_way_back_in(web, agent, kind):
    _expire(_link_session(web))

    response = web.get("/web/", headers=agent)

    assert response.status_code == 401
    assert _WAY_BACK[kind] in response.text
    assert "/web" in _read(response)
    assert f'{staff_web.COOKIE}=""' in response.headers["set-cookie"], "the cookie goes"
    _no_face_id_on_a_computer(kind, _read(response))


@pytest.mark.parametrize("agent,kind", _DEVICES)
def test_logout_offers_the_way_back_in(web, agent, kind):
    _link_session(web)

    response = web.post("/web/logout", data={"csrf": _csrf(web)}, headers=agent)

    assert response.status_code == 200
    assert "Você saiu" in response.text
    assert _WAY_BACK[kind] in response.text
    _no_face_id_on_a_computer(kind, _read(response))


def test_no_way_back_in_link_while_the_app_is_off(web, monkeypatch):
    token = _link_session(web)
    monkeypatch.setattr(settings, "web_app", "off")

    left = web.post("/web/logout", data={"csrf": _csrf(web)}, headers=_WINDOWS)
    _expire(token)
    ended = web.get("/web/", headers=_WINDOWS)

    assert left.status_code == 200 and ended.status_code == 401
    for response in (left, ended):
        assert "/web/faceid/" not in response.text
        assert "Entrar com" not in response.text


# ── /web/ opens where the work is ───────────────────────────────────────────


def _past_the_offer(token: str) -> None:
    _query("UPDATE web_sessions SET created_at = NOW() - make_interval(secs => %s) "
           "WHERE token_digest = %s",
           (settings.web_passkey_enrol_seconds + 60, web_access.digest(token)))


def test_home_with_nothing_to_say_opens_the_assistant(web, monkeypatch):
    monkeypatch.setattr(settings, "web_assistant", "on")
    monkeypatch.setattr(settings, "web_assistant_staff", "")
    _past_the_offer(_link_session(web))

    response = web.get("/web/")

    assert response.status_code == 303
    assert response.headers["location"] == "/web/assistente/#escrever"


def test_home_without_the_assistant_opens_the_files(web, monkeypatch):
    monkeypatch.setattr(settings, "web_assistant", "off")
    monkeypatch.setattr(settings, "web_explorer", "on")
    _past_the_offer(_link_session(web))

    response = web.get("/web/")

    assert response.status_code == 303
    assert response.headers["location"] == "/web/explorar/"


def test_home_still_says_that_face_id_was_turned_on(web):
    _past_the_offer(_link_session(web))

    response = web.get("/web/?aviso=faceid")

    assert response.status_code == 200
    assert "Face ID ativado neste aparelho" in response.text


def test_no_menu_holds_inicio(web, monkeypatch):
    monkeypatch.setattr(settings, "web_explorer", "on")
    _link_session(web)
    sidebar = web.get("/web/").text
    monkeypatch.setattr(settings, "web_app_staff", "1")
    bar = staff_web.nav(web.people[4], "a-session").markup

    assert "sidebar-nav" in sidebar and "Início" not in sidebar
    assert bar.startswith("<nav>") and "Início" not in bar
    assert "href='/web/integracoes/'" in bar
