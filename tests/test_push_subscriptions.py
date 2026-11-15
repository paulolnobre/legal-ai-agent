"""Subscribing the installed app to notices, against a real Postgres.

Only an unlocked app session (a passkey on a phone) may subscribe, and the
subscription takes that session's passkey, so removing the passkey stops its
notices. The endpoint is a URL the server will later POST to, so only known
push services are accepted. Every rule here that is a security property runs
through the routes and the real tables: a stub of the session would pass with
the rule gone.

Skipped without TEST_DATABASE_URL, except the endpoint checks, which are pure.
"""
import asyncio
import base64
import contextlib
import html
import json
import secrets
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from core import config
from core.config import settings
from core.repositories import push_subscriptions, web_access
from core.repositories.push_subscriptions import allowed_endpoint
from core.web import assistant, pages, passkeys, staff_web

PAULO, RUI = 1, 4
ENDPOINT = "https://web.push.apple.com/QGuQyavXutnMH0wtDFDzLdkC8F2hD2tvqQZ"
PUBLIC_KEY = "BPub" + "A" * 83
DENIED = ("Os avisos estão desligados nos Ajustes do iPhone para este app. Ative em "
          "Ajustes > Notificações > Demo & Associados.")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _device_keys() -> tuple[str, str]:
    """What a browser hands back from PushManager.subscribe: its P-256 public
    point and a 16-byte secret, both unpadded base64url."""
    point = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return _b64(point), _b64(secrets.token_bytes(16))


# ── the endpoint ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("url", [
    "https://web.push.apple.com/x",
    "https://api.push.apple.com/x",
    "https://fcm.googleapis.com/x",
    "https://updates.push.services.mozilla.com/wpush/v2/x",
])
def test_allowed_endpoint_accepts_the_push_services(url):
    assert allowed_endpoint(url)


@pytest.mark.parametrize("url", [
    "http://web.push.apple.com/x",
    "https://xpush.apple.com/x",
    "https://push.apple.com.example/x",
    "https://evilpush.apple.com.example/x",
    "https://web.push.apple.com:8443/x",
    "https://web.push.apple.com:443/x",
    "https://u:p@web.push.apple.com/x",
    "https://u@web.push.apple.com/x",
    "https://evil/x",
    "https://push.apple.com/x",
    "https://.push.apple.com/x",
    "https://fcm.googleapis.com.evil/x",
    "https://web.push.apple.com\\@evil/x",
    "https://evil\\.push.apple.com/x",
    "https://web.push.apple.com/x y",
    "https://web.push.apple.com/x\n",
    "https://[::1/x",
    "https://web.push.apple.com:x/x",
    "",
    "https://web.push.apple.com/" + "x" * 1000,
])
def test_allowed_endpoint_refuses_anything_else(url):
    assert not allowed_endpoint(url)


def test_allowed_endpoint_takes_up_to_1024_characters():
    base = "https://web.push.apple.com/"
    assert allowed_endpoint(base + "x" * (1024 - len(base)))
    assert not allowed_endpoint(base + "x" * (1025 - len(base)))


# ── the site ─────────────────────────────────────────────────────────────────


@pytest.fixture
def site(db, monkeypatch):
    async def build():
        conn = await db.connect()
        try:
            await db.build(conn)
        finally:
            await conn.close()

    asyncio.run(build())

    class _Pool:
        def connection(self):
            @contextlib.asynccontextmanager
            async def opened():
                conn = await db.connect()
                try:
                    yield conn
                    await conn.commit()
                finally:
                    await conn.close()
            return opened()

    async def get_pool():
        return _Pool()

    monkeypatch.setattr(settings, "web_enabled", True)
    monkeypatch.setattr(settings, "web_base_url", "https://testserver")
    monkeypatch.setattr(settings, "web_app", "pilot")
    monkeypatch.setattr(settings, "web_app_staff", f"{PAULO},{RUI}")
    monkeypatch.setattr(settings, "web_assistant", "on")
    monkeypatch.setattr(settings, "web_push_vapid_public_key", PUBLIC_KEY)
    monkeypatch.setattr(settings, "web_push_vapid_private_key", SecretStr("private"))
    monkeypatch.setattr(settings, "web_push_subject", "mailto:contato@example.com")
    for module in (staff_web, passkeys, assistant):
        monkeypatch.setattr(module, "get_pool", get_pool)

    app = FastAPI()
    app.include_router(staff_web.router)
    app.include_router(passkeys.router)
    app.include_router(assistant.router)
    staff_web.install(app)
    client = TestClient(app, base_url="https://testserver", follow_redirects=False,
                        headers={"Sec-Fetch-Site": "same-origin"})

    def run(fn):
        async def main():
            conn = await db.connect()
            try:
                return await fn(conn)
            finally:
                await conn.close()
        return asyncio.run(main())

    try:
        yield SimpleNamespace(client=client, run=run)
    finally:
        client.close()
        asyncio.run(db.drop())


def _rows(site, statement, params=()):
    async def query(conn):
        async with conn.cursor() as cur:
            await cur.execute(statement, params)
            rows = await cur.fetchall() if cur.description else None
        await conn.commit()
        return rows
    return site.run(query)


def _passkey(site, who: int, n: int = 1) -> int:
    async def add(conn):
        return await web_access.add_passkey(
            conn, who, credential_id=bytes([who, n]) * 16, public_key=b"public-key",
            sign_count=0, user_handle=await web_access.user_handle_for(conn, who))
    added = site.run(add)
    assert added
    return added


def _session(site, who: int, kind: str, passkey_id: int | None = None) -> str:
    """A session as each sign-in opens it: `link` (link and code), `computer`
    (a passkey on a computer, the browser rule) or `app` (a passkey on a phone)."""
    async def opened(conn):
        if kind == "link":
            return await web_access.open_session(conn, who, idle_seconds=3600)
        return await web_access.open_session(
            conn, who, idle_seconds=3600, method="passkey", passkey_id=passkey_id,
            rule="app" if kind == "app" else "browser",
            absolute_seconds=settings.web_app_session_seconds)
    return site.run(opened)


def _app(site, who: int = RUI, n: int = 1) -> tuple[str, int]:
    passkey_id = _passkey(site, who, n)
    return _session(site, who, "app", passkey_id), passkey_id


def _lock(site, token: str) -> None:
    _rows(site, "UPDATE web_sessions SET last_seen_at = NOW() - make_interval(secs => %s) "
                "WHERE token_digest = %s",
          (settings.web_app_lock_seconds + 60, web_access.digest(token)))


def _cookie(token: str) -> dict[str, str]:
    return {"cookie": f"{staff_web.COOKIE}={token}"}


def _assinar(site, token: str, *, csrf: str | None = None, headers: dict | None = None,
             **fields):
    p256dh, auth = _device_keys()
    body = {"csrf": staff_web.csrf_for(token) if csrf is None else csrf,
            "endpoint": ENDPOINT, "p256dh": p256dh, "auth": auth, **fields}
    return site.client.post("/web/avisos/assinar", json=body,
                            headers={**_cookie(token), **(headers or {})})


def _subscriptions(site):
    return _rows(site, "SELECT staff_id, passkey_id, endpoint, p256dh, auth "
                       "FROM push_subscriptions ORDER BY id")


# ── subscribing ──────────────────────────────────────────────────────────────


def test_subscribe_from_an_app_session_stores_its_passkey(site):
    token, passkey_id = _app(site)
    p256dh, auth = _device_keys()

    response = _assinar(site, token, p256dh=p256dh, auth=auth)

    assert response.status_code == 204, response.text
    rows = _subscriptions(site)
    assert len(rows) == 1
    staff_id, stored_passkey, endpoint, stored_p256dh, stored_auth = rows[0]
    assert (staff_id, stored_passkey, endpoint) == (RUI, passkey_id, ENDPOINT)
    assert _b64(bytes(stored_p256dh)) == p256dh and _b64(bytes(stored_auth)) == auth


def test_subscribe_refused_from_a_link_session(site):
    token = _session(site, RUI, "link")

    response = _assinar(site, token)

    assert response.status_code == 403
    assert _subscriptions(site) == []


def test_subscribe_refused_from_a_computer_passkey_session(site):
    """A passkey on a computer opens a browser-rule session: method passkey,
    rule browser, never locked. It gets no push in this version."""
    passkey_id = _passkey(site, RUI)
    token = _session(site, RUI, "computer", passkey_id)
    assert _rows(site, "SELECT method, rule FROM web_sessions WHERE token_digest = %s",
                 (web_access.digest(token),)) == [("passkey", "browser")]

    response = _assinar(site, token)

    assert response.status_code == 403
    assert _subscriptions(site) == []


def test_subscribe_refused_from_a_locked_session(site):
    token, _passkey_id = _app(site)
    _lock(site, token)

    response = _assinar(site, token)

    assert response.status_code == 403
    assert _subscriptions(site) == []


def test_subscribe_refused_without_a_session(site):
    response = site.client.post("/web/avisos/assinar", json={"endpoint": ENDPOINT})

    assert response.status_code == 403
    assert _subscriptions(site) == []


@pytest.mark.parametrize("csrf", ["", "wrong", staff_web.csrf_for("another session")])
def test_subscribe_refused_without_csrf(site, csrf):
    token, _passkey_id = _app(site)

    response = _assinar(site, token, csrf=csrf)

    assert response.status_code == 403
    assert _subscriptions(site) == []


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
    {"Origin": "https://evil.example"},
])
def test_subscribe_refused_from_another_site(site, headers):
    token, _passkey_id = _app(site)

    response = _assinar(site, token, headers=headers)

    assert response.status_code == 403
    assert _subscriptions(site) == []


@pytest.mark.parametrize("field,value", [
    ("p256dh", _b64(b"\x04" + b"\x01" * 63)),
    ("p256dh", _b64(b"\x04" + b"\x01" * 65)),
    ("p256dh", _b64(b"\x05" + b"\x01" * 64)),
    ("p256dh", _b64(b"\x04" + b"\x00" * 64)),
    ("auth", _b64(b"\x01" * 15)),
    ("auth", _b64(b"\x01" * 17)),
    ("auth", "not base64!"),
    ("auth", 16),
    ("p256dh", None),
])
def test_subscribe_refused_with_wrong_key_sizes(site, field, value):
    token, _passkey_id = _app(site)

    response = _assinar(site, token, **{field: value})

    assert response.status_code == 400
    assert _subscriptions(site) == []


@pytest.mark.parametrize("endpoint", [
    "https://xpush.apple.com/x", "https://push.apple.com.example/x",
    "http://web.push.apple.com/x", "https://web.push.apple.com:8443/x",
    "https://u:p@web.push.apple.com/x", 5,
])
def test_subscribe_refused_for_an_endpoint_off_the_list(site, endpoint):
    token, _passkey_id = _app(site)

    response = _assinar(site, token, endpoint=endpoint)

    assert response.status_code == 400
    assert _subscriptions(site) == []


def test_subscribe_accepts_another_apple_push_host(site):
    token, _passkey_id = _app(site)

    response = _assinar(site, token, endpoint="https://api.push.apple.com/abc")

    assert response.status_code == 204
    assert [row[2] for row in _subscriptions(site)] == ["https://api.push.apple.com/abc"]


def test_subscribe_refuses_a_body_over_4_kib(site):
    token, _passkey_id = _app(site)

    response = _assinar(site, token, padding="x" * 4096)

    assert response.status_code == 400
    assert _subscriptions(site) == []


def test_subscribe_rebinds_an_existing_endpoint(site):
    ruis, _ = _app(site, RUI)
    pauls, pauls_passkey = _app(site, PAULO)
    assert _assinar(site, ruis).status_code == 204
    p256dh, auth = _device_keys()

    response = _assinar(site, pauls, p256dh=p256dh, auth=auth)

    assert response.status_code == 204
    rows = _subscriptions(site)
    assert len(rows) == 1
    assert rows[0][:3] == (PAULO, pauls_passkey, ENDPOINT)
    assert _b64(bytes(rows[0][3])) == p256dh and _b64(bytes(rows[0][4])) == auth


def test_a_person_keeps_at_most_the_newest_subscriptions(site):
    token, _passkey_id = _app(site)
    for n in range(push_subscriptions.MAX_PER_PERSON + 2):
        assert _assinar(site, token,
                        endpoint=f"https://web.push.apple.com/{n}").status_code == 204

    kept = [row[2] for row in _subscriptions(site)]

    assert kept == [f"https://web.push.apple.com/{n}"
                    for n in range(2, push_subscriptions.MAX_PER_PERSON + 2)]


def test_subscribe_answers_404_outside_the_switch_or_without_keys(site, monkeypatch):
    token, _passkey_id = _app(site)

    monkeypatch.setattr(settings, "web_app_staff", str(PAULO))
    assert _assinar(site, token).status_code == 404
    monkeypatch.setattr(settings, "web_app_staff", f"{PAULO},{RUI}")
    monkeypatch.setattr(settings, "web_push_vapid_private_key", None)
    assert _assinar(site, token).status_code == 404
    assert site.client.get("/web/avisos/chave", headers=_cookie(token)).status_code == 404
    monkeypatch.setattr(settings, "web_push_vapid_private_key", SecretStr("private"))
    monkeypatch.setattr(settings, "web_app", "off")
    assert _assinar(site, token).status_code == 404
    assert _subscriptions(site) == []


def test_push_ready_needs_all_three_settings(monkeypatch):
    monkeypatch.setattr(settings, "web_push_vapid_public_key", PUBLIC_KEY)
    monkeypatch.setattr(settings, "web_push_vapid_private_key", SecretStr("private"))
    monkeypatch.setattr(settings, "web_push_subject", "mailto:contato@example.com")
    assert config.push_ready()
    for name, empty in (("web_push_vapid_public_key", ""),
                        ("web_push_vapid_private_key", SecretStr("")),
                        ("web_push_vapid_private_key", None),
                        ("web_push_subject", None),
                        ("web_push_subject", "contato@example.com")):
        with monkeypatch.context() as m:
            m.setattr(settings, name, empty)
            assert not config.push_ready(), name


# ── the key, and cancelling ─────────────────────────────────────────────────


def test_the_public_key_is_handed_to_an_app_session_only(site):
    app_token, _ = _app(site)
    link_token = _session(site, RUI, "link")

    given = site.client.get("/web/avisos/chave", headers=_cookie(app_token))
    refused = site.client.get("/web/avisos/chave", headers=_cookie(link_token))

    assert given.status_code == 200 and given.json() == {"chave": PUBLIC_KEY}
    assert given.headers["cache-control"].startswith("no-store")
    assert refused.status_code == 403


def test_cancel_removes_only_ones_own_subscription(site):
    ruis, _ = _app(site, RUI)
    pauls, _ = _app(site, PAULO)
    assert _assinar(site, ruis).status_code == 204

    by_paulo = site.client.post("/web/avisos/cancelar",
                                json={"csrf": staff_web.csrf_for(pauls), "endpoint": ENDPOINT},
                                headers=_cookie(pauls))
    assert len(_subscriptions(site)) == 1
    no_token = site.client.post("/web/avisos/cancelar", json={"endpoint": ENDPOINT},
                                headers=_cookie(ruis))
    assert no_token.status_code == 403 and len(_subscriptions(site)) == 1
    by_rui = site.client.post("/web/avisos/cancelar",
                              json={"csrf": staff_web.csrf_for(ruis), "endpoint": ENDPOINT},
                              headers=_cookie(ruis))

    assert by_paulo.status_code == 204
    assert by_rui.status_code == 204
    assert _subscriptions(site) == []


# ── what removes a subscription ─────────────────────────────────────────────


def test_removing_the_passkey_drops_its_subscriptions(site):
    token, passkey_id = _app(site, RUI, 1)
    other, _other_passkey = _app(site, RUI, 2)
    assert _assinar(site, token).status_code == 204
    assert _assinar(site, other, endpoint="https://web.push.apple.com/other").status_code == 204

    removed = site.run(lambda conn: web_access.remove_passkey(conn, RUI, passkey_id))

    assert removed
    assert [row[2] for row in _subscriptions(site)] == ["https://web.push.apple.com/other"]


def test_revoke_devices_drops_subscriptions(site):
    ruis, _ = _app(site, RUI)
    pauls, _ = _app(site, PAULO)
    assert _assinar(site, ruis).status_code == 204
    assert _assinar(site, pauls, endpoint="https://web.push.apple.com/paulo").status_code == 204

    site.run(lambda conn: web_access.revoke_devices(conn, RUI))

    assert [row[:1] + row[2:3] for row in _subscriptions(site)] == [
        (PAULO, "https://web.push.apple.com/paulo")]


def test_the_repository_reads_marks_and_drops(site):
    token, _ = _app(site)
    assert _assinar(site, token).status_code == 204

    own = site.run(lambda conn: push_subscriptions.of(conn, RUI))
    assert [(s.endpoint, len(s.p256dh), len(s.auth)) for s in own] == [(ENDPOINT, 65, 16)]
    assert site.run(lambda conn: push_subscriptions.of(conn, PAULO)) == []
    site.run(lambda conn: push_subscriptions.mark_ok(conn, own[0].id))
    assert _rows(site, "SELECT last_ok_at IS NOT NULL FROM push_subscriptions") == [(True,)]
    assert not site.run(lambda conn: push_subscriptions.unsubscribe(conn, PAULO, ENDPOINT))
    site.run(lambda conn: push_subscriptions.drop(conn, own[0].id))
    assert _subscriptions(site) == []


# ── the button ───────────────────────────────────────────────────────────────


def _assistant(site, token: str):
    return site.client.get("/web/assistente/", headers=_cookie(token))


def test_the_avisos_button_only_for_app_sessions(site, monkeypatch):
    app_token, _ = _app(site)
    computer = _session(site, RUI, "computer", _passkey(site, RUI, 2))
    link = _session(site, RUI, "link")

    offered = _assistant(site, app_token)

    assert offered.status_code == 200
    assert "data-avisos " in offered.text and "Ativar avisos" in offered.text
    button = offered.text.split("data-avisos ", 1)[1].split(">", 1)[0]
    assert f"data-csrf='{staff_web.csrf_for(app_token)}'" in button
    assert " hidden" in button, "the script shows it, only in the installed app"
    # A nav item is display:flex, which would beat the bare attribute.
    assert ".sidebar-nav [data-avisos][hidden]{display:none}" in offered.text
    assert html.escape(DENIED, quote=True) in offered.text
    for token in (computer, link):
        page = _assistant(site, token)
        assert page.status_code == 200
        assert "data-avisos " not in page.text and "data-avisos-nota" not in page.text
        assert "Ativar avisos" not in page.text
    monkeypatch.setattr(settings, "web_push_subject", None)
    assert "Ativar avisos" not in _assistant(site, app_token).text


def test_the_avisos_button_never_on_a_locked_session(site):
    token, _ = _app(site)
    _lock(site, token)

    response = site.client.get("/web/assistente/",
                               headers={**_cookie(token), "Sec-Fetch-Mode": "navigate"})

    assert response.status_code == 303
    assert response.headers["location"].startswith("/web/faceid/")
    assert "Ativar avisos" not in response.text


def test_notices_can_be_enabled_without_access_to_the_assistant(site, monkeypatch):
    token, _ = _app(site)

    monkeypatch.setattr(settings, "web_assistant", "off")
    assert _assistant(site, token).status_code == 404
    devices = site.client.get("/web/aparelhos/", headers=_cookie(token))

    assert devices.status_code == 200
    assert "Ativar avisos" in devices.text
    button = devices.text.split("data-avisos ", 1)[1].split(">", 1)[0]
    assert f"data-csrf='{staff_web.csrf_for(token)}'" in button and " hidden" in button
    assert ".sidebar-nav [data-avisos][hidden]{display:none}" in devices.text, (
        "without it the nav item's display:flex shows the button in every browser")
    assert "<script type=module src='/web/notices.js'></script>" in devices.text
    assert devices.headers["content-security-policy"] == (
        pages.HEADERS["Content-Security-Policy"]
        + "; script-src 'self'; connect-src 'self'; worker-src 'self'")
    script = site.client.get("/web/notices.js")
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert script.headers["x-content-type-options"] == "nosniff"
    assert script.content == (Path(staff_web.__file__).parent / "static"
                              / "notices.js").read_bytes()


def test_the_aparelhos_page_offers_notices_only_to_an_unlocked_app_session(site, monkeypatch):
    """The same rule as the assistant page; otherwise the page loads no
    notices script, and its policy is the sidebar's: its own scripts, and
    requests to this host only for the theme switch, no service worker."""
    policy = pages.HEADERS["Content-Security-Policy"]
    sidebar_policy = policy + "; script-src 'self'; connect-src 'self'"
    app_token, _ = _app(site)
    computer = _session(site, RUI, "computer", _passkey(site, RUI, 2))
    link = _session(site, RUI, "link")

    for token in (computer, link):
        page = site.client.get("/web/aparelhos/", headers=_cookie(token))
        assert page.status_code == 200
        assert "data-avisos" not in page.text and "Ativar avisos" not in page.text
        assert "/web/notices.js" not in page.text
        assert page.headers["content-security-policy"] == sidebar_policy
    assert pages.HEADERS["Content-Security-Policy"] == policy, "the shared policy is untouched"

    monkeypatch.setattr(settings, "web_push_subject", None)
    off = site.client.get("/web/aparelhos/", headers=_cookie(app_token))
    assert off.status_code == 200 and "Ativar avisos" not in off.text
    assert off.headers["content-security-policy"] == sidebar_policy

    monkeypatch.setattr(settings, "web_push_subject", "mailto:contato@example.com")
    _lock(site, app_token)
    locked = site.client.get("/web/aparelhos/",
                             headers={**_cookie(app_token), "Sec-Fetch-Mode": "navigate"})
    assert locked.status_code == 303
    assert locked.headers["location"].startswith("/web/faceid/")
    assert "Ativar avisos" not in locked.text


def test_the_notices_script_is_not_served_with_the_app_off(site, monkeypatch):
    monkeypatch.setattr(settings, "web_app", "off")
    assert site.client.get("/web/notices.js").status_code == 404


# ── the script, run ──────────────────────────────────────────────────────────
# notices.js runs under Node (tests/fixtures/notices_harness.mjs), with a
# stand-in for the button, display mode, the push manager and fetch. What is
# asserted is what the page shows and what it sends, not the source text.

NODE = shutil.which("node")
HARNESS = Path(__file__).parent / "fixtures" / "notices_harness.mjs"
SCRIPT = Path(staff_web.__file__).parent / "static" / "notices.js"
needs_node = pytest.mark.skipif(
    NODE is None, reason="runs the page's script under Node, which is not installed here")
HELD = {"endpoint": ENDPOINT, "p256dh": "P", "auth": "A"}


def _script(**scenario) -> dict:
    assert NODE
    done = subprocess.run([NODE, str(HARNESS), str(SCRIPT), json.dumps(scenario)],
                          capture_output=True, text=True, timeout=30, check=True)
    return json.loads(done.stdout)


@needs_node
def test_the_button_shows_only_in_the_installed_app():
    tab = _script(standalone=False)
    app = _script(standalone=True)

    assert tab["button_hidden"] and tab["requests"] == []
    assert not app["button_hidden"]
    assert [r["url"] for r in app["requests"]] == ["/web/avisos/chave"]


@needs_node
def test_a_held_subscription_is_handed_back_instead_of_offered():
    kept = _script(standalone=True, held=HELD, csrf="token")

    assert kept["button_hidden"] and kept["unsubscribed"] == []
    assert kept["requests"] == [{"url": "/web/avisos/chave", "method": "GET",
                                 "body": None}, {"url": "/web/avisos/assinar", "method": "POST",
                                 "body": {"csrf": "token", **HELD}}]


@needs_node
@pytest.mark.parametrize("status", [403, 404])
def test_a_session_refusal_keeps_the_held_subscription(status):
    page = _script(standalone=True, held=HELD, post_status=status)

    assert page["button_hidden"] and page["unsubscribed"] == []


@needs_node
def test_a_held_subscription_refused_as_invalid_is_replaced():
    page = _script(standalone=True, held=HELD, post_status=400)

    assert page["unsubscribed"] == [ENDPOINT]
    assert not page["button_hidden"]


@needs_node
def test_a_tap_subscribes_and_hands_it_to_the_server():
    page = _script(standalone=True, click=True, csrf="token",
                   new={"endpoint": "https://web.push.apple.com/new", "p256dh": "P", "auth": "A"})

    assert page["subscribed"] == 1 and page["subscribe_options"] == {
        "userVisibleOnly": True, "applicationServerKey": [4, 0, 0]}
    assert page["requests"][-1]["body"] == {"csrf": "token", "endpoint":
                                            "https://web.push.apple.com/new",
                                            "p256dh": "P", "auth": "A"}
    assert page["button_hidden"] and page["note"] == "ATIVADO"


@needs_node
def test_denied_permission_shows_the_copy():
    page = _script(standalone=True, permission="denied")

    assert page["button_hidden"] and page["note"] == "NEGADO"


@needs_node
def test_notices_come_back_after_revocation_and_re_enrolment(site):
    """`/web encerrar` deletes the server's row and leaves the device's
    subscription. With Face ID turned on again, opening the app hands that
    subscription back, and the server stores it under the new passkey."""
    token, _ = _app(site)
    p256dh, auth = _device_keys()
    assert _assinar(site, token, p256dh=p256dh, auth=auth).status_code == 204
    site.run(lambda conn: web_access.revoke_devices(conn, RUI))
    assert _subscriptions(site) == []

    new_token, new_passkey = _app(site, RUI, 2)
    page = _assistant(site, new_token)
    assert "data-avisos " in page.text, "the app's page carries the button again"
    sent = _script(standalone=True, csrf=staff_web.csrf_for(new_token),
                   held={"endpoint": ENDPOINT, "p256dh": p256dh, "auth": auth})
    [request] = [r for r in sent["requests"] if r["method"] == "POST"]
    answer = site.client.request(request["method"], request["url"], json=request["body"],
                                 headers=_cookie(new_token))

    assert answer.status_code == 204
    assert [row[:3] for row in _subscriptions(site)] == [(RUI, new_passkey, ENDPOINT)]


def test_handing_back_an_unchanged_subscription_keeps_its_dates(site):
    token, _ = _app(site)
    p256dh, auth = _device_keys()
    assert _assinar(site, token, p256dh=p256dh, auth=auth).status_code == 204
    own = site.run(lambda conn: push_subscriptions.of(conn, RUI))
    site.run(lambda conn: push_subscriptions.mark_ok(conn, own[0].id))
    before = _rows(site, "SELECT created_at, last_ok_at FROM push_subscriptions")

    assert _assinar(site, token, p256dh=p256dh, auth=auth).status_code == 204

    after = _rows(site, "SELECT created_at, last_ok_at FROM push_subscriptions")
    assert after == before and after[0][1] is not None


@needs_node
def test_key_replacement_drops_the_old_subscription_and_requires_a_fresh_tap():
    page = _script(standalone=True, held=HELD, key="BAEC")
    assert page["unsubscribed"] == [ENDPOINT]
    assert not page["button_hidden"] and page["subscribed"] == 0
    assert all(r["method"] == "GET" for r in page["requests"])
    tapped = _script(standalone=True, held=HELD, key="BAEC", click=True,
                     new={**HELD, "endpoint": "https://web.push.apple.com/new"})
    assert tapped["subscribe_options"]["applicationServerKey"] == [4, 1, 2]
    assert tapped["button_hidden"] and tapped["note"] == "ATIVADO"


@needs_node
def test_a_key_fetch_refusal_preserves_the_device_subscription():
    page = _script(standalone=True, held=HELD, key_status=403)
    assert page["button_hidden"] and page["unsubscribed"] == []
    assert all(r["method"] == "GET" for r in page["requests"])


@needs_node
def test_a_subscription_that_does_not_name_its_key_is_handed_back():
    """A browser without `options` is taken at its word, not asked for a tap
    on every open."""
    page = _script(standalone=True, held={**HELD, "no_options": True}, csrf="token")

    assert page["button_hidden"] and page["unsubscribed"] == []
    assert page["requests"][-1] == {"url": "/web/avisos/assinar", "method": "POST",
                                    "body": {"csrf": "token", **HELD}}


@needs_node
def test_a_held_subscription_for_the_current_key_is_kept():
    """The other side of the key change: the same key bytes, written
    differently, are the same key."""
    page = _script(standalone=True, held={**HELD, "applicationKey": "BAEC"}, key="BAEC",
                   csrf="token")

    assert page["button_hidden"] and page["unsubscribed"] == []
    assert [r["method"] for r in page["requests"]] == ["GET", "POST"]
