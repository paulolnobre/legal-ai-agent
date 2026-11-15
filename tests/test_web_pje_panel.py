"""PJe enrolment from the panel, against a real Postgres.

The panel's `/pje` answer names `/web/pje/`, which holds nothing. Only a
POST from this site, with the session's form token, by somebody `pje.connect`
allows, opens an enrolment, and the token leaves only in that answer's
address. A GET creates nothing.
"""
import asyncio
import contextlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.config import settings
from core.repositories import pje_credentials, web_access
from core.repositories.staff import ROLE_LAWYER
from core.services import authorization
from core.web import assistant, pje_panel, staff_web

PAULO, RUI, ANA = 1, 4, 5
PAGE = "/web/pje/"


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
    monkeypatch.setattr(settings, "web_assistant", "on")
    monkeypatch.setattr(settings, "web_assistant_staff", "")
    monkeypatch.setattr(settings, "pje_enabled", True)
    monkeypatch.setattr(staff_web, "get_pool", get_pool)
    monkeypatch.setattr(pje_panel, "get_pool", get_pool)

    app = FastAPI()
    app.include_router(staff_web.router)
    app.include_router(pje_panel.router)
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
        yield SimpleNamespace(client=client, run=run)
    finally:
        client.close()
        asyncio.run(db.drop())


def _token(site, who: int) -> str:
    return site.run(lambda conn: web_access.open_session(conn, who, idle_seconds=3600))


def _get(site, who: int | None):
    headers = {} if who is None else {"cookie": f"{staff_web.COOKIE}={_token(site, who)}"}
    return site.client.get(PAGE, headers=headers)


def _post(site, who: int | None, *, site_header: str | None = "same-origin",
          csrf: str | None = None, origin: str | None = None):
    headers = {}
    if site_header is not None:
        headers["sec-fetch-site"] = site_header
    if origin is not None:
        headers["origin"] = origin
    form = {}
    if who is not None:
        token = _token(site, who)
        headers["cookie"] = f"{staff_web.COOKIE}={token}"
        form["csrf"] = staff_web.csrf_for(token) if csrf is None else csrf
    return site.client.post(PAGE, data=form, headers=headers)


def _enrolments(site) -> list[tuple]:
    async def query(conn):
        async with conn.cursor() as cur:
            await cur.execute("SELECT token_digest, staff_id, used_at FROM pje_enrolments")
            return await cur.fetchall()
    return site.run(query)


# ── the GET ─────────────────────────────────────────────────────────────────


def test_the_page_shows_a_button_and_creates_nothing(site):
    response = _get(site, RUI)

    assert response.status_code == 200
    assert f"<form method=post action='{PAGE}'>" in response.text
    assert "name=csrf" in response.text
    assert "sidebar-nav" in response.text or "<nav>" in response.text
    assert _enrolments(site) == []


def test_without_a_session_the_page_is_signed_out(site):
    assert _get(site, None).status_code == 401
    assert _enrolments(site) == []


def test_switched_off_the_page_does_not_exist(site, monkeypatch):
    monkeypatch.setattr(settings, "web_assistant", "off")

    assert _get(site, RUI).status_code == 404
    assert _post(site, RUI).status_code == 404
    assert _enrolments(site) == []


def test_outside_the_pilot_the_page_does_not_exist(site, monkeypatch):
    monkeypatch.setattr(settings, "web_assistant", "pilot")
    monkeypatch.setattr(settings, "web_assistant_staff", str(PAULO))

    assert _get(site, RUI).status_code == 404
    assert _post(site, RUI).status_code == 404
    assert _get(site, PAULO).status_code == 200
    assert _enrolments(site) == []


def test_pje_switched_off_opens_nothing(site, monkeypatch):
    monkeypatch.setattr(settings, "pje_enabled", False)

    assert _post(site, RUI).status_code == 404
    assert "não está configurado" in _get(site, RUI).text
    assert _enrolments(site) == []


# ── the POST ────────────────────────────────────────────────────────────────


def test_the_post_opens_one_enrolment_and_goes_to_its_form(site):
    response = _post(site, RUI)

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/pje/")
    token = location.removeprefix("/pje/")
    rows = _enrolments(site)
    assert rows == [(pje_credentials.token_digest(token), RUI, None)]
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["referrer-policy"] == "no-referrer"


def test_the_link_it_opens_is_the_bots_own_kind(site):
    """Spendable once, by the form `/pje/{token}` already serves."""
    token = _post(site, RUI).headers["location"].removeprefix("/pje/")

    async def spend(conn):
        return (await pje_credentials.spend_enrolment(conn, token),
                await pje_credentials.spend_enrolment(conn, token))

    assert site.run(spend) == (RUI, None)


def test_a_second_press_replaces_the_first_link(site):
    first = _post(site, RUI).headers["location"].removeprefix("/pje/")
    second = _post(site, RUI).headers["location"].removeprefix("/pje/")

    assert first != second
    assert _enrolments(site) == [(pje_credentials.token_digest(second), RUI, None)]


@pytest.mark.parametrize("how", ["cross-site", "same-site", "foreign origin",
                                 "missing csrf", "wrong csrf", "non-ascii csrf",
                                 "no session"])
def test_a_post_not_from_this_page_opens_nothing(site, how):
    if how == "cross-site":
        response = _post(site, RUI, site_header="cross-site")
    elif how == "same-site":
        response = _post(site, RUI, site_header="same-site")
    elif how == "foreign origin":
        response = _post(site, RUI, origin="https://outro.exemplo")
    elif how == "missing csrf":
        response = _post(site, RUI, csrf="")
    elif how == "wrong csrf":
        response = _post(site, RUI, csrf=staff_web.csrf_for("outra-sessao"))
    elif how == "non-ascii csrf":
        response = _post(site, RUI, csrf="é")
    else:
        response = _post(site, None)

    assert response.status_code == 403
    assert "location" not in response.headers
    assert _enrolments(site) == []


def test_another_persons_form_token_opens_nothing(site):
    other = _token(site, PAULO)

    response = _post(site, RUI, csrf=staff_web.csrf_for(other))

    assert response.status_code == 403
    assert _enrolments(site) == []


# ── who may ─────────────────────────────────────────────────────────────────


def test_an_intern_without_pje_connect_is_refused(site, monkeypatch):
    """The page asks `authorization.decide` by the action's name: moved to
    the lawyers, the intern is refused at the page and at the POST."""
    monkeypatch.setitem(authorization._REQUIRED_RANK, "pje.connect", ROLE_LAWYER)

    page = _get(site, ANA)
    posted = _post(site, ANA)

    assert page.status_code == 403 and posted.status_code == 403
    assert "Este recurso é para advogados" in page.text
    assert "location" not in posted.headers
    assert "<form method=post action='/web/pje/'>" not in page.text
    assert _enrolments(site) == []
    assert _post(site, RUI).status_code == 303


def test_an_intern_enrols_while_pje_connect_is_every_ranks(site):
    response = _post(site, ANA)

    assert response.status_code == 303
    assert [row[1] for row in _enrolments(site)] == [ANA]


def test_a_person_deactivated_since_sign_in_opens_nothing(site):
    token = _token(site, RUI)

    async def deactivate(conn):
        async with conn.cursor() as cur:
            await cur.execute("UPDATE staff SET active = FALSE WHERE id = %s", (RUI,))
        await conn.commit()

    site.run(deactivate)
    response = site.client.post(PAGE, data={"csrf": staff_web.csrf_for(token)},
                                headers={"sec-fetch-site": "same-origin",
                                         "cookie": f"{staff_web.COOKIE}={token}"})

    assert response.status_code == 401
    assert _enrolments(site) == []


# ── the panel's answer, linked ──────────────────────────────────────────────


def test_the_bots_answer_links_the_page_at_its_fixed_address():
    text = ("Para cadastrar, abra a página de cadastro no painel:\n\n"
            "https://agente.exemplo.br/web/pje/\n\nDigite a senha só na página.")

    rendered = assistant._bot_text(text, link_sources=False)

    assert "<a href='/web/pje/'>https://agente.exemplo.br/web/pje/</a>" in rendered


@pytest.mark.parametrize("text", [
    "https://x.exemplo/web/pje/abc",
    "veja /web/pje/'><script>",
    "x/web/pje/",
])
def test_nothing_else_is_linked_as_the_page(text):
    rendered = assistant._bot_text(text, link_sources=False)

    assert "<a " not in rendered
    assert "<script>" not in rendered
