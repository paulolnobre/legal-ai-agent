"""The assistant's page, against a real Postgres.

The page is the person's own conversation and nobody else's, every POST is
this site's and carries its token, what the bot wrote is escaped before it
is linked, and a file goes only to its owner before it expires.
"""
import asyncio
import contextlib
import html
import io
import json
import re
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import document_crypto
from core.config import settings
from core.models import StaffMember
from core.repositories import conversation, panel, publications, web_access
from core.services import authorization, budget, ingestion, media, panel_intake, work_hours
from core.web import actions, assistant, staff_web

PAULO, RUI, ANA = 1, 4, 5
CHATS = {PAULO: "100", RUI: "400", ANA: "500"}


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

    queued: list[tuple[dict, bool]] = []

    async def enqueue(update, *, deferred):
        queued.append((update, deferred))
        return True

    monkeypatch.setattr(settings, "web_enabled", True)
    monkeypatch.setattr(settings, "web_assistant", "on")
    monkeypatch.setattr(settings, "web_assistant_staff", "")
    monkeypatch.setattr(settings, "web_explorer", "on")
    monkeypatch.setattr(staff_web, "get_pool", get_pool)
    monkeypatch.setattr(assistant, "get_pool", get_pool)
    monkeypatch.setattr(panel_intake.update_queue, "enqueue_update", enqueue)
    monkeypatch.setattr(work_hours, "is_office_hours", lambda: True)

    app = FastAPI()
    app.include_router(staff_web.router)
    app.include_router(assistant.router)
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
        yield SimpleNamespace(client=client, run=run, queued=queued, connect=db.connect,
                              app=app)
    finally:
        client.close()
        asyncio.run(db.drop())


def _token(site, who: int) -> str:
    return site.run(lambda conn: web_access.open_session(conn, who, idle_seconds=3600))


def _get(site, who: int, path: str = "/web/assistente/"):
    return site.client.get(path, headers={"cookie": f"{staff_web.COOKIE}={_token(site, who)}"})


def _post(site, who: int | None, path: str, data: dict[str, str], *,
          site_header: str = "same-origin", csrf: str | None = None):
    headers = {"sec-fetch-site": site_header}
    form = dict(data)
    if who is not None:
        token = _token(site, who)
        headers["cookie"] = f"{staff_web.COOKIE}={token}"
        form["csrf"] = staff_web.csrf_for(token) if csrf is None else csrf
    return site.client.post(path, data=form, headers=headers)


def _turn(site, who: int, speaker: conversation.Speaker, text: str, *,
          channel: str = "panel",
          extras: dict | None = None) -> int:
    return site.run(lambda conn: conversation.record_turn(
        conn, staff_id=who, chat_id=CHATS[who], speaker=speaker, text=text,
        channel=channel, extras=extras))


def _rows(site, statement: str, params=()):
    async def query(conn):
        async with conn.cursor() as cur:
            await cur.execute(statement, params)
            rows = await cur.fetchall()
        await conn.commit()
        return rows
    return site.run(query)


def _refreshes(response) -> bool:
    """Whether the page, without its script, looks again by itself. Only
    without it: with the script, the script is the one that looks."""
    refresh = (f"<noscript><meta http-equiv=refresh content={assistant.REFRESH_SECONDS}>"
               f"</noscript>")
    head = response.text.split("</head>", 1)[0]
    return refresh in head and "refresh" not in {k.lower() for k in response.headers}


def _texts(page: str) -> list[str]:
    return [html.unescape(re.sub(r"<[^>]+>", "", t))
            for t in re.findall(r"<p class=text>(.*?)</p>", page, re.S)]


# ── the switch ──────────────────────────────────────────────────────────────


def test_switched_off_the_assistant_does_not_exist_and_takes_no_connection(site, monkeypatch):
    async def no_pool():
        raise AssertionError("a switched-off page took a connection")

    monkeypatch.setattr(settings, "web_assistant", "off")
    monkeypatch.setattr(assistant, "get_pool", no_pool)

    for path in ("/web/assistente/", "/web/assistente/arquivo/abc"):
        response = site.client.get(path)
        assert response.status_code == 404 and response.json() == {"detail": "Not Found"}
    for path in ("/web/assistente/mensagem", "/web/assistente/botao",
                 "/web/assistente/retirar", "/web/assistente/anexo"):
        assert _post(site, RUI, path, {"texto": "oi"}).status_code == 404
    monkeypatch.setattr(settings, "web_assistant", "on")
    monkeypatch.setattr(settings, "web_enabled", False)
    assert site.client.get("/web/assistente/").status_code == 404


def test_pilot_and_whole_id_matching(site, monkeypatch):
    monkeypatch.setattr(settings, "web_assistant", "pilot")
    monkeypatch.setattr(settings, "web_assistant_staff", "14, 1")

    assert _get(site, PAULO).status_code == 200
    assert _get(site, RUI).status_code == 404, "4 is not 14"


def test_the_nav_shows_assistente_to_whoever_has_it(site, monkeypatch):
    monkeypatch.setattr(settings, "web_assistant", "pilot")
    monkeypatch.setattr(settings, "web_assistant_staff", "1")

    assert "href='/web/assistente/#escrever'>Assistente</a>" in _get(site, PAULO).text
    assert "/web/assistente/" not in _get(site, RUI, "/web/").text


# ── the conversation ────────────────────────────────────────────────────────


def test_the_page_shows_only_this_persons_conversation(site):
    _turn(site, RUI, "person", "pergunta do Rui")
    _turn(site, ANA, "person", "pergunta da Ana")
    _turn(site, RUI, "bot", "resposta ao Rui")

    page = _get(site, RUI).text

    assert _texts(page) == ["pergunta do Rui", "resposta ao Rui"]
    assert "Ana" not in page


def test_telegram_turns_are_labelled(site):
    _turn(site, RUI, "person", "pelo bot", channel="telegram")
    _turn(site, RUI, "person", "pelo painel")

    page = _get(site, RUI).text
    turns = re.findall(r"<li class='turn [^']*'.*?</li>", page, re.S)

    assert "pelo Telegram" in turns[0] and "pelo Telegram" not in turns[1]


def test_sources_become_explorer_links_and_text_is_escaped(site, monkeypatch):
    _turn(site, RUI, "bot", "<script>alert(1)</script> veja [12] Contrato <b>Acme</b>")

    page = _get(site, RUI).text

    assert "<script>alert" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<a href='/web/explorar/documento/12'>[12]</a> Contrato &lt;b&gt;Acme" in page
    monkeypatch.setattr(settings, "web_explorer", "off")
    assert "/web/explorar/documento/12" not in _get(site, RUI).text, \
        "without the explorer, a source is not a link to a page that does not exist"


def test_a_person_turn_is_escaped_and_never_linked(site):
    _turn(site, RUI, "person", "abra o [12] <img src=x onerror=alert(1)>")

    page = _get(site, RUI).text

    assert "/web/explorar/documento/12" not in page
    assert "<img src=x" not in page and "&lt;img src=x onerror=alert(1)&gt;" in page


def test_generated_docx_and_xlsx_get_an_edit_link_and_nothing_else_does(site):
    for name, href in (("r.docx", "/web/assistente/arquivo/tokd"),
                       ("r.XLSX", "/web/assistente/arquivo/toke"),
                       ("r.pdf", "/web/assistente/arquivo/tokp"),
                       ("a.docx", "/web/arquivos/7/original"),
                       ("x.docx", "/web/assistente/arquivo/tok/extra")):
        _turn(site, RUI, "bot", name, extras={"file": {"href": href, "name": name, "hours": 24}})

    page = _get(site, RUI).text

    assert "href='/web/editor/tokd'>Editar</a>" in page
    assert "href='/web/editor/toke'>Editar</a>" in page
    assert page.count("/web/editor/") == 2, "no edit link for a pdf, an original or an odd shape"


def test_files_are_linked_only_to_the_two_download_addresses(site):
    _turn(site, RUI, "bot", "original", extras={"file": {
        "href": "/web/arquivos/7/original", "name": "a<b>.pdf"}})
    _turn(site, RUI, "bot", "export", extras={"file": {
        "href": "/web/assistente/arquivo/tok", "name": "r.pdf", "hours": 24}})
    _turn(site, RUI, "bot", "elsewhere", extras={"file": {
        "href": "https://evil.example/x", "name": "x.pdf"}})
    _turn(site, RUI, "bot", "quoted", extras={"file": {
        "href": "/web/arquivos/7/original' onclick='x", "name": "y.pdf"}})

    page = _get(site, RUI).text

    assert "href='/web/arquivos/7/original'>Baixar a&lt;b&gt;.pdf</a>" in page
    assert "href='/web/assistente/arquivo/tok'>Baixar r.pdf</a>" in page
    assert "disponível por 24 h" not in page, "the channel's own text says it, once"
    assert "evil.example" not in page and "onclick" not in page


def test_buttons_are_forms_only_on_panel_replies(site):
    extras = panel.buttons_extras([[("Apagar", "tok-1"), ("<Cancelar>", "tok-2")]])
    panel_turn = _turn(site, RUI, "bot", "Apagar?", extras=extras)
    _turn(site, RUI, "bot", "Apagar?", channel="telegram", extras=extras)

    page = _get(site, RUI).text

    assert page.count(f"name=turno value='{panel_turn}'") == 2
    assert page.count("action='/web/assistente/botao'") == 2
    assert "&lt;Cancelar&gt;" in page


def test_older_messages_page_back(site, monkeypatch):
    monkeypatch.setattr(assistant, "PER_PAGE", 3)
    ids = [_turn(site, RUI, "person", f"m{n}") for n in range(5)]

    first = _get(site, RUI).text
    older = re.search(r"href='/web/assistente/\?antes=(\d+)'>Mensagens anteriores", first)

    assert _texts(first) == ["m2", "m3", "m4"]
    assert older and int(older.group(1)) == ids[2]
    back = _get(site, RUI, f"/web/assistente/?antes={older.group(1)}").text
    assert _texts(back) == ["m0", "m1"]
    assert "Mensagens anteriores" not in back
    assert "class=composer" not in back, "the composer is on the newest page only"


# ── sending ─────────────────────────────────────────────────────────────────


def test_sending_queues_and_redirects(site):
    response = _post(site, RUI, "/web/assistente/mensagem", {"texto": "Qual o prazo?"})

    assert response.status_code == 303
    assert response.headers["location"] == "/web/assistente/#escrever"
    (update, deferred), = site.queued
    assert update["message"]["text"] == "Qual o prazo?" and deferred is False
    assert update["message"]["chat"]["id"] == 400, "the session's person, not the form's"
    assert _texts(_get(site, RUI).text) == ["Qual o prazo?"]


def test_the_form_cannot_name_who_is_speaking(site):
    _post(site, RUI, "/web/assistente/mensagem",
          {"texto": "oi", "chat_id": "500", "staff_id": "5"})

    assert site.queued[0][0]["message"]["chat"]["id"] == 400
    assert _rows(site, "SELECT staff_id FROM panel_requests") == [(RUI,)]


@pytest.mark.parametrize("path, data", [
    ("/web/assistente/mensagem", {"texto": "oi"}),
    ("/web/assistente/botao", {"turno": "1", "token": "t"}),
    ("/web/assistente/retirar", {"pedido": "00000000-0000-0000-0000-000000000000"}),
    ("/web/assistente/acao", {"acao": "feriados"}),
])
@pytest.mark.parametrize("how", ["missing", "non-ascii", "cross-site", "no session"])
def test_every_post_needs_csrf_and_this_site(site, path, data, how):
    if how == "missing":
        response = _post(site, RUI, path, data, csrf="")
    elif how == "non-ascii":
        response = _post(site, RUI, path, data, csrf="é")
    elif how == "cross-site":
        response = _post(site, RUI, path, data, site_header="cross-site")
    else:
        response = _post(site, None, path, data)

    assert response.status_code == 403
    assert _rows(site, "SELECT count(*) FROM conversation_turns") == [(0,)]
    assert _rows(site, "SELECT count(*) FROM panel_requests") == [(0,)]
    assert site.queued == []


def test_an_oversized_body_is_refused_without_effect(site, monkeypatch):
    monkeypatch.setattr(settings, "max_question_chars", 10)

    response = _post(site, RUI, "/web/assistente/mensagem", {"texto": "x" * 2000})

    assert response.status_code == 403
    assert site.queued == []
    assert _rows(site, "SELECT count(*) FROM panel_requests") == [(0,)]


@pytest.mark.parametrize("text, note", [
    ("/mfa refazer ABCD-EFGH", "telegram"),
    ("/pje 12345678901 ABCD-senha", "pje_words"),
    ("   ", "empty"),
])
def test_refused_messages_show_fixed_notes_without_echoing_input(site, text, note):
    response = _post(site, RUI, "/web/assistente/mensagem", {"texto": text})

    assert response.headers["location"] == f"/web/assistente/?aviso={note}#escrever"
    page = _get(site, RUI, response.headers["location"]).text
    assert assistant.NOTES[note] in page
    assert "ABCD" not in page and "/mfa" not in page


def test_busy_shows_the_note_and_disables_the_composer(site):
    _post(site, RUI, "/web/assistente/mensagem", {"texto": "primeira"})

    response = _post(site, RUI, "/web/assistente/mensagem", {"texto": "segunda <i>"})
    page = _get(site, RUI, response.headers["location"]).text

    assert response.headers["location"] == "/web/assistente/?aviso=busy#escrever"
    assert "Aguarde a resposta anterior." in page
    assert "<textarea id=texto name=texto " in page and " required disabled>" in page
    assert re.search(r"<button type=submit class=send aria-label=Enviar disabled>", page)
    assert "segunda" not in page and len(site.queued) == 1


def test_an_unknown_note_shows_nothing(site):
    page = _get(site, RUI, "/web/assistente/?aviso=<script>").text

    assert "<p class=note role=alert id=aviso hidden></p>" in page, "the note stays empty"
    assert "<script>" not in page


# ── waiting ─────────────────────────────────────────────────────────────────


def test_the_wait_refreshes_itself_only_while_queued_or_running(site, monkeypatch):
    assert not _refreshes(_get(site, RUI))

    _post(site, RUI, "/web/assistente/mensagem", {"texto": "pergunta"})
    queued = _get(site, RUI)
    assert _refreshes(queued)
    assert "Respondendo…" in queued.text

    request_id = site.queued[0][0]["panel_request"]
    site.run(lambda conn: panel.start(conn, request_id))
    assert _refreshes(_get(site, RUI))

    site.run(lambda conn: panel.finish(conn, request_id, "done"))
    done = _get(site, RUI)
    assert not _refreshes(done)
    assert " disabled" not in done.text

    monkeypatch.setattr(work_hours, "is_office_hours", lambda: False)
    _post(site, RUI, "/web/assistente/mensagem", {"texto": "à noite"})
    deferred = _get(site, RUI)
    assert not _refreshes(deferred), \
        "a request waiting for the morning does not refresh all night"
    assert "a partir das 08:00" in deferred.text


def test_a_failed_request_says_so(site):
    _post(site, RUI, "/web/assistente/mensagem", {"texto": "pergunta"})
    request_id = site.queued[0][0]["panel_request"]
    site.run(lambda conn: panel.finish(conn, request_id, "failed"))

    page = _get(site, RUI).text

    assert "Não consegui responder. Tente de novo." in page
    assert " disabled" not in page


def test_a_deferred_request_can_be_withdrawn(site, monkeypatch):
    monkeypatch.setattr(work_hours, "is_office_hours", lambda: False)
    _post(site, RUI, "/web/assistente/mensagem", {"texto": "para amanhã"})
    page = _get(site, RUI).text
    pedido = re.search(r"name=pedido value='([0-9a-f-]+)'", page)
    assert pedido

    by_ana = _post(site, ANA, "/web/assistente/retirar", {"pedido": pedido.group(1)})
    assert by_ana.headers["location"] == "/web/assistente/?aviso=not_withdrawn#escrever"

    response = _post(site, RUI, "/web/assistente/retirar", {"pedido": pedido.group(1)})
    assert response.headers["location"] == "/web/assistente/?aviso=withdrawn#escrever"
    assert _rows(site, "SELECT status FROM panel_requests") == [("withdrawn",)]
    assert " disabled" not in _get(site, RUI).text


def test_a_press_queues_the_button(site):
    extras = panel.buttons_extras([[("Apagar", "tok-1")]])
    turn_id = _turn(site, RUI, "bot", "Apagar?", extras=extras)

    response = _post(site, RUI, "/web/assistente/botao", {"turno": str(turn_id),
                                                           "token": "tok-1"})
    elsewhere = _post(site, ANA, "/web/assistente/botao", {"turno": str(turn_id),
                                                            "token": "tok-1"})

    assert response.headers["location"] == "/web/assistente/#escrever"
    assert site.queued[0][0]["callback_query"]["data"] == "tok-1"
    assert elsewhere.headers["location"] == "/web/assistente/?aviso=no_button#escrever"
    assert len(site.queued) == 1


# ── files ───────────────────────────────────────────────────────────────────


@pytest.fixture
def master_key(monkeypatch):
    monkeypatch.setattr(settings, "doc_master_key", document_crypto.new_master_key())


def _store(site, who: int, data: bytes = b"%PDF-1.7 resposta") -> str:
    token = site.run(lambda conn: panel.store_file(
        conn, staff_id=who, filename="Resposta à consulta.pdf",
        media_type="application/pdf", data=data))
    assert token
    return token


def test_a_file_downloads_owned_and_unexpired_as_an_attachment_and_is_logged(
        site, master_key):
    token = _store(site, RUI)

    response = _get(site, RUI, f"/web/assistente/arquivo/{token}")

    assert response.status_code == 200
    assert response.content == b"%PDF-1.7 resposta"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert "Resposta%20%C3%A0%20consulta.pdf" in response.headers["content-disposition"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert _rows(site, "SELECT staff_id, action FROM document_access_log") == [
        (RUI, "download_export")]

    _rows(site, "UPDATE panel_files SET expires_at = NOW() - interval '1 second' "
                "RETURNING 1")
    assert _get(site, RUI, f"/web/assistente/arquivo/{token}").status_code == 404


def test_someone_elses_file_token_is_not_found(site, master_key):
    token = _store(site, RUI)

    response = _get(site, ANA, f"/web/assistente/arquivo/{token}")

    assert response.status_code == 404
    assert _rows(site, "SELECT count(*) FROM document_access_log") == [(0,)]


def test_a_file_needs_a_session(site, master_key):
    token = _store(site, RUI)

    response = site.client.get(f"/web/assistente/arquivo/{token}")

    assert response.status_code != 200 and b"%PDF" not in response.content


def test_a_reply_that_lands_while_the_page_is_read_is_never_lost(site, monkeypatch):
    """The worker may record its reply and finish between the page's two
    reads. The request's state is read first, so the page either shows the
    reply or keeps refreshing until it does."""
    _post(site, RUI, "/web/assistente/mensagem", {"texto": "pergunta"})
    request_id = site.queued[0][0]["panel_request"]
    site.run(lambda conn: panel.start(conn, request_id))
    page_of = conversation.page_of
    landed: list[bool] = []

    async def worker_finishes_meanwhile(conn, staff_id, **kwargs):
        read = await page_of(conn, staff_id, **kwargs)
        if not landed:
            landed.append(True)
            other = await site.connect()
            try:
                await conversation.record_turn(other, staff_id=RUI, chat_id="400",
                                               speaker="bot", text="a resposta",
                                               channel="panel")
                await panel.finish(other, request_id, "done")
            finally:
                await other.close()
        return read

    monkeypatch.setattr(assistant.conversation, "page_of", worker_finishes_meanwhile)

    response = _get(site, RUI)

    shown = "a resposta" in _texts(response.text)
    assert shown or _refreshes(response), "the reply is shown or still awaited"


@pytest.mark.parametrize("path", ["/web/assistente/?antes=²", "/web/assistente/?antes=١٢"])
def test_a_non_ascii_number_in_the_address_is_ignored(site, path):
    assert _get(site, RUI, path).status_code == 200


def test_a_non_ascii_turn_number_is_refused_as_no_button(site):
    response = _post(site, RUI, "/web/assistente/botao", {"turno": "²", "token": "t"})

    assert response.status_code == 303
    assert response.headers["location"] == "/web/assistente/?aviso=no_button#escrever"


# ── the script ──────────────────────────────────────────────────────────────


def test_only_the_assistant_page_allows_its_script(site):
    page = _get(site, RUI)
    policy = page.headers["content-security-policy"]

    assert "default-src 'none'" in policy and "frame-ancestors 'none'" in policy
    assert "script-src 'self'" in policy and "connect-src 'self'" in policy
    assert "unsafe" not in policy.replace("style-src 'unsafe-inline'", "")
    assert f"<script type=module src='{assistant.SCRIPT}'></script>" in page.text
    assert re.findall(r"<script(?![^>]*\bsrc=)", page.text) == [], "nothing inline"

    from core.web import pages
    before = dict(pages.HEADERS)
    other = pages.page("Outra", "<h1>Outra</h1>")
    assert pages.HEADERS == before, "the shared policy was changed in place"
    assert "script-src" not in other.headers["content-security-policy"]
    older = _get(site, RUI, "/web/assistente/?antes=1")
    assert "<script" not in older.text, "an older page sends nothing"


def test_the_script_is_served_as_javascript_and_not_when_off(site, monkeypatch):
    response = site.client.get(assistant.SCRIPT)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["x-content-type-options"] == "nosniff"
    monkeypatch.setattr(settings, "web_assistant", "off")
    assert site.client.get(assistant.SCRIPT).status_code == 404


def test_the_script_writes_no_markup_of_its_own():
    from pathlib import Path

    source = Path("core/web/static/assistant.js").read_text(encoding="utf-8")

    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                      "eval(", "new Function", "setTimeout(\"", "http://", "https://"):
        assert forbidden not in source, forbidden


def _fragment(site, who: int, desde: str = ""):
    from urllib.parse import quote

    response = _get(site, who, f"/web/assistente/conversa?desde={quote(desde)}")
    assert response.status_code == 200
    novas = re.search(r"<div id=novas data-cursor='([^']+)'>", response.text)
    assert novas
    ids = [int(n) for n in re.findall(r"<li class='turn [a-z]+' id=t(\d+)>", response.text)]
    estado = re.search(r"<div id=estado data-estado=([a-z]+)>", response.text)
    assert estado
    return ids, novas.group(1), estado.group(1), response


def _age_turn(site, turn_id: int, minutes: int) -> None:
    _rows(site, "UPDATE conversation_turns SET updated_at = clock_timestamp() - "
                "make_interval(mins => %s) WHERE id = %s RETURNING 1", (minutes, turn_id))


def test_conversa_returns_only_newer_or_updated_turns_of_this_person(site):
    old = _turn(site, RUI, "person", "antiga")
    _age_turn(site, old, 30)
    _turn(site, ANA, "person", "da Ana")
    _, cursor, _, _ = _fragment(site, RUI)

    fresh = _turn(site, RUI, "bot", "nova")
    ids, _, _, response = _fragment(site, RUI, cursor)

    assert ids == [fresh]
    assert "da Ana" not in response.text
    _rows(site, "UPDATE conversation_turns SET text = 'editada', updated_at = "
                "clock_timestamp() WHERE id = %s RETURNING 1", (old,))
    assert old in _fragment(site, RUI, cursor)[0], "an edited turn comes back to be replaced"


def test_conversa_does_not_lose_a_turn_with_an_equal_timestamp_or_a_late_commit(site):
    first = _turn(site, RUI, "person", "um")
    second = _turn(site, RUI, "bot", "dois")
    _rows(site, "UPDATE conversation_turns SET updated_at = (SELECT updated_at FROM "
                "conversation_turns WHERE id = %s) WHERE id = %s RETURNING 1",
          (first, second))
    ids, cursor, _, _ = _fragment(site, RUI)
    assert {first, second} <= set(ids)

    late = _turn(site, RUI, "bot", "commitado depois do cursor")
    _age_turn(site, late, 4)  # stamped before the cursor, seen only now
    assert late in _fragment(site, RUI, cursor)[0]


def test_conversa_always_returns_the_whole_latest_request(site):
    _post(site, RUI, "/web/assistente/mensagem", {"texto": "pergunta antiga"})
    request_id = site.queued[0][0]["panel_request"]
    person = _rows(site, "SELECT id FROM conversation_turns")[0][0]
    _age_turn(site, person, 60)
    _, cursor, _, _ = _fragment(site, RUI)

    ids, _, estado, _ = _fragment(site, RUI, cursor)

    assert person in ids and estado == "queued"
    site.run(lambda conn: panel.start(conn, request_id))
    assert _fragment(site, RUI, cursor)[2] == "running"


def test_conversa_reports_the_request_state(site, monkeypatch):
    assert _fragment(site, RUI)[2] == "nenhum"
    monkeypatch.setattr(work_hours, "is_office_hours", lambda: False)
    _post(site, RUI, "/web/assistente/mensagem", {"texto": "à noite"})

    _, _, estado, response = _fragment(site, RUI)

    assert estado == "deferred" and "Retirar" in response.text


@pytest.mark.parametrize("desde", ["", "lixo", "2026-10-21T10:00:00", "²"])
def test_conversa_takes_any_cursor_without_failing(site, desde):
    _turn(site, RUI, "person", "oi")

    ids, _, _, _ = _fragment(site, RUI, desde)

    assert ids, "a cursor it cannot read falls back to the recent window"


def test_conversa_answers_401_when_signed_out(site):
    """A marker the script reads, not the signed-out page."""
    response = site.client.get("/web/assistente/conversa")

    assert response.status_code == 401
    assert response.text == "ended"
    assert response.headers["content-type"].startswith("text/plain")


def test_the_poll_does_not_keep_a_phone_unlocked(site, monkeypatch):
    """A page left open while waiting polls every second and a half. If that
    were use, the phone would never lock; it is still refused once locked."""
    monkeypatch.setattr(settings, "web_app", "pilot")
    monkeypatch.setattr(settings, "web_app_staff", str(RUI))

    async def opened(conn):
        pid = await web_access.add_passkey(conn, RUI, credential_id=b"rui-phone",
                                           public_key=b"key", sign_count=0,
                                           user_handle=bytes(32))
        assert pid is not None
        return await web_access.open_session(
            conn, RUI, idle_seconds=3600, method="passkey", passkey_id=pid, rule="app",
            absolute_seconds=settings.web_app_session_seconds)

    def last_seen(expression: str) -> None:
        _rows(site, f"UPDATE web_sessions SET last_seen_at = {expression} RETURNING 1")

    site.client.cookies.set(staff_web.COOKIE, site.run(opened))
    last_seen("NOW() - interval '14 minutes'")
    polled = site.client.get("/web/assistente/conversa")
    last_seen("last_seen_at - interval '2 minutes'")
    locked = site.client.get("/web/assistente/conversa")
    page = site.client.get("/web/assistente/", headers={"Sec-Fetch-Mode": "navigate"})

    assert polled.status_code == 200
    assert locked.status_code == 401 and locked.text == "locked"
    assert page.status_code == 303
    assert page.headers["location"] == "/web/faceid/?volta=%2Fweb%2Fassistente%2F"


def test_a_form_that_meets_the_lock_is_not_sent_and_returns_home(site, monkeypatch):
    """Back after Face ID is a GET, which the form's address does not answer."""
    monkeypatch.setattr(settings, "web_app", "on")

    async def locked(conn):
        pid = await web_access.add_passkey(conn, RUI, credential_id=b"rui-phone",
                                           public_key=b"key", sign_count=0,
                                           user_handle=bytes(32))
        assert pid is not None
        token = await web_access.open_session(
            conn, RUI, idle_seconds=3600, method="passkey", passkey_id=pid, rule="app",
            absolute_seconds=settings.web_app_session_seconds)
        async with conn.cursor() as cur:
            await cur.execute("UPDATE web_sessions SET last_seen_at = NOW() - interval '1 hour'")
        await conn.commit()
        return token

    token = site.run(locked)
    site.client.cookies.set(staff_web.COOKIE, token)
    response = site.client.post(
        "/web/assistente/mensagem",
        data={"texto": "oi", "csrf": staff_web.csrf_for(token)},
        headers={"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "same-origin"})

    assert response.status_code == 303
    assert response.headers["location"] == "/web/faceid/?volta=%2Fweb%2F"
    assert site.queued == [], "a locked session sent a message"


def test_the_script_is_answered_with_the_note_alone(site):
    sent = _post(site, RUI, "/web/assistente/mensagem", {"texto": "oi", "via": "script"})
    busy = _post(site, RUI, "/web/assistente/mensagem", {"texto": "de novo", "via": "script"})

    assert sent.status_code == 200 and sent.text == ""
    assert busy.status_code == 409 and busy.text == assistant.NOTES["busy"]
    assert busy.headers["content-type"].startswith("text/plain")
    assert busy.headers["x-content-type-options"] == "nosniff"
    refused = _post(site, RUI, "/web/assistente/mensagem", {"texto": "oi", "via": "script"},
                    csrf="")
    assert refused.status_code == 403, "the token is checked for the script as for the form"


def test_the_page_opens_at_the_composer_with_its_note_beside_it(site):
    """A long conversation opens at its end, and a note is said where the
    person is looking: inside the form they just sent from."""
    response = _post(site, RUI, "/web/assistente/mensagem", {"texto": "   "})
    page = _get(site, RUI, response.headers["location"]).text
    form = re.search(r"<form class=composer id=escrever .*?</form>", page, re.S)

    assert response.headers["location"].endswith("#escrever")
    assert form and assistant.NOTES["empty"] in form.group(0)
    assert page.count("id=aviso") == 1


def _found(pattern: str, text: str, flags: int = 0) -> re.Match[str]:
    """The match, or a failed assertion that says what was not on the page."""
    match = re.search(pattern, text, flags)
    assert match, f"not on the page: {pattern}"
    return match


def test_the_composer_is_a_chat_box_with_a_slot_for_the_tools_to_come(site):
    page = _get(site, RUI).text
    form = _found(r"<form class=composer id=escrever .*?</form>", page, re.S).group(0)
    area = _found(r"<textarea [^>]*>", form).group(0)
    button = _found(r"<button type=submit [^>]*>", form).group(0)

    assert "placeholder='Escreva sua mensagem…'" in area
    assert "aria-label='Sua mensagem'" in area and "rows=1" in area
    assert "<label for=texto" not in form, "the textarea is named by aria-label"
    assert "type=submit" in button and "aria-label=Enviar" in button
    assert "disabled" not in button, "without the script the button is on and the form posts"
    assert "<svg" in form and "<div class=composer-tools>" in form
    # One box holds the text, the empty slot on the left and the button on the right.
    box = _found(r"<div class=box>(.*)</div></form>", form, re.S).group(1)
    assert box.index("<textarea") < box.index("composer-tools") < box.index("<button type=submit")


def test_the_form_without_the_script_still_posts(site):
    """The form as the page draws it, posted as a browser without the script
    would: its own method, address and token, nothing added."""
    cookie = {"cookie": f"{staff_web.COOKIE}={_token(site, RUI)}"}
    page = site.client.get("/web/assistente/", headers=cookie).text
    form = _found(r"<form class=composer id=escrever ([^>]*)>(.*?)</form>", page, re.S)
    token = _found(r"name=csrf value='([^']+)'", form.group(2)).group(1)
    assert "method=post" in form.group(1) and "action='/web/assistente/mensagem'" in form.group(1)

    sent = site.client.post("/web/assistente/mensagem",
                            data={"texto": "sem script", "csrf": token},
                            headers={**cookie, "sec-fetch-site": "same-origin"})

    assert sent.status_code == 303 and sent.headers["location"] == "/web/assistente/#escrever"
    assert len(site.queued) == 1


def test_the_composer_is_styled_as_one_box_of_16px_corners():
    style = assistant.STYLE

    assert ".composer .box{" in style and "border-radius:16px" in style
    assert "border:1px solid var(--line-strong)" in style and "background:var(--panel)" in style
    assert ".composer .box:focus-within{border-color:var(--accent)}" in style
    assert "max-height:12.4rem" in style and "resize:none" in style
    assert ".composer .send{" in style and "border-radius:50%" in style


def test_the_cursor_is_honoured_beyond_the_late_commit_window(site):
    """A tab that slept: its cursor is older than the window, and what changed
    after it comes back while what did not stays out."""
    unchanged = _turn(site, RUI, "person", "antes do cursor")
    _age_turn(site, unchanged, 60)
    changed = _turn(site, RUI, "bot", "depois do cursor")
    _age_turn(site, changed, 20)
    old_cursor = _rows(site, "SELECT (clock_timestamp() - interval '30 minutes')::text")[0][0]
    from datetime import datetime

    cursor = datetime.fromisoformat(old_cursor.replace(" ", "T")).isoformat()
    ids, next_cursor, _, _ = _fragment(site, RUI, cursor)

    assert ids == [changed]
    assert next_cursor > cursor


def test_too_many_changes_ask_the_page_to_reload_rather_than_lose_any(site, monkeypatch):
    from core.repositories import conversation as repository

    monkeypatch.setattr(repository, "_CHANGED_LIMIT", 3)
    for n in range(4):
        _turn(site, RUI, "person", f"m{n}")

    response = _get(site, RUI, "/web/assistente/conversa")

    assert "data-recarregar=1" in response.text
    assert "<li class='turn" not in response.text


@pytest.mark.parametrize('mode,pilot,who,expected', [
    ('off', '1', PAULO, False), ('pilot', '1', PAULO, True),
    ('pilot', '1', RUI, False), ('on', '', RUI, True),
])
def test_app_lock_meta_only_for_the_pilot(site, monkeypatch, mode, pilot, who, expected):
    monkeypatch.setattr(settings, 'web_app', mode)
    monkeypatch.setattr(settings, 'web_app_staff', pilot)
    response = _get(site, who)
    assert response.status_code == 200
    assert ('<meta name=app-lock content=1>' in response.text) is expected
    assert ("href='/web/aparelhos/'" in response.text) is expected


def test_poll_answers_ended_as_text_when_signed_out(site):
    response = site.client.get('/web/assistente/conversa')
    assert response.status_code == 401 and response.text == 'ended'
    assert 'set-cookie' not in response.headers


# ── an attachment, before its message ──────────────────────────────────────


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 60
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 40
_ANEXO = "/web/assistente/anexo"


def _forbidden(reads: list):
    """A body that fails the test if the endpoint reads a byte of it."""
    async def receive():
        reads.append("read")
        raise AssertionError("the body was read before every check passed")
    return receive


def _chunks(chunks, reads: list, *, then=None):
    """The body in these chunks, the last one closing it unless `then` says
    what the client does next (stall, trickle, disconnect)."""
    async def receive():
        reads.append("read")
        index = len(reads) - 1
        if index < len(chunks):
            more = index < len(chunks) - 1 or then is not None
            return {"type": "http.request", "body": chunks[index], "more_body": more}
        assert then is not None, "read past the end of the body"
        return await then()
    return receive


def _upload(site, who: int | None = RUI, *, kind: str = "photo", archive: str = "0",
            chunks=(JPEG,), receive=None, csrf: str | None = None,
            length: str | None = "auto", sec_site: str = "same-origin",
            content_type: str = "application/octet-stream", name: str | None = None):
    """POST /anexo straight to the ASGI app, so the test owns `receive`: what
    the client sends, and whether the endpoint read it at all."""
    token = _token(site, who) if who is not None else "sessao-que-nao-existe"
    headers = [(b"host", b"testserver"), (b"x-anexo-tipo", kind.encode()),
               (b"x-arquivar", archive.encode()),
               (b"x-csrf", (staff_web.csrf_for(token) if csrf is None else csrf).encode()),
               (b"cookie", f"{staff_web.COOKIE}={token}".encode()),
               (b"content-type", content_type.encode()),
               (b"sec-fetch-site", sec_site.encode())]
    if name is not None:
        headers.append((b"x-anexo-nome", name.encode()))
    if length == "auto":
        length = str(sum(len(c) for c in chunks))
    if length is not None:
        headers.append((b"content-length", length.encode()))
    reads: list = []
    receive = receive(reads) if receive else _chunks(list(chunks), reads)
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "POST", "scheme": "https", "path": _ANEXO,
             "raw_path": _ANEXO.encode(), "query_string": b"", "root_path": "",
             "headers": headers, "client": ("127.0.0.1", 50000),
             "server": ("testserver", 443), "state": {}}
    sent: list = []

    async def send(message):
        sent.append(message)

    asyncio.run(site.app(scope, receive, send))
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return SimpleNamespace(status=sent[0]["status"], text=body.decode(), reads=len(reads))


def _uploads(site):
    return _rows(site, "SELECT staff_id, kind, media_type, archive, size_bytes, request_id "
                       "FROM panel_uploads ORDER BY created_at")


def _docx(paragraphs=("Contrato de locação.",), *, with_document: bool = True) -> bytes:
    """A real .docx, or a zip that only looks like one."""
    import zipfile
    out = io.BytesIO()
    if with_document:
        import docx
        document = docx.Document()
        for paragraph in paragraphs:
            document.add_paragraph(paragraph)
        document.save(out)
    else:
        with zipfile.ZipFile(out, "w") as archive:
            archive.writestr("xl/workbook.xml", "<workbook/>")
    return out.getvalue()


def test_an_attachment_is_sealed_and_answered_with_its_id(site, master_key):
    sent = _upload(site, RUI, archive="1", chunks=(JPEG[:20], JPEG[20:]), length=None)

    assert sent.status == 200, sent.text
    upload_id = json.loads(sent.text)["anexo"]
    assert _rows(site, "SELECT id::text FROM panel_uploads") == [(upload_id,)]
    assert _uploads(site) == [(RUI, "photo", "image/jpeg", True, len(JPEG), None)]
    assert assistant._SLOTS.taken == 0


@pytest.mark.parametrize("case, status, note", [
    ("switched off", 404, None),
    ("another site", 403, None),
    ("no token", 403, assistant.NOTES["refused"]),
    ("no session", 401, "ended"),
    ("unknown kind", 400, assistant.NOTES["refused"]),
    ("archive by an intern", 403, "Arquivar pelo chat fica com advogados e administradores."),
    ("busy", 409, "Aguarde a resposta anterior."),
    ("two already arriving", 429, None),
])
def test_every_refusal_comes_before_a_byte_of_the_body(site, master_key, monkeypatch,
                                                       case, status, note):
    who, kwargs = RUI, {}
    if case == "switched off":
        monkeypatch.setattr(settings, "web_assistant", "off")
    elif case == "another site":
        kwargs["sec_site"] = "cross-site"
    elif case == "no token":
        kwargs["csrf"] = ""
    elif case == "no session":
        who = None
    elif case == "unknown kind":
        kwargs["kind"] = "video"
    elif case == "archive by an intern":
        who, kwargs["archive"] = ANA, "1"
    elif case == "busy":
        assert site.run(lambda conn: panel.create_request(
            conn, staff_id=RUI, chat_id="400", kind="message", text="antes"))
    elif case == "two already arriving":
        monkeypatch.setattr(assistant._SLOTS, "taken", settings.panel_uploads_in_flight)

    sent = _upload(site, who, receive=_forbidden, **kwargs)

    assert sent.status == status and sent.reads == 0
    if note is not None:
        assert sent.text == note
    if case != "switched off":
        assert _uploads(site) == []


def test_a_lawyer_may_ask_to_archive(site, master_key):
    assert _upload(site, RUI, archive="1").status == 200


@pytest.mark.parametrize("length", ["lying", "absent"])
def test_the_byte_cap_holds_whatever_content_length_says(site, master_key, monkeypatch,
                                                         length):
    monkeypatch.setattr(settings, "panel_photo_max_bytes", 1000)
    chunks = [JPEG] + [b"\x00" * 300] * 20

    sent = _upload(site, RUI, chunks=chunks,
                   length=str(len(JPEG)) if length == "lying" else None)

    assert (sent.status, sent.text) == (413, "A foto passa de 10 MB.")
    assert sent.reads == 5, "the reading did not stop at the cap"
    assert _uploads(site) == [] and assistant._SLOTS.taken == 0


def test_a_declared_length_over_the_cap_is_refused_unread(site, master_key, monkeypatch):
    monkeypatch.setattr(settings, "panel_audio_max_bytes", 1000)

    sent = _upload(site, RUI, kind="audio", receive=_forbidden, length="1001")

    assert (sent.status, sent.text, sent.reads) == (413, "O áudio passa de 100 MB.", 0)
    assert assistant._SLOTS.taken == 0


def test_an_idle_body_is_cut_and_frees_its_slot(site, master_key, monkeypatch):
    monkeypatch.setattr(settings, "panel_upload_idle_seconds", 0.05)

    async def stall():
        await asyncio.sleep(30)

    sent = _upload(site, RUI, receive=lambda reads: _chunks([JPEG[:10]], reads, then=stall),
                   length=None)

    assert sent.status == 408 and assistant._SLOTS.taken == 0
    assert _uploads(site) == []
    assert _upload(site, RUI).status == 200, "the slot was not given back"


def test_a_trickling_body_is_cut_at_the_total_deadline(site, master_key, monkeypatch):
    monkeypatch.setattr(settings, "panel_upload_seconds", 0.3)
    monkeypatch.setattr(settings, "panel_upload_idle_seconds", 0.2)

    trickled: list = []

    async def trickle():
        # A byte every 20 ms, well inside the idle deadline, for two seconds:
        # only the total deadline stops it before it ends.
        await asyncio.sleep(0.02)
        trickled.append(1)
        return {"type": "http.request", "body": b"\x00", "more_body": len(trickled) < 100}

    sent = _upload(site, RUI, receive=lambda reads: _chunks([JPEG[:10]], reads, then=trickle),
                   length=None)

    assert sent.status == 408 and sent.reads > 2 and assistant._SLOTS.taken == 0


def test_a_client_that_leaves_frees_its_slot(site, master_key):
    async def leave():
        return {"type": "http.disconnect"}

    _upload(site, RUI, receive=lambda reads: _chunks([JPEG[:10]], reads, then=leave),
            length=None)

    assert assistant._SLOTS.taken == 0 and _uploads(site) == []


@pytest.mark.parametrize("kind, data, content_type, status, note", [
    ("audio", PNG, "audio/mpeg", 415, "Não reconheci esse arquivo como foto ou áudio."),
    ("photo", b"<script>alert(1)</script>" + b" " * 40, "image/jpeg", 415,
     "Não reconheci esse arquivo como foto ou áudio."),
    ("photo", HEIC, "image/jpeg", 415,
     "Essa foto está em HEIC. Exporte como JPEG e envie de novo."),
    ("voice", HEIC, "audio/mp4", 415, "Não reconheci esse arquivo como foto ou áudio."),
    ("photo", b"", "image/jpeg", 415, "Não reconheci esse arquivo como foto ou áudio."),
])
def test_the_type_is_read_from_the_bytes_never_from_the_name(site, master_key, kind, data,
                                                             content_type, status, note):
    sent = _upload(site, RUI, kind=kind, chunks=(data,), content_type=content_type)

    assert (sent.status, sent.text) == (status, note)
    assert _uploads(site) == []


def test_a_png_declared_as_audio_is_kept_as_the_png_it_is(site, master_key):
    assert _upload(site, RUI, chunks=(PNG,), content_type="audio/mpeg").status == 200
    assert _uploads(site)[0][2] == "image/png"


@pytest.mark.parametrize("data, media_type", [
    (JPEG, "image/jpeg"),
    (PNG, "image/png"),
    (HEIC, "image/heic"),
    (b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00", "audio/mp4"),
    (b"\x00\x00\x00\x20ftypiso5\x00\x00\x02\x00", "audio/mp4"),
    (b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00", None),
    (b"ID3\x04\x00\x00\x00\x00\x00\x00", "audio/mpeg"),
    (b"\xff\xfb\x90\x64\x00\x00", "audio/mpeg"),
    (b"RIFF\x24\x00\x00\x00WAVEfmt ", "audio/wav"),
    (b"RIFF\x24\x00\x00\x00AVI LIST", None),
    (b"OggS\x00\x02\x00\x00", "audio/ogg"),
    (b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81", "audio/webm"),
    (b"\xff\xf1\x50\x80\x02\x1f", "audio/aac"),
    (b"%PDF-1.7\n", None),
    (b"MZ\x90\x00", None),
    (b"\xff", None),
])
def test_sniff_knows_each_listed_type_by_its_first_bytes(data, media_type):
    assert assistant.sniff(data) == media_type


def test_the_live_cap_refuses_a_further_attachment(site, master_key, monkeypatch):
    monkeypatch.setattr(settings, "panel_uploads_max_live_bytes", len(JPEG) + 10)

    assert _upload(site, RUI).status == 200
    over = _upload(site, RUI)

    assert over.status == 409 and over.text.startswith("Você tem anexos ainda sem mensagem.")
    assert len(_uploads(site)) == 1 and assistant._SLOTS.taken == 0


def test_a_message_takes_its_attachment_even_without_text(site, master_key):
    upload_id = json.loads(_upload(site, RUI).text)["anexo"]

    response = _post(site, RUI, "/web/assistente/mensagem", {"texto": "", "anexo": upload_id})

    assert response.headers["location"] == "/web/assistente/#escrever"
    assert len(site.queued) == 1
    request_id = site.queued[0][0]["panel_request"]
    assert [str(row[5]) for row in _uploads(site)] == [request_id]


@pytest.mark.parametrize("whose", ["someone else", "not an id"])
def test_a_message_with_an_attachment_not_its_own_is_refused(site, master_key, whose):
    anexo = "nada"
    if whose == "someone else":
        anexo = json.loads(_upload(site, ANA).text)["anexo"]

    response = _post(site, RUI, "/web/assistente/mensagem", {"texto": "o que é?",
                                                             "anexo": anexo})

    assert response.headers["location"] == "/web/assistente/?aviso=no_upload#escrever"
    assert site.queued == [] and _rows(site, "SELECT count(*) FROM panel_requests") == [(0,)]


def test_empty_text_without_an_attachment_is_still_refused(site):
    response = _post(site, RUI, "/web/assistente/mensagem", {"texto": "  ", "anexo": ""})

    assert response.headers["location"] == "/web/assistente/?aviso=empty#escrever"
    assert site.queued == []


# ── the script's behaviour, run in Node ─────────────────────────────────────

_NODE_DRIVER = r"""
const listeners = {};
const field = { value: "", disabled: false, scrollHeight: 40, style: {},
  addEventListener(name, fn) { (listeners[name] ||= []).push(fn); } };
const send = { disabled: false };
const composer = { action: "/web/assistente/mensagem", requestSubmitted: 0,
  addEventListener(name, fn) { (listeners["form:" + name] ||= []).push(fn); },
  requestSubmit() { this.requestSubmitted += 1; },
  querySelector(sel) { return sel === "textarea" ? field : sel.startsWith("button") ? send : null; } };
const section = { dataset: { cursor: "" }, querySelector() { return null; } };
globalThis.document = {
  getElementById: (id) => (id === "conversa" ? section : null),
  querySelector: (sel) => (sel === "form.composer" ? composer : null),
  addEventListener() {}, visibilityState: "visible" };
globalThis.window = { addEventListener() {}, matchMedia: () => ({ matches: false }) };
globalThis.getComputedStyle = () => ({ maxHeight: "198px" });
globalThis.FormData = class { *[Symbol.iterator]() { yield ["texto", field.value]; } };
// What the server's poll says about the newest request; the first answer is
// that nothing is waiting any more.
let polled = 0;
const fresh = { dataset: { cursor: "c1" }, querySelectorAll: () => [] };
const state = { dataset: { estado: "nenhum" } };
globalThis.DOMParser = class { parseFromString() {
  return { getElementById: (id) => (id === "novas" ? fresh : id === "estado" ? state : null) }; } };
const posts = [];
const locked = [];
globalThis.fetch = async (url, options) => {
  if (options && options.method === "POST") {
    posts.push(String(options.body));
    locked.push(field.disabled && send.disabled);
  } else polled += 1;
  return { ok: true, status: 200, type: "basic",
           headers: { get: () => "text/plain" }, text: async () => "" };
};
await import(process.argv[2]);
const out = {};
out.start = { disabled: send.disabled, height: field.style.height };
field.value = "   "; listeners.input[0]();
out.spaces = send.disabled;
field.value = "oi"; field.scrollHeight = 120; listeners.input[0]();
out.typed = { disabled: send.disabled, height: field.style.height };
field.scrollHeight = 900; listeners.input[0]();
out.capped = field.style.height;
const key = (extra) => { const e = { key: "Enter", shiftKey: false, isComposing: false,
  prevented: false, preventDefault() { this.prevented = true; }, ...extra };
  listeners.keydown[0](e); return e.prevented; };
out.enter = [key({}), key({ shiftKey: true })];
out.requested = composer.requestSubmitted;
field.scrollHeight = 40;
field.value = "oi";
await listeners["form:submit"][0]({ preventDefault() {} });
out.after = { value: field.value, disabled: send.disabled, height: field.style.height,
              posts: posts.length, via: posts[0].includes("via=script") };
out.sent = posts[0];
// The poll finds the request done: the box opens, and a second message goes.
const settle = () => new Promise((resolve) => setTimeout(resolve, 20));
await settle();
out.polled = polled;
out.unlocked = { field: field.disabled, button: send.disabled };
field.value = "segunda"; listeners.input[0]();
out.second = send.disabled;
await listeners["form:submit"][0]({ preventDefault() {} });
await settle();
out.posts = posts.length;
out.lockedWhileSending = locked;
console.log(JSON.stringify(out));
process.exit(0);
"""


def test_the_script_grows_the_box_and_turns_the_button_on_and_off(tmp_path):
    """assistant.js run under Node with a minimal page: the button follows the
    text, the box grows to a cap, Enter sends and Shift+Enter does not, and
    after sending the box is empty and one line again."""
    import json
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    driver = tmp_path / "driver.mjs"
    driver.write_text(_NODE_DRIVER, encoding="utf-8")
    script = Path("core/web/static/assistant.js").resolve().as_uri()
    result = subprocess.run([node, str(driver), script], capture_output=True, text=True,
                            check=True, timeout=30)
    seen = json.loads(result.stdout)

    assert seen["start"] == {"disabled": True, "height": "40px"}
    assert seen["spaces"] is True, "only spaces is still empty"
    assert seen["typed"] == {"disabled": False, "height": "120px"}
    assert seen["capped"] == "198px", "taller than the cap scrolls instead"
    assert seen["enter"] == [True, False] and seen["requested"] == 1
    assert seen["after"] == {"value": "", "disabled": True, "height": "40px",
                             "posts": 1, "via": True}
    assert seen["sent"] == "texto=oi&via=script", "what was typed went, not an empty box"
    # One request at a time while it is sent; once the poll says it is done,
    # the box opens and the next message goes (a button stuck off fails here).
    assert seen["polled"] >= 1
    assert seen["unlocked"] == {"field": False, "button": True}, "empty again, but open"
    assert seen["second"] is False, "typing after an answer turns the button on"
    assert seen["posts"] == 2 and seen["lockedWhileSending"] == [True, True]


_NODE_ATTACHMENT_DRIVER = r"""
const listeners = {};
function element(name, extra = {}) {
  return { name, hidden: true, disabled: false, textContent: "", value: "", checked: false,
    required: true, attributes: {}, clicked: 0, files: [],
    setAttribute(key, value) { this.attributes[key] = value; },
    toggleAttribute(key, on) { if (key === "hidden") this.hidden = on; },
    addEventListener(kind, fn) { (listeners[name + ":" + kind] ||= []).push(fn); },
    click() { this.clicked += 1; }, ...extra };
}
const field = element("field", { hidden: false, scrollHeight: 40, style: {} });
const send = element("send", { hidden: false });
const nodes = {
  "[data-attach-toggle]": element("clip"), "[data-attach-choices]": element("choices"),
  "[data-photo-input]": element("photo"), "[data-audio-input]": element("audio"),
  "[data-microphone]": element("microphone"), "[data-record-clock]": element("clock"),
  "[data-record-icon]": element("recordIcon"), "[data-stop-icon]": element("stopIcon"),
  "[data-attachment-chip]": element("chip"), "[data-attachment-label]": element("label"),
  "[data-remove-attachment]": element("remove"), "[data-archive-choice]": element("archiveChoice"),
  "[data-archive]": element("archive"), "[data-choose-photo]": element("choosePhoto"),
  "[data-choose-audio]": element("chooseAudio"),
  "input[name=csrf]": element("csrf", { value: "token-1" }),
  textarea: field,
};
const composer = { action: "/web/assistente/mensagem",
  addEventListener(kind, fn) { (listeners["form:" + kind] ||= []).push(fn); },
  requestSubmit() {},
  querySelector(sel) { return sel.startsWith("button[type=submit]") ? send : nodes[sel] || null; } };
const section = { dataset: { cursor: "" }, querySelector() { return null; } };
const note = { textContent: "", hidden: true };
globalThis.document = {
  getElementById: (id) => (id === "conversa" ? section : id === "aviso" ? note : null),
  querySelector: (sel) => (sel === "form.composer" ? composer : null),
  addEventListener() {}, visibilityState: "visible" };
globalThis.window = { addEventListener() {}, matchMedia: () => ({ matches: false }) };
globalThis.getComputedStyle = () => ({ maxHeight: "198px" });
globalThis.FormData = class { *[Symbol.iterator]() { yield ["texto", field.value]; } };
const fresh = { dataset: { cursor: "c1" }, querySelectorAll: () => [] };
const state = { dataset: { estado: "nenhum" } };
globalThis.DOMParser = class { parseFromString() {
  return { getElementById: (id) => (id === "novas" ? fresh : id === "estado" ? state : null) }; } };
const calls = [];
let refuse = false;
globalThis.fetch = async (url, options = {}) => {
  calls.push({ url: String(url), method: options.method || "GET", headers: options.headers || {},
               body: options.body instanceof URLSearchParams ? String(options.body) : options.body });
  if (String(url).endsWith("/anexo") && refuse === "page") {
    return { ok: false, status: 404, headers: { get: () => "application/json" },
             text: async () => '{"detail":"Not Found"}' };
  }
  if (String(url).endsWith("/anexo") && refuse) {
    return { ok: false, status: 415, headers: { get: () => "text/plain; charset=utf-8" },
             text: async () => "Não reconheci esse arquivo como foto ou áudio." };
  }
  if (String(url).endsWith("/anexo")) {
    return { ok: true, status: 200, headers: { get: () => "application/json" },
             json: async () => ({ anexo: "abc-123" }) };
  }
  return { ok: true, status: 200, type: "basic", headers: { get: () => "text/plain" },
           text: async () => "" };
};
await import(process.argv[2]);
const out = {};
const fire = (key) => { for (const fn of listeners[key] || []) fn(); };
const settle = () => new Promise((resolve) => setTimeout(resolve, 20));
out.empty = send.disabled;
const photo = { size: 2048, type: "image/jpeg" };
nodes["[data-photo-input]"].files = [photo];
fire("photo:change");
out.chosen = { chip: nodes["[data-attachment-chip]"].hidden, label: nodes["[data-attachment-label]"].textContent,
               send: send.disabled, required: field.required,
               archive: nodes["[data-archive-choice]"].hidden };
nodes["[data-archive]"].checked = true;
await listeners["form:submit"][0]({ preventDefault() {} });
await settle();
out.calls = calls.filter((c) => c.method === "POST");
out.after = { chip: nodes["[data-attachment-chip]"].hidden, archive: nodes["[data-archive]"].checked,
              required: field.required };
// A refused attachment: said, and no message is sent.
refuse = true;
nodes["[data-audio-input]"].files = [{ size: 4096, type: "audio/mpeg" }];
fire("audio:change");
out.audioLabel = nodes["[data-attachment-label]"].textContent;
const before = calls.filter((c) => c.method === "POST").length;
await listeners["form:submit"][0]({ preventDefault() {} });
await settle();
out.refused = { posts: calls.filter((c) => c.method === "POST").length - before, note: note.textContent,
                chip: nodes["[data-attachment-chip]"].hidden, send: send.disabled,
                field: field.disabled };
// A refusal that is not the route's sentence is never shown as it came.
refuse = "page";
await listeners["form:submit"][0]({ preventDefault() {} });
await settle();
out.page = note.textContent;
// Removed, the button is off again (the box is empty).
fire("remove:click");
out.removed = { chip: nodes["[data-attachment-chip]"].hidden, send: send.disabled };
console.log(JSON.stringify(out));
process.exit(0);
"""


def test_the_script_sends_an_attachment_alone_upload_first(tmp_path):
    """assistant.js in Node with the composer's attachment controls: a photo
    alone turns the button on; sending uploads it first, with the form token
    and the archive choice in headers and the file as the raw body, then the
    message names it; a refused attachment is said and no message goes."""
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    driver = tmp_path / "driver.mjs"
    driver.write_text(_NODE_ATTACHMENT_DRIVER, encoding="utf-8")
    script = Path("core/web/static/assistant.js").resolve().as_uri()
    result = subprocess.run([node, str(driver), script], capture_output=True, text=True,
                            check=True, timeout=30)
    seen = json.loads(result.stdout)

    assert seen["empty"] is True
    assert seen["chosen"] == {"chip": False, "label": "Foto · 2 KB", "send": False,
                              "required": False, "archive": False}, "a photo alone may go"
    upload, message = seen["calls"]
    assert upload["url"] == "/web/assistente/anexo"
    assert upload["headers"] == {"X-CSRF": "token-1", "X-Anexo-Tipo": "photo",
                                 "X-Arquivar": "1"}
    assert upload["body"] == {"size": 2048, "type": "image/jpeg"}, "the file itself, raw"
    assert message["url"] == "/web/assistente/mensagem"
    assert message["body"] == "texto=&via=script&anexo=abc-123"
    assert seen["after"] == {"chip": True, "archive": False, "required": True}
    assert seen["audioLabel"] == "Áudio · 4 KB"
    assert seen["refused"] == {"posts": 1, "note": "Não reconheci esse arquivo como foto ou áudio.",
                               "chip": False, "send": False, "field": False}
    assert seen["page"] == "Recarregue a página e tente de novo."
    assert seen["removed"] == {"chip": True, "send": True}


def test_an_attachment_sent_alone_is_its_chip_without_an_empty_line(site):
    _turn(site, RUI, "person", "", extras={"attachment": {"kind": "voice", "seconds": 42}})
    _turn(site, RUI, "person", "e isto?", extras={"attachment": {"kind": "photo"}})

    page = _get(site, RUI).text

    assert "<p class=attachment-chip>Nota de voz 0:42</p></li>" in page
    assert "<p class=attachment-chip>Foto</p><p class=text>e isto?</p>" in page
    assert "<p class=text></p>" not in page


# ── a document for the archive (Task M.6) ──────────────────────────────────

_FORMAT = "Formato não suportado. Envie PDF, DOCX, TXT, Markdown ou uma imagem."
_UNIDENTIFIED = "Não consegui identificar o arquivo enviado."
_INTERN_REFUSAL = authorization.decide(
    StaffMember(id=5, chat_id="500", name="Ana", role="estagiario"), "document.ingest").reason
TIFF = b"II*\x00" + b"\x00" * 40


def _documents_kept(site):
    return _rows(site, "SELECT staff_id, kind, media_type, archive, filename "
                       "FROM panel_uploads ORDER BY created_at")


@pytest.mark.parametrize("name, data, media_type", [
    ("Contrato%20de%20loca%C3%A7%C3%A3o.pdf", b"%PDF-1.7\n1 0 obj", "application/pdf"),
    ("peticao.DOCX", None, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ("nota.txt", "Ata da reunião.".encode(), "text/plain"),
    ("resumo.md", b"# Resumo\n", "text/markdown"),
    ("lei.html", b"\xef\xbb\xbf  <html><body>Lei</body></html>", "text/html"),
    ("pagina.htm", "<p>Lei nº 8.245</p>".encode("cp1252"), "text/html"),
    ("despacho.jpg", JPEG, "image/jpeg"),
    ("despacho.jpeg", JPEG, "image/jpeg"),
    ("scan.png", PNG, "image/png"),
    ("scan.tif", TIFF, "image/tiff"),
    ("scan.tiff", b"MM\x00*" + b"\x00" * 40, "image/tiff"),
])
def test_each_document_type_is_accepted_by_its_bytes(site, master_key, name, data, media_type):
    data = _docx() if data is None else data

    sent = _upload(site, RUI, kind="document", name=name, chunks=(data,))

    assert sent.status == 200, sent.text
    from urllib.parse import unquote
    assert _documents_kept(site) == [(RUI, "document", media_type, False, unquote(name))]


@pytest.mark.parametrize("name, data", [
    ("contrato.pdf", PNG),
    ("contrato.pdf", b""),
    ("peticao.docx", b"%PDF-1.7\n"),
    ("peticao.docx", None),  # a zip, but not a Word document
    ("nota.txt", b"\xff\xfeA\x00t\x00a\x00"),  # UTF-16
    ("nota.txt", "Ata da reunião".encode("latin-1")),  # not UTF-8, and no NUL
    ("nota.md", b"texto\x00binario"),
    ("lei.html", b"%PDF-1.7\n"),
    ("despacho.jpg", PNG),
    ("scan.png", JPEG),
    ("scan.tiff", b"%PDF-1.7\n"),
])
def test_a_document_whose_name_lies_is_refused_in_telegrams_words(site, master_key, name,
                                                                 data):
    data = _docx(with_document=False) if data is None else data

    sent = _upload(site, RUI, kind="document", name=name, chunks=(data,))

    assert (sent.status, sent.text) == (415, _FORMAT)
    assert _documents_kept(site) == [] and assistant._SLOTS.taken == 0


@pytest.mark.parametrize("case, status, note", [
    ("an intern", 403, _INTERN_REFUSAL),
    ("no name", 400, _UNIDENTIFIED),
    ("a path for a name", 400, _UNIDENTIFIED),
    ("a format Telegram refuses", 415, _FORMAT),
    ("over the documents' limit", 413, "O documento excede o limite de 25 MB."),
])
def test_a_document_is_refused_before_a_byte_of_the_body(site, master_key, case, status, note):
    who, name, length = RUI, "contrato.pdf", "auto"
    if case == "an intern":
        who = ANA
    elif case == "no name":
        name = None
    elif case == "a path for a name":
        name = "..%2F..%2Fetc%2Fcontrato.pdf"
    elif case == "a format Telegram refuses":
        name = "programa.exe"
    elif case == "over the documents' limit":
        length = str(25 * 1024 * 1024 + 1)

    sent = _upload(site, who, kind="document", name=name, receive=_forbidden, length=length)

    assert (sent.status, sent.text, sent.reads) == (status, note, 0)
    assert _documents_kept(site) == [] and assistant._SLOTS.taken == 0


@pytest.mark.parametrize("raw, name", [
    ("contrato.pdf", "contrato.pdf"),
    ("Contrato%20de%20loca%C3%A7%C3%A3o%20(1).pdf", "Contrato de locação (1).pdf"),
    ("%3Cb%3Ex%3C%2Fb%3E.pdf", None),  # a "/" decoded is still a path separator
    ("%3Cb%3Ex.pdf", "<b>x.pdf"),  # markup is a name like any other, shown escaped
    ("", None),
    ("a%2Fb.pdf", None),
    ("a%5Cb.pdf", None),
    ("..", None),
    ("%00.pdf", None),
    ("linha%0Anova.pdf", None),
    ("tab%09.pdf", None),
    ("%E2%80%AEfdp.exe", None),  # right-to-left override: not printable
    ("%FF.pdf", None),  # not UTF-8
    ("contratoç.pdf", None),  # raw non-ASCII: a header carries it encoded
    ("%20contrato.pdf", None),
    ("contrato.pdf%20", None),
    ("a" * 196 + ".pdf", "a" * 196 + ".pdf"),
    ("a" * 197 + ".pdf", None),
])
def test_a_documents_name_is_a_name_and_never_a_path(raw, name):
    assert assistant.document_name(raw) == name


def test_documento_is_offered_only_to_whom_may_archive(site):
    lawyer, intern = _get(site, RUI).text, _get(site, ANA).text

    assert "data-choose-document>Documento</button>" in lawyer
    accept = ",".join(sorted(ingestion.DOCUMENT_EXTENSIONS))
    assert f"data-document-input hidden accept='{accept}'" in lawyer
    assert f"data-limit={25 * 1024 * 1024} " in lawyer
    assert "data-too-large='O documento excede o limite de 25 MB.'" in lawyer
    assert f"data-format='{_FORMAT}'" in lawyer
    assert "data-choose-document" not in intern and "data-document-input" not in intern


def test_a_document_turn_shows_its_name_escaped(site):
    _turn(site, RUI, "person", "", extras={"attachment": {
        "kind": "document", "size_bytes": 10, "name": "<script>x</script>.pdf"}})

    page = _get(site, RUI).text

    assert ("<p class=attachment-chip>Documento · &lt;script&gt;x&lt;/script&gt;.pdf</p>"
            in page)
    assert "<script>x" not in page


def test_a_document_message_waits_for_the_morning_with_its_upload(site, master_key,
                                                                 monkeypatch):
    monkeypatch.setattr(work_hours, "is_office_hours", lambda: False)
    upload_id = json.loads(_upload(site, RUI, kind="document", name="nota.txt",
                                   chunks=(b"Ata.",)).text)["anexo"]

    response = _post(site, RUI, "/web/assistente/mensagem", {"texto": "", "anexo": upload_id})

    assert response.status_code == 303 and site.queued[0][1] is True, "deferred"
    assert [str(row[5]) for row in _uploads(site)] == [site.queued[0][0]["panel_request"]]
    state = _get(site, RUI).text
    assert "Fora do horário" in state


def test_the_page_says_a_document_is_being_processed(site, master_key):
    upload_id = json.loads(_upload(site, RUI, kind="document", name="nota.txt",
                                   chunks=(b"Ata.",)).text)["anexo"]
    _post(site, RUI, "/web/assistente/mensagem", {"texto": "", "anexo": upload_id})

    page = _get(site, RUI).text

    assert "<div class='note state' role=status>Processando o documento…</div>" in page


# ── the actions ─────────────────────────────────────────────────────────────

CNJ = "1000000-37.2024.8.26.0100"
CNJ_DIGITS = "10000003720248260100"
OTHER_DIGITS = "20000002020248260100"


def _execute(site, statement: str, params=()) -> None:
    async def run(conn):
        async with conn.cursor() as cur:
            await cur.execute(statement, params)
        await conn.commit()
    site.run(run)


def test_an_action_queues_its_command_and_shows_its_label(site):
    response = _post(site, RUI, "/web/assistente/acao",
                     {"acao": "prazo", "data": "2026-10-06", "dias": "15", "regime": "uteis"})

    assert response.status_code == 303
    assert response.headers["location"] == "/web/assistente/#escrever"
    (update, _deferred), = site.queued
    assert update["message"]["text"] == "/prazo 06/10/2026 15 úteis"
    assert update["message"]["chat"]["id"] == 400
    page = _get(site, RUI).text
    assert _texts(page) == ["Calcular prazo · 06/10/2026, 15 dias úteis"]
    assert "/prazo" not in page


def test_the_list_offers_the_actions_outside_the_composer(site):
    page = _get(site, RUI).text

    menu = page.index("<details class=actions")
    composer = page.index("<form class=composer")
    assert menu < composer, "a form inside the composer's form would be dropped"
    for label in ("Calcular prazo", "Feriados", "Consultar processo", "Autos no PJe",
                  "Intimações pendentes", "Acompanhar minha OAB", "Acompanhar processo",
                  "Meus acompanhamentos"):
        assert label in page
    assert page.count("action='/web/assistente/acao'") >= 6


def test_an_action_the_rank_does_not_allow_is_refused_and_not_listed(site, monkeypatch):
    real = authorization.decide
    seen = []

    def deny_pje(staff, action):
        seen.append((staff.id, staff.role, action))
        if action == "pje.read":
            return authorization.Decision(allowed=False, action=action, reason="não")
        return real(staff, action)

    monkeypatch.setattr(authorization, "decide", deny_pje)
    assert "Autos no PJe" not in _get(site, RUI).text
    seen.clear()
    # The rank changes after the page was drawn: the POST decides on the new one.
    _execute(site, "UPDATE staff SET role = 'estagiario' WHERE id = %s", (RUI,))

    response = _post(site, RUI, "/web/assistente/acao", {"acao": "autos", "cnj": CNJ})

    assert response.headers["location"] == "/web/assistente/?aviso=refused#escrever"
    assert site.queued == []
    assert (RUI, "estagiario", "pje.read") in seen, "decided on the row read on this request"
    assert all(role == "estagiario" for _id, role, _action in seen)


def test_a_person_deactivated_after_the_page_cannot_act(site):
    token = _token(site, RUI)
    cookie = {"cookie": f"{staff_web.COOKIE}={token}", "sec-fetch-site": "same-origin"}
    assert site.client.get("/web/assistente/", headers=cookie).status_code == 200
    _execute(site, "UPDATE staff SET active = FALSE WHERE id = %s", (RUI,))

    response = site.client.post("/web/assistente/acao", headers=cookie,
                                data={"acao": "feriados", "csrf": staff_web.csrf_for(token)})

    assert response.status_code == 401, "signed out, as the page answers nobody"
    assert site.queued == []
    assert _rows(site, "SELECT count(*) FROM panel_requests") == [(0,)]


def test_an_unknown_action_is_refused(site):
    response = _post(site, RUI, "/web/assistente/acao", {"acao": "promover", "id": "3"})

    assert response.headers["location"] == "/web/assistente/?aviso=refused#escrever"
    assert site.queued == []


def test_bad_fields_are_refused_with_a_fixed_note(site):
    response = _post(site, RUI, "/web/assistente/acao",
                     {"acao": "prazo", "data": "2026-10-06", "dias": "0<b>",
                      "regime": "uteis"})

    assert response.headers["location"] == "/web/assistente/?aviso=action_fields#escrever"
    assert site.queued == []
    page = _get(site, RUI, response.headers["location"]).text
    assert "Confira os campos e tente de novo." in page
    assert "0<b>" not in page and "0&lt;b&gt;" not in page


def test_an_action_while_busy_is_refused(site):
    _post(site, RUI, "/web/assistente/mensagem", {"texto": "primeira"})

    response = _post(site, RUI, "/web/assistente/acao", {"acao": "feriados"})
    page = _get(site, RUI).text

    assert response.headers["location"] == "/web/assistente/?aviso=busy#escrever"
    assert len(site.queued) == 1
    menu = page[page.index("<details class=actions"):page.index("<form class=composer")]
    assert "<button type=submit" in menu
    assert all("disabled" in b for b in re.findall(r"<button type=submit[^>]*>", menu))


def test_an_action_out_of_hours_is_deferred_with_its_label(site, monkeypatch):
    monkeypatch.setattr(work_hours, "is_office_hours", lambda: False)

    _post(site, RUI, "/web/assistente/acao", {"acao": "feriados"})
    page = _get(site, RUI).text

    assert site.queued[0][1] is True
    assert "Feriados" in _texts(page)
    assert "Retirar" in page


def _watch(site, who: int):
    async def seed(conn):
        await publications.watch_oab(conn, who, "123456", "SP")
        await publications.watch_process(conn, who, CNJ_DIGITS)
        await conn.commit()
    site.run(seed)


def test_my_follows_are_listed_with_a_stop_each(site):
    assert "Você ainda não acompanha nada." in _get(site, RUI).text
    _watch(site, RUI)

    page = _get(site, RUI).text

    assert "value='oab:123456/SP'" in page
    assert f"value='processo:{CNJ_DIGITS}'" in page
    assert "Você ainda não acompanha nada." not in page
    _post(site, RUI, "/web/assistente/acao",
          {"acao": "desacompanhar", "alvo": f"processo:{CNJ_DIGITS}"})
    assert site.queued[0][0]["message"]["text"] == f"/desacompanhar {CNJ}"


def test_the_follows_fragment_is_this_persons_and_fresh(site):
    async def seed_ana(conn):
        await publications.watch_oab(conn, ANA, "999999", "RJ")
        await publications.watch_process(conn, ANA, OTHER_DIGITS)
        await conn.commit()
    site.run(seed_ana)
    _watch(site, RUI)

    fresh = _get(site, RUI, "/web/assistente/acompanhamentos")
    assert fresh.status_code == 200
    assert "value='oab:123456/SP'" in fresh.text and "data-follows" in fresh.text
    assert fresh.text.count("value='oab:") == 1, "only this person's"
    assert "999999" not in fresh.text and OTHER_DIGITS not in fresh.text

    _execute(site, "DELETE FROM watched_oabs WHERE staff_id = %s", (RUI,))
    again = _get(site, RUI, "/web/assistente/acompanhamentos").text
    assert "value='oab:" not in again and f"value='processo:{CNJ_DIGITS}'" in again

    anonymous = site.client.get("/web/assistente/acompanhamentos")
    assert anonymous.status_code in (303, 401, 403)
    assert "value='" not in anonymous.text


def test_a_label_is_escaped_and_a_bad_one_falls_back(site):
    _turn(site, RUI, "person", "/feriados", extras={"action": "<b>x</b>"})
    _turn(site, RUI, "person", "texto um", extras={"action": 5})
    _turn(site, RUI, "person", "texto dois", extras={"action": "y" * 121})
    _turn(site, RUI, "bot", "resposta", extras={"action": "não sou da pessoa"})

    page = _get(site, RUI).text

    assert "&lt;b&gt;x&lt;/b&gt;" in page and "<b>x</b>" not in page
    assert _texts(page)[1:] == ["texto um", "texto dois", "resposta"]


def test_the_follows_fragment_keeps_its_buttons_off_while_busy(site):
    _watch(site, RUI)
    _post(site, RUI, "/web/assistente/mensagem", {"texto": "primeira"})

    fragment = _get(site, RUI, "/web/assistente/acompanhamentos").text

    buttons = re.findall(r"<button type=submit[^>]*>", fragment)
    assert buttons and all("disabled" in b for b in buttons)


# ── dictation: a voice note read back into the box ──────────────────────────

_DITADO = "/web/assistente/ditado"
OGG = b"OggS" + b"\0" * 200


def _dictate(site, who: int | None = RUI, body: bytes = OGG, *, csrf: str | None = None,
             sec_site: str = "same-origin"):
    token = _token(site, who) if who is not None else "sessao-que-nao-existe"
    headers = {"x-csrf": staff_web.csrf_for(token) if csrf is None else csrf,
               "cookie": f"{staff_web.COOKIE}={token}", "sec-fetch-site": sec_site,
               "content-type": "application/octet-stream"}
    return site.client.post(_DITADO, content=body, headers=headers)


@pytest.fixture
def transcribed(monkeypatch):
    heard: list[tuple[bytes, bool, str | None]] = []
    said = {"text": "qual o prazo do recurso do caso Acme?"}

    async def transcribe(data, *, voice, prompt):
        heard.append((data, voice, prompt))
        if isinstance(said["text"], Exception):
            raise said["text"]
        return media.Transcript(text=said["text"], seconds=4.0)

    monkeypatch.setattr(media, "transcribe_audio", transcribe)
    # Each test counts its own dictations: the limiter lives in the process.
    monkeypatch.setattr(assistant, "_DICTATION_LIMITER",
                        assistant.RateLimiter(assistant.DICTATIONS_PER_HOUR, 3600))
    return SimpleNamespace(heard=heard, said=said)


def test_dictation_answers_the_text_and_stores_nothing(site, transcribed):
    response = _dictate(site)

    assert response.status_code == 200
    assert response.json() == {"texto": "qual o prazo do recurso do caso Acme?"}
    (data, voice, prompt), = transcribed.heard
    assert data == OGG and voice is True and prompt
    for table in ("panel_requests", "panel_uploads", "conversation_turns"):
        assert _rows(site, f"SELECT count(*) FROM {table}") == [(0,)]
    assert site.queued == []


@pytest.mark.parametrize("how", ["missing", "cross-site", "no session"])
def test_dictation_needs_the_token_this_site_and_a_session(site, transcribed, how):
    if how == "missing":
        response = _dictate(site, csrf="")
    elif how == "cross-site":
        response = _dictate(site, sec_site="cross-site")
    else:
        response = _dictate(site, None)

    assert response.status_code in (401, 403)
    assert transcribed.heard == []


def test_dictation_takes_only_audio(site, transcribed):
    response = _dictate(site, body=JPEG)

    assert response.status_code == 415
    assert transcribed.heard == []


def test_dictation_refuses_more_than_a_voice_note(site, transcribed, monkeypatch):
    monkeypatch.setattr(settings, "panel_voice_max_bytes", 100)

    response = _dictate(site)

    assert response.status_code == 413
    assert response.text == "A nota de voz passa de 5 MB."
    assert transcribed.heard == []


def test_silence_is_said_and_not_returned_as_text(site, transcribed):
    transcribed.said["text"] = " "

    response = _dictate(site)

    assert response.status_code == 422
    assert "sem fala audível" in response.text


def test_a_media_error_is_said_in_its_own_words(site, transcribed):
    transcribed.said["text"] = media.MediaError(media.VOICE_TOO_LONG)

    response = _dictate(site)

    assert response.status_code == 422
    assert response.text == media.VOICE_TOO_LONG


def test_a_spent_budget_is_said_and_whisper_is_not_asked(site, transcribed):
    transcribed.said["text"] = budget.BudgetExceeded("month", "amanhã")

    response = _dictate(site)

    assert response.status_code == 429
    assert "orçamento" in response.text.casefold() and "amanhã" in response.text


def test_dictation_is_limited_per_person(site, transcribed, monkeypatch):
    calls = []

    async def limited(key, max_events, window_seconds):
        calls.append((key, max_events, window_seconds))
        return False, True

    monkeypatch.setattr(assistant.cache, "rate_limit_hit", limited)

    response = _dictate(site)

    assert response.status_code == 429
    assert transcribed.heard == []
    assert calls == [(f"dictation:{RUI}", 20, 3600)]


def test_the_composer_offers_the_actions_beside_the_clip(site):
    page = _get(site, RUI).text
    tools = page[page.index("<div class=composer-tools>"):page.index("class=send")]

    assert "data-actions-toggle" in tools and "aria-label='Ações'" in tools
    # In this order: actions, clip, microphone; the actions as three lines.
    assert (tools.index("data-actions-toggle") < tools.index("data-attach-toggle")
            < tools.index("data-microphone"))
    toggle = tools[tools.index("data-actions-toggle"):tools.index("data-attach-toggle")]
    assert actions.ICON in toggle and "d='M4 6h16M4 12h16M4 18h10'" in toggle


_NODE_DICTATION_DRIVER = r"""
const listeners = {};
function element(name, extra = {}) {
  return { name, hidden: true, disabled: false, textContent: "", value: "", checked: false,
    required: true, attributes: {}, clicked: 0, files: [], classes: new Set(),
    classList: { toggle(c, on) { on ? this.owner.classes.add(c) : this.owner.classes.delete(c); },
                 contains(c) { return this.owner.classes.has(c); } },
    setAttribute(key, value) { this.attributes[key] = value; },
    toggleAttribute(key, on) { if (key === "hidden") this.hidden = on; },
    addEventListener(kind, fn) { (listeners[name + ":" + kind] ||= []).push(fn); },
    focus() { this.focused = true; }, click() { this.clicked += 1; }, ...extra };
}
function el(name, extra) { const e = element(name, extra); e.classList.owner = e; return e; }
const field = el("field", { hidden: false, scrollHeight: 40, style: {} });
const send = el("send", { hidden: false });
const box = el("box", { hidden: false });
const nodes = {
  "[data-attach-toggle]": el("clip"), "[data-attach-choices]": el("choices"),
  "[data-photo-input]": el("photo"), "[data-audio-input]": el("audio"),
  "[data-microphone]": el("microphone"), "[data-recording]": el("bar"),
  "[data-record-cancel]": el("cancel"), "[data-record-done]": el("done"),
  "[data-record-clock]": el("clock"), "[data-record-state]": el("state"),
  "[data-wave]": null,
  "[data-attachment-chip]": el("chip"), "[data-attachment-label]": el("label"),
  "[data-remove-attachment]": el("remove"), "[data-archive-choice]": el("archiveChoice"),
  "[data-archive]": el("archive"), "[data-choose-photo]": el("choosePhoto"),
  "[data-choose-audio]": el("chooseAudio"), ".box": box,
  "input[name=csrf]": el("csrf", { value: "token-1" }),
  textarea: field,
};
const composer = { action: "/web/assistente/mensagem",
  addEventListener(kind, fn) { (listeners["form:" + kind] ||= []).push(fn); },
  requestSubmit() {},
  querySelector(sel) { return sel.startsWith("button[type=submit]") ? send : nodes[sel] || null; } };
const section = { dataset: { cursor: "" }, querySelector() { return null; } };
const note = { textContent: "", hidden: true };
globalThis.document = {
  getElementById: (id) => (id === "conversa" ? section : id === "aviso" ? note : null),
  querySelector: (sel) => (sel === "form.composer" ? composer : null),
  addEventListener() {}, visibilityState: "visible" };
globalThis.window = { addEventListener() {}, matchMedia: () => ({ matches: false }),
                      MediaRecorder: true };
globalThis.getComputedStyle = () => ({ maxHeight: "198px" });
globalThis.requestAnimationFrame = () => 0;
globalThis.cancelAnimationFrame = () => {};
let stopped = 0;
Object.defineProperty(globalThis, "navigator", { configurable: true, value: {
  mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop() { stopped += 1; } }] }) } } });
class Recorder {
  static isTypeSupported(type) { return type === "audio/mp4"; }
  constructor(stream, options) { this.mimeType = options.mimeType; this.state = "inactive"; this.on = {}; }
  addEventListener(kind, fn) { (this.on[kind] ||= []).push(fn); }
  start() { this.state = "recording"; }
  stop() {
    this.state = "inactive";
    for (const fn of this.on.dataavailable || []) fn({ data: new Blob(["voz"]) });
    for (const fn of this.on.stop || []) fn();
  }
}
globalThis.MediaRecorder = Recorder;
window.MediaRecorder = Recorder;
const calls = [];
globalThis.fetch = async (url, options = {}) => {
  calls.push({ url: String(url), headers: options.headers || {}, body: options.body });
  return { ok: true, status: 200, headers: { get: () => "application/json" },
           json: async () => ({ texto: "qual o prazo do recurso?" }) };
};
await import(process.argv[2]);
const fire = async (key) => { for (const fn of listeners[key] || []) await fn(); };
const settle = () => new Promise((resolve) => setTimeout(resolve, 30));
const out = {};
field.value = "Bom dia.";
await fire("microphone:click");
await settle();
out.recording = { bar: nodes["[data-recording]"].hidden, box: box.classes.has("is-recording"),
                  send: send.disabled };
await fire("done:click");
await settle();
out.done = { value: field.value, bar: nodes["[data-recording]"].hidden,
             box: box.classes.has("is-recording"), send: send.disabled, stopped };
out.call = calls.map((c) => ({ url: c.url, csrf: c.headers["X-CSRF"], blob: c.body instanceof Blob }));
await fire("microphone:click");
await settle();
await fire("cancel:click");
await settle();
out.cancelled = { calls: calls.length, value: field.value, bar: nodes["[data-recording]"].hidden };
console.log(JSON.stringify(out));
process.exit(0);
"""


def test_the_script_dictates_into_the_box(tmp_path):
    """assistant.js in Node: the microphone turns the box into the recording
    bar, the tick sends the recording to /ditado and puts the text in the box
    after what was there, ready to correct and send; X drops it unsent."""
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    driver = tmp_path / "driver.mjs"
    driver.write_text(_NODE_DICTATION_DRIVER, encoding="utf-8")
    script = Path("core/web/static/assistant.js").resolve().as_uri()
    result = subprocess.run([node, str(driver), script], capture_output=True, text=True,
                            check=True, timeout=30)
    seen = json.loads(result.stdout)

    assert seen["recording"] == {"bar": False, "box": True, "send": True}
    assert seen["done"] == {"value": "Bom dia. qual o prazo do recurso?", "bar": True,
                            "box": False, "send": False, "stopped": 1}
    assert seen["call"] == [{"url": "/web/assistente/ditado", "csrf": "token-1", "blob": True}]
    assert seen["cancelled"] == {"calls": 1, "value": "Bom dia. qual o prazo do recurso?",
                                 "bar": True}


def test_dictation_is_limited_even_without_the_shared_counter(site, transcribed, monkeypatch):
    async def always(key, max_events, window_seconds):
        return True, False

    monkeypatch.setattr(assistant.cache, "rate_limit_hit", always)
    monkeypatch.setattr(assistant, "_DICTATION_LIMITER",
                        assistant.RateLimiter(assistant.DICTATIONS_PER_HOUR, 3600))

    answers = [_dictate(site).status_code for _ in range(assistant.DICTATIONS_PER_HOUR + 1)]

    assert answers[:-1] == [200] * assistant.DICTATIONS_PER_HOUR
    assert answers[-1] == 429
    assert len(transcribed.heard) == assistant.DICTATIONS_PER_HOUR


_NODE_DICTATION_EDGES = _NODE_DICTATION_DRIVER.split("await import(process.argv[2]);")[0] + r"""
let release;
let big = false;
globalThis.fetch = async (url, options = {}) => {
  calls.push({ url: String(url), headers: options.headers || {}, body: options.body });
  await new Promise((resolve) => { release = resolve; });
  if (options.signal && options.signal.aborted) throw new DOMException("aborted", "AbortError");
  return { ok: true, status: 200, headers: { get: () => "application/json" },
           json: async () => ({ texto: "tarde demais" }) };
};
const BaseStop = Recorder.prototype.stop;
Recorder.prototype.stop = function () {
  if (!big) return BaseStop.call(this);
  this.state = "inactive";
  for (const fn of this.on.dataavailable || []) fn({ data: { size: 6 * 1024 * 1024 } });
  for (const fn of this.on.stop || []) fn();
};
await import(process.argv[2]);
const fire = async (key) => { for (const fn of listeners[key] || []) await fn(); };
const settle = () => new Promise((resolve) => setTimeout(resolve, 30));
const out = {};
field.value = "Rascunho";
// X while it is being read: the box keeps what it had, even when the answer comes.
await fire("microphone:click"); await settle();
fire("done:click"); await settle();
out.reading = { bar: nodes["[data-recording]"].hidden, state: nodes["[data-record-state]"].hidden };
await fire("cancel:click"); await settle();
release(); await settle();
out.cancelled = { value: field.value, bar: nodes["[data-recording]"].hidden,
                  microphone: nodes["[data-microphone]"].disabled };
// A recording that ends over the limit: said, and the next one still finishes.
big = true;
await fire("microphone:click"); await settle();
await fire("done:click"); await settle();
out.tooBig = note.textContent;
big = false;
const before = calls.length;
await fire("microphone:click"); await settle();
fire("done:click"); await settle();
out.nextFinishes = calls.length - before;
release(); await settle();
console.log(JSON.stringify(out));
process.exit(0);
"""


def test_the_script_cancels_a_dictation_being_read_and_recovers_from_a_large_one(tmp_path):
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    driver = tmp_path / "driver.mjs"
    driver.write_text(_NODE_DICTATION_EDGES, encoding="utf-8")
    script = Path("core/web/static/assistant.js").resolve().as_uri()
    result = subprocess.run([node, str(driver), script], capture_output=True, text=True,
                            check=True, timeout=30)
    seen = json.loads(result.stdout)

    assert seen["reading"] == {"bar": False, "state": False}
    assert seen["cancelled"] == {"value": "Rascunho", "bar": True, "microphone": False}
    assert "5 MB" in seen["tooBig"]
    assert seen["nextFinishes"] == 1, "after a too-large recording, the tick still reads"
