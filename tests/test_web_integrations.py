"""The integrations page, against a real Postgres.

Each card is shown to whom its action allows; every change is a POST from
this site with the form token, for the session's own person; Microsoft is
connected from the page with the device code kept on the server.
"""
import asyncio
import contextlib
import re
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.config import settings
from core.integrations import microsoft
from core.repositories import microsoft_accounts, pje_credentials, publications, web_access
from core.services import authorization, panel_intake, work_hours
from core.web import assistant, integrations, staff_web

PAULO, RUI, ANA, SEM_CHAT = 1, 4, 5, 6
CNJ_DIGITS = "10000003720248260100"
HOME = "/web/integracoes/"


class _Microsoft:
    def __init__(self):
        self.answer: object = microsoft.SignInPending("authorization_pending")
        self.polled: list[str] = []

    async def start(self):
        return microsoft.DeviceFlow(user_code="<b>AB</b>-12",
                                    verification_uri="https://microsoft.com/devicelogin",
                                    device_code="SECRET-DEVICE-CODE", interval=5,
                                    expires_in=900)

    async def poll(self, device_code):
        self.polled.append(device_code)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    async def whoami(self, _token):
        return "rui@x.adv.br"


@pytest.fixture
def site(db, monkeypatch):
    async def build():
        conn = await db.connect()
        try:
            await db.build(conn)
            await microsoft_accounts.ensure_microsoft_schema(conn)
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

    queued: list = []

    async def enqueue(update, *, deferred):
        queued.append((update, deferred))
        return True

    fake = _Microsoft()
    monkeypatch.setattr(settings, "web_enabled", True)
    monkeypatch.setattr(settings, "web_assistant", "on")
    monkeypatch.setattr(settings, "web_assistant_staff", "")
    monkeypatch.setattr(settings, "web_explorer", "on")
    monkeypatch.setattr(settings, "pje_enabled", True)
    monkeypatch.setattr(settings, "pje_cred_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "ms_enabled", True)
    monkeypatch.setattr(settings, "ms_token_key", Fernet.generate_key().decode())
    monkeypatch.setattr(microsoft, "configured", lambda: True)
    monkeypatch.setattr(microsoft, "start_device_flow", fake.start)
    monkeypatch.setattr(microsoft, "poll_once", fake.poll)
    monkeypatch.setattr(microsoft, "whoami", fake.whoami)
    for module in (staff_web, assistant, integrations):
        monkeypatch.setattr(module, "get_pool", get_pool)
    monkeypatch.setattr(panel_intake.update_queue, "enqueue_update", enqueue)
    monkeypatch.setattr(work_hours, "is_office_hours", lambda: True)

    app = FastAPI()
    app.include_router(staff_web.router)
    app.include_router(assistant.router)
    app.include_router(integrations.router)
    staff_web.install(app)
    client = TestClient(app, base_url="https://testserver", follow_redirects=False)

    def run(fn):
        async def main():
            conn = await db.connect()
            try:
                return await fn(conn)
            finally:
                await conn.close()
        return asyncio.run(main())

    try:
        yield type("Site", (), {"client": client, "run": staticmethod(run),
                                "queued": queued, "ms": fake})
    finally:
        client.close()
        asyncio.run(db.drop())


def _token(site, who: int) -> str:
    return site.run(lambda conn: web_access.open_session(conn, who, idle_seconds=3600))


def _get(site, who: int | None, path: str = HOME, token: str | None = None):
    headers = {}
    if who is not None:
        headers["cookie"] = f"{staff_web.COOKIE}={token or _token(site, who)}"
    return site.client.get(path, headers=headers)


def _post(site, who: int | None, path: str, data: dict[str, str] | None = None, *,
          csrf: str | None = None, site_header: str = "same-origin", token: str | None = None):
    headers = {"sec-fetch-site": site_header}
    form = dict(data or {})
    if who is not None:
        token = token or _token(site, who)
        headers["cookie"] = f"{staff_web.COOKIE}={token}"
        form["csrf"] = staff_web.csrf_for(token) if csrf is None else csrf
    return site.client.post(path, data=form, headers=headers)


def _execute(site, statement: str, params=()) -> None:
    async def run(conn):
        async with conn.cursor() as cur:
            await cur.execute(statement, params)
        await conn.commit()
    site.run(run)


def _rows(site, statement: str, params=()):
    async def run(conn):
        async with conn.cursor() as cur:
            await cur.execute(statement, params)
            return await cur.fetchall()
    return site.run(run)


def _enrol(site, who: int) -> None:
    site.run(lambda conn: pje_credentials.save_credential(
        conn, who, cpf="12345678901", password="senha", court="TJPA"))


def _connect(site, who: int, name: str = "rui@x.adv.br") -> None:
    site.run(lambda conn: microsoft_accounts.save_account(
        conn, who, account_name=name, refresh_token="t"))


def _due(site) -> None:
    _execute(site, "UPDATE microsoft_sign_ins SET next_poll_at = NOW() - INTERVAL '1 second'")


# ── the page and its cards ──────────────────────────────────────────────────


def test_the_page_shows_a_card_per_integration(site):
    page = _get(site, RUI).text

    for title in ("PJe", "Microsoft", "DJEN", "Telegram"):
        assert f"<h2>{title}" in page
    assert "Não cadastrado" in page and "Não conectada" in page
    assert "Ligado ao bot" in page


def test_without_a_session_the_page_is_refused(site):
    response = _get(site, None)

    assert response.status_code in (401, 303)
    assert "<h2>PJe" not in response.text


@pytest.mark.parametrize("denied, title", [
    ("pje.connect", "PJe"), ("microsoft.connect", "Microsoft"),
    ("publications.watch", "DJEN"),
])
def test_a_card_shows_only_when_its_action_is_allowed(site, monkeypatch, denied, title):
    real = authorization.decide

    def deny(staff, action):
        if action == denied:
            return authorization.Decision(allowed=False, action=action, reason="não")
        return real(staff, action)

    monkeypatch.setattr(authorization, "decide", deny)
    page = _get(site, RUI).text

    assert f"<h2>{title}" not in page
    assert sum(f"<h2>{t}" in page for t in ("PJe", "Microsoft", "DJEN", "Telegram")) == 3


def test_services_not_configured_say_so_without_buttons(site, monkeypatch):
    monkeypatch.setattr(settings, "pje_enabled", False)
    monkeypatch.setattr(microsoft, "configured", lambda: False)

    page = _get(site, RUI).text

    assert page.count("Não disponível neste servidor") == 2
    assert "microsoft/conectar" not in page and "action='/web/pje/'" not in page


def test_the_telegram_card_without_a_chat(site):
    assert "Peça acesso pelo bot" in _get(site, SEM_CHAT).text


def test_the_page_allows_its_own_script_and_fetches(site):
    policy = _get(site, RUI).headers["content-security-policy"]

    assert "script-src 'self'" in policy and "connect-src 'self'" in policy


# ── PJe ─────────────────────────────────────────────────────────────────────


def test_an_enrolled_pje_shows_since_when_and_offers_replace_and_remove(site):
    _enrol(site, RUI)

    page = _get(site, RUI).text

    assert "Cadastrado em" in page and "Substituir" in page
    assert "action='/web/integracoes/pje/remover'" in page


def test_removing_pje_asks_first_then_removes(site):
    _enrol(site, RUI)

    first = _post(site, RUI, HOME + "pje/remover")
    assert first.status_code == 200 and "Remover o seu acesso ao PJe?" in first.text
    assert _rows(site, "SELECT count(*) FROM pje_credentials") == [(1,)]

    second = _post(site, RUI, HOME + "pje/remover", {"confirmar": "1"})
    assert second.status_code == 303
    assert second.headers["location"] == HOME + "?aviso=pje_removido"
    assert _rows(site, "SELECT count(*) FROM pje_credentials") == [(0,)]


def test_one_person_never_removes_anothers_pje(site):
    _enrol(site, RUI)

    _post(site, ANA, HOME + "pje/remover", {"confirmar": "1", "staff_id": str(RUI)})

    assert _rows(site, "SELECT staff_id FROM pje_credentials") == [(RUI,)]
    assert "Cadastrado em" not in _get(site, ANA).text


def test_pje_enrolment_is_offered_only_with_the_assistant(site, monkeypatch):
    monkeypatch.setattr(settings, "web_assistant", "pilot")
    monkeypatch.setattr(settings, "web_assistant_staff", str(PAULO))

    page = _get(site, RUI).text

    assert "action='/web/pje/'" not in page
    assert "Cadastre pelo Telegram com /pje" in page


# ── Microsoft ───────────────────────────────────────────────────────────────


def test_connecting_shows_the_code_and_never_the_device_code(site):
    response = _post(site, RUI, HOME + "microsoft/conectar")
    page = _get(site, RUI, response.headers["location"]).text

    assert response.status_code == 303
    assert "&lt;b&gt;AB&lt;/b&gt;-12" in page and "<b>AB</b>" not in page
    assert "href='https://microsoft.com/devicelogin'" in page
    assert "Já autorizei" in page
    assert "SECRET-DEVICE-CODE" not in page
    stored = _rows(site, "SELECT device_code_sealed FROM microsoft_sign_ins")
    assert b"SECRET-DEVICE-CODE" not in bytes(stored[0][0])


def test_checking_by_script_answers_the_outcome_word(site):
    _post(site, RUI, HOME + "microsoft/conectar")
    _due(site)

    waiting = _post(site, RUI, HOME + "microsoft/verificar", {"via": "script"})
    assert waiting.text == "aguardando"

    site.ms.answer = microsoft.Tokens(access_token="a", refresh_token="r")
    _due(site)
    done = _post(site, RUI, HOME + "microsoft/verificar", {"via": "script"})

    assert done.text == "conectada"
    assert "Conectada: rui@x.adv.br" in _get(site, RUI).text


def test_checking_by_hand_comes_back_with_a_note(site):
    _post(site, RUI, HOME + "microsoft/conectar")
    _due(site)
    site.ms.answer = microsoft.SignInDeclined("authorization_declined")

    response = _post(site, RUI, HOME + "microsoft/verificar")

    assert response.headers["location"] == HOME + "?aviso=ms_recusada"
    assert integrations.NOTES["ms_recusada"] in _get(site, RUI, response.headers["location"]).text


def test_disconnecting_drops_the_account_and_a_waiting_code(site):
    _connect(site, RUI)
    _execute(site, "INSERT INTO microsoft_sign_ins (staff_id, flow_id, device_code_sealed, "
                   "user_code, verification_uri, interval_seconds, next_poll_at, expires_at) "
                   "VALUES (%s, gen_random_uuid(), '\\x00', 'X', 'https://m', 5, NOW(), "
                   "NOW() + INTERVAL '10 minutes')", (RUI,))

    response = _post(site, RUI, HOME + "microsoft/desconectar")

    assert response.headers["location"] == HOME + "?aviso=ms_desconectada"
    assert _rows(site, "SELECT count(*) FROM microsoft_accounts") == [(0,)]
    assert _rows(site, "SELECT count(*) FROM microsoft_sign_ins") == [(0,)]


def test_one_person_never_sees_or_disconnects_anothers_microsoft(site):
    _connect(site, RUI, "rui@x.adv.br")

    assert "rui@x.adv.br" not in _get(site, ANA).text
    _post(site, ANA, HOME + "microsoft/desconectar")

    assert _rows(site, "SELECT staff_id FROM microsoft_accounts") == [(RUI,)]


def test_a_deactivated_person_cannot_check(site):
    token = _token(site, RUI)
    _post(site, RUI, HOME + "microsoft/conectar", token=token)
    _due(site)
    _execute(site, "UPDATE staff SET active = FALSE WHERE id = %s", (RUI,))

    response = _post(site, RUI, HOME + "microsoft/verificar", {"via": "script"}, token=token)

    assert response.status_code == 401
    assert site.ms.polled == []


def test_microsoft_in_the_assistants_chat_points_here(site):
    response = _post(site, RUI, "/web/assistente/mensagem", {"texto": "/microsoft"})
    page = _get(site, RUI, response.headers["location"]).text

    assert response.headers["location"] == "/web/assistente/?aviso=microsoft_page#escrever"
    assert "Conecte a Microsoft na aba Integrações." in page
    assert site.queued == []


# ── DJEN ────────────────────────────────────────────────────────────────────


def _watch(site, who: int, number: str = "123456", uf: str = "SP") -> None:
    async def seed(conn):
        await publications.watch_oab(conn, who, number, uf)
        await publications.watch_process(conn, who, CNJ_DIGITS)
        await conn.commit()
    site.run(seed)


def test_djen_lists_only_own_follows_with_controls(site):
    _watch(site, RUI)
    _watch(site, ANA, "999999", "RJ")

    page = _get(site, RUI).text

    assert "value='oab:123456/SP'" in page and "999999" not in page
    assert "name=acao value='oab'" in page and "name=acao value='acompanhar'" in page


def test_djen_without_the_assistant_is_read_only(site, monkeypatch):
    monkeypatch.setattr(settings, "web_assistant", "pilot")
    monkeypatch.setattr(settings, "web_assistant_staff", str(PAULO))
    _watch(site, RUI)

    page = _get(site, RUI).text

    assert "OAB 123456/SP" in page
    assert "/web/assistente/acao" not in page
    assert "Gerencie pelo Telegram" in page


# ── every change ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["pje/remover", "microsoft/conectar",
                                  "microsoft/verificar", "microsoft/desconectar"])
@pytest.mark.parametrize("how", ["missing", "cross-site", "no session"])
def test_every_post_needs_csrf_and_this_site(site, path, how):
    _enrol(site, RUI)
    _connect(site, RUI)
    if how == "missing":
        response = _post(site, RUI, HOME + path, {"confirmar": "1"}, csrf="")
    elif how == "cross-site":
        response = _post(site, RUI, HOME + path, {"confirmar": "1"}, site_header="cross-site")
    else:
        response = _post(site, None, HOME + path, {"confirmar": "1"})

    assert response.status_code in (401, 403)
    assert _rows(site, "SELECT count(*) FROM pje_credentials") == [(1,)]
    assert _rows(site, "SELECT count(*) FROM microsoft_accounts") == [(1,)]
    assert _rows(site, "SELECT count(*) FROM microsoft_sign_ins") == [(0,)]


def test_an_unknown_note_shows_nothing(site):
    page = _get(site, RUI, HOME + "?aviso=<script>").text

    assert "<script>" not in page.split("</head>", 1)[1].replace(
        "<script type=module", "")


# ── the menu ────────────────────────────────────────────────────────────────


def test_both_menus_lead_here(site, monkeypatch):
    monkeypatch.setattr(staff_web, "web_app_on_for", lambda _id: True)
    assert "href='/web/integracoes/'" in _get(site, RUI).text
    monkeypatch.setattr(staff_web, "web_app_on_for", lambda _id: False)
    assert "href='/web/integracoes/'" in _get(site, RUI).text


def test_the_page_says_when_the_code_runs_out(site):
    _post(site, RUI, HOME + "microsoft/conectar")

    page = _get(site, RUI).text
    expires = re.search(r"data-expira=(\d+)", page)

    assert expires
    left = int(expires.group(1)) - datetime.now(timezone.utc).timestamp()
    assert 800 < left <= 900


@pytest.mark.parametrize("denied, path", [
    ("pje.connect", "pje/remover"), ("microsoft.connect", "microsoft/conectar"),
    ("microsoft.connect", "microsoft/desconectar"),
])
def test_a_change_the_rank_does_not_allow_is_refused(site, monkeypatch, denied, path):
    _enrol(site, RUI)
    _connect(site, RUI)
    real = authorization.decide

    def deny(staff, action):
        if action == denied:
            return authorization.Decision(allowed=False, action=action, reason="não")
        return real(staff, action)

    monkeypatch.setattr(authorization, "decide", deny)
    response = _post(site, RUI, HOME + path, {"confirmar": "1"})

    assert response.headers["location"] == HOME + "?aviso=refused"
    assert _rows(site, "SELECT count(*) FROM pje_credentials") == [(1,)]
    assert _rows(site, "SELECT count(*) FROM microsoft_accounts") == [(1,)]


def test_the_account_name_is_escaped(site):
    _connect(site, RUI, "<img src=x onerror=alert(1)>'\"@x")

    page = _get(site, RUI).text

    assert "<img src=x" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;&#x27;&quot;@x" in page


def test_a_connected_account_can_be_dropped_even_when_microsoft_is_off(site, monkeypatch):
    _connect(site, RUI)
    monkeypatch.setattr(microsoft, "configured", lambda: False)

    page = _get(site, RUI).text

    assert "Conectada: rui@x.adv.br" in page and "microsoft/desconectar" in page
    assert "microsoft/conectar" not in page
