"""The Processos page, against a real Postgres.

Each person sees their own cases and their teams'; every change is a POST
from this site with the form token, for the session's own person; a counted
deadline is shown before it is saved.
"""
import asyncio
import contextlib
import re
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.config import settings
from core.repositories import firm_holidays, processes, web_access
from core.repositories.teams import scope_for
from core.services.deadlines import Regime, calcular_prazo, calendario_do_escritorio
from core.web import processes as page_module
from core.web import staff_web

PAULO, RUI, ANA = 1, 4, 5
CIVEL, TRABALHISTA = 10, 11
CNJ = "1000000-37.2024.8.26.0100"
CNJ_DIGITS = "10000003720248260100"
WRONG_DIGIT = "1000000-99.2024.8.26.0100"
HOME = "/web/processos/"


@pytest.fixture
def site(db, monkeypatch):
    async def build():
        conn = await db.connect()
        try:
            await db.build(conn)
            await processes.ensure_process_schema(conn)
            await firm_holidays.ensure_firm_holidays_schema(conn)
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO teams (id, name) VALUES (10, 'Cível'), (11, 'Trabalhista');"
                    "INSERT INTO team_members (team_id, staff_id) VALUES (10, 4), (10, 5)")
            await conn.commit()
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
    monkeypatch.setattr(settings, "djen_enabled", True)
    for module in (staff_web, page_module):
        monkeypatch.setattr(module, "get_pool", get_pool)
    app = FastAPI()
    app.include_router(staff_web.router)
    app.include_router(page_module.router)
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
        yield type("Site", (), {"client": client, "run": staticmethod(run)})
    finally:
        client.close()
        asyncio.run(db.drop())


def _token(site, who: int) -> str:
    return site.run(lambda conn: web_access.open_session(conn, who, idle_seconds=3600))


def _get(site, who: int | None, path: str = HOME):
    headers = {}
    if who is not None:
        headers["cookie"] = f"{staff_web.COOKIE}={_token(site, who)}"
    return site.client.get(path, headers=headers)


def _post(site, who: int, path: str, data: dict[str, str] | None = None, *,
          csrf: str | None = None, site_header: str = "same-origin"):
    token = _token(site, who)
    form = dict(data or {})
    form["csrf"] = staff_web.csrf_for(token) if csrf is None else csrf
    return site.client.post(HOME.rstrip("/") + path, data=form,
                            headers={"sec-fetch-site": site_header,
                                     "cookie": f"{staff_web.COOKIE}={token}"})


def _rows(site, statement: str, params=()):
    async def run(conn):
        async with conn.cursor() as cur:
            await cur.execute(statement, params)
            return await cur.fetchall()
    return site.run(run)


def _make(site, who: int, *, team_id=None, client="Cliente X", note=None) -> int:
    async def run(conn):
        made = await processes.create(conn, await scope_for(conn, who),
                                      process_number=CNJ_DIGITS, client=client, note=note,
                                      team_id=team_id)
        assert made is not None and made > 0
        return made
    return site.run(run)


def _deadline(site, who: int, pid: int, due: date, title="Contestação") -> int:
    async def run(conn):
        made = await processes.add_deadline(conn, await scope_for(conn, who), pid,
                                            title=title, due_date=due, staff_id=who)
        assert made is not None
        return made
    return site.run(run)


# ── the tab ─────────────────────────────────────────────────────────────────


def test_tab_in_sidebar_and_plain_bar(site, monkeypatch):
    plain = _get(site, RUI).text
    assert f"href='{HOME}'" in plain and ">Processos<" in plain

    monkeypatch.setattr(settings, "web_app", "on")
    sidebar = _get(site, RUI).text
    assert re.search(rf"<a class=nav-item href='{HOME}' aria-current=page>.*?"
                     r"<span>Processos</span>", sidebar)


def test_without_a_session_the_page_is_refused(site):
    response = _get(site, None)
    assert response.status_code in (401, 303)


def test_page_refused_when_the_action_is_not_allowed(site, monkeypatch):
    real = page_module.authorization.decide
    monkeypatch.setattr(page_module.authorization, "decide",
                        lambda staff, action: real(None, action)
                        if action == "process.track" else real(staff, action))
    assert _get(site, RUI).status_code == 403


# ── what is listed ──────────────────────────────────────────────────────────


def test_page_lists_only_visible_processes(site):
    _make(site, RUI, client="Cliente Rui")
    assert "Cliente Rui" in _get(site, RUI).text
    assert CNJ in _get(site, RUI).text
    for other in (ANA, PAULO):
        assert "Cliente Rui" not in _get(site, other).text


def test_client_and_note_are_escaped(site):
    _make(site, RUI, client="<script>x</script>", note="'\"><b>n</b>")
    page = _get(site, RUI).text
    assert "<script>x</script>" not in page and "&lt;script&gt;x&lt;/script&gt;" in page
    assert "<b>n</b>" not in page


def test_unknown_ok_key_shows_nothing(site):
    page = _get(site, RUI, HOME + "?aviso=%3Cb%3Eoi%3C%2Fb%3E").text
    assert "<b>oi</b>" not in page and "&lt;b&gt;oi" not in page
    assert "class=note role=status" not in page


def test_done_deadlines_fold_under_concluidos(site):
    pid = _make(site, RUI)
    done = _deadline(site, RUI, pid, date(2026, 12, 1), title="Já feito")
    _deadline(site, RUI, pid, date(2026, 12, 2), title="Aberto")
    response = _post(site, RUI, f"/prazo/{done}/concluir")
    assert response.status_code == 303
    page = _get(site, RUI).text
    fold = page[page.index("<summary>Concluídos"):]
    assert "Já feito" in fold
    assert "Aberto" in page[:page.index("<summary>Concluídos")]


# ── changes ─────────────────────────────────────────────────────────────────


_STATE = ("SELECT p.process_number, p.visibility, p.archived_at IS NULL, d.title, "
          "d.done_at IS NULL FROM tracked_processes p LEFT JOIN process_deadlines d "
          "ON d.process_id = p.id ORDER BY p.id, d.id")


@pytest.mark.parametrize("bad", [{"csrf": "x"}, {"site_header": "cross-site"}])
def test_every_post_refused_without_csrf_and_from_another_site(site, bad):
    pid = _make(site, RUI, team_id=CIVEL)
    deadline = _deadline(site, RUI, pid, date(2026, 12, 1))
    before = _rows(site, _STATE)
    for path, data in (
            ("/novo", {"cnj": "0000001-11.2024.8.14.0001", "onde": "individual"}),
            (f"/{pid}/prazo", {"titulo": "X", "vence": "05/12/2026"}),
            (f"/{pid}/prazo", {"titulo": "X", "vence": "05/12/2026", "confirmar": "1"}),
            (f"/prazo/{deadline}/concluir", {}),
            (f"/{pid}/arquivar", {"confirmar": "1"}),
            (f"/{pid}/onde", {"onde": "individual"})):
        assert _post(site, RUI, path, data, **bad).status_code == 403, path
    assert _rows(site, _STATE) == before


def test_cnj_with_wrong_check_digit_refused(site):
    response = _post(site, RUI, "/novo", {"cnj": WRONG_DIGIT, "onde": "individual"})
    assert response.headers["location"] == f"{HOME}?aviso=cnj"
    assert _rows(site, "SELECT id FROM tracked_processes") == []


def test_new_process_follows_djen_when_checked_and_not_when_unchecked(site):
    response = _post(site, RUI, "/novo", {"cnj": CNJ, "cliente": "X", "onde": "individual",
                                          "djen": "1"})
    assert response.headers["location"] == f"{HOME}?aviso=criado"
    assert _rows(site, "SELECT process_number, created_by, visibility FROM tracked_processes") \
        == [(CNJ_DIGITS, RUI, "private")]
    assert _rows(site, "SELECT staff_id, process_number FROM watched_processes") \
        == [(RUI, CNJ_DIGITS)]
    _post(site, ANA, "/novo", {"cnj": CNJ, "onde": f"team:{CIVEL}"})
    assert _rows(site, "SELECT staff_id FROM watched_processes") == [(RUI,)]


def test_duplicate_is_said(site):
    _post(site, RUI, "/novo", {"cnj": CNJ, "onde": "individual"})
    response = _post(site, RUI, "/novo", {"cnj": CNJ, "onde": "individual"})
    assert response.headers["location"] == f"{HOME}?aviso=duplicado"


def test_new_process_in_a_team_one_is_not_in_refused(site):
    response = _post(site, RUI, "/novo", {"cnj": CNJ, "onde": f"team:{TRABALHISTA}"})
    assert response.headers["location"] == f"{HOME}?aviso=equipe"
    assert _rows(site, "SELECT id FROM tracked_processes") == []


def test_deadline_by_count_shows_preview_and_saves_only_on_confirm(site):
    site.run(lambda conn: firm_holidays.add_holiday(conn, date(2026, 10, 15), "Feriado local",
                                                    added_by=PAULO))
    pid = _make(site, RUI)
    preview = _post(site, RUI, f"/{pid}/prazo", {"titulo": "Contestação", "inicio": "06/10/2026",
                                                  "dias": "15", "regime": "uteis"})
    expected = calcular_prazo(date(2026, 10, 6), 15, regime=Regime.DIAS_UTEIS,
                              calendario=calendario_do_escritorio(
                                  [(date(2026, 10, 15), "Feriado local")])).vencimento
    assert preview.status_code == 200
    assert f"{expected:%d/%m/%Y}" in preview.text and "Salvar prazo" in preview.text
    assert "Feriado local" in preview.text
    assert _rows(site, "SELECT id FROM process_deadlines") == []

    saved = _post(site, RUI, f"/{pid}/prazo", _hidden_fields(preview.text))
    assert saved.status_code == 303
    assert _rows(site, "SELECT title, due_date FROM process_deadlines") \
        == [("Contestação", expected)]


def _hidden_fields(page: str) -> dict[str, str]:
    """The confirmation form the page offered, as the browser would send it."""
    import html as _html
    form = page[page.index("Salvar prazo") - 2000:page.index("Salvar prazo")]
    return {name: _html.unescape(value) for name, value in
            re.findall(r"<input type=hidden name=(\w+) value='([^']*)'>", form)
            if name != "csrf"}


def test_a_title_with_quotes_survives_the_confirmation(site):
    pid = _make(site, RUI)
    title = "Réplica d'\"Alfa\" <b>"
    preview = _post(site, RUI, f"/{pid}/prazo", {"titulo": title, "inicio": "06/10/2026",
                                                  "dias": "5", "regime": "corridos"})
    assert "&lt;b&gt;" in preview.text and "Alfa&quot; &lt;b&gt;" in preview.text
    _post(site, RUI, f"/{pid}/prazo", _hidden_fields(preview.text))
    assert _rows(site, "SELECT title FROM process_deadlines") == [(title,)]


def test_a_count_past_the_save_window_offers_no_save(site):
    pid = _make(site, RUI)
    today = date.today()
    start = date(today.year + 10, 1, 5)
    preview = _post(site, RUI, f"/{pid}/prazo", {"titulo": "Longe", "inicio": f"{start:%d/%m/%Y}",
                                                  "dias": "365", "regime": "uteis"})
    assert preview.status_code == 422
    assert "Salvar prazo" not in preview.text


def test_deadline_by_date_saves_directly(site):
    pid = _make(site, RUI)
    response = _post(site, RUI, f"/{pid}/prazo", {"titulo": "Audiência", "vence": "2026-12-05"})
    assert response.status_code == 303
    assert _rows(site, "SELECT due_date FROM process_deadlines") == [(date(2026, 12, 5),)]


def test_deadline_on_invisible_process_is_not_found(site):
    pid = _make(site, RUI)
    response = _post(site, ANA, f"/{pid}/prazo", {"titulo": "X", "vence": "05/12/2026"})
    assert response.headers["location"] == f"{HOME}?aviso=nao_encontrado"
    assert _post(site, PAULO, f"/{pid}/arquivar", {"confirmar": "1"}).headers["location"] \
        == f"{HOME}?aviso=nao_encontrado"
    assert _rows(site, "SELECT id FROM process_deadlines") == []
    assert _rows(site, "SELECT archived_at FROM tracked_processes") == [(None,)]


def test_archive_asks_first(site):
    pid = _make(site, RUI)
    asked = _post(site, RUI, f"/{pid}/arquivar")
    assert asked.status_code == 200 and "Arquivar" in asked.text
    assert _rows(site, "SELECT archived_at FROM tracked_processes") == [(None,)]
    assert _post(site, RUI, f"/{pid}/arquivar", {"confirmar": "1"}).status_code == 303
    assert _rows(site, "SELECT archived_at IS NOT NULL FROM tracked_processes") == [(True,)]


def test_only_creator_sees_and_uses_onde(site):
    pid = _make(site, RUI, team_id=CIVEL)
    assert f"action='{HOME}{pid}/onde'" in _get(site, RUI).text
    assert f"action='{HOME}{pid}/onde'" not in _get(site, ANA).text
    refused = _post(site, ANA, f"/{pid}/onde", {"onde": "individual"})
    assert refused.headers["location"] == f"{HOME}?aviso=nao_encontrado"
    assert _post(site, RUI, f"/{pid}/onde", {"onde": "individual"}).headers["location"] \
        == f"{HOME}?aviso=movido"
    assert "Cliente X" not in _get(site, ANA).text
