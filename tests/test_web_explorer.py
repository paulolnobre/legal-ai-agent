"""The explorer shows folders, clients and documents: exactly what the bot shows.

Built on the real schema, because what is under test is mostly queries: which
rows the scope lets into a count, a tile or a card, and whether a client's tile
leads to exactly that client's documents. A double would only test the double.

The cast of tests/test_web_files.py: Rui and Ana share the team Cível (Rui is
also alone in Vazia), Bia is in Trabalhista, Paulo is the owner and sees no
private folder but his own. The firm's shelf holds clients whose names are
hard to carry in an address, and 120 documents of one client for paging.

Skipped without TEST_DATABASE_URL; CI provides one.
"""
import asyncio
import html
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, LiteralString, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import sql

from core.config import settings
from core.repositories import web_access
from core.repositories.documents import ensure_document_storage_schema
from core.repositories.staff import ensure_access_schema
from core.repositories.teams import ensure_team_schema
from core.web import explorer, files, staff_web

URL = os.environ.get("TEST_DATABASE_URL")
SCHEMA = f"explorer_{uuid.uuid4().hex[:12]}"

pytestmark = pytest.mark.skipif(
    not URL, reason="needs a real Postgres: set TEST_DATABASE_URL",
)

PAULO, RUI, ANA, BIA = 1, 4, 5, 6
SYMBOLS = "A & B + C #1 'x' %20"
LONG = "L" * 255
VOLUME = list(range(1119, 999, -1))  # newest first: same timestamp, id breaks the tie

_SEED = """
INSERT INTO staff (id, chat_id, name, role) VALUES
    (1, '100', 'Paulo', 'owner'), (4, '400', 'Rui', 'advogado'),
    (5, '500', 'Ana', 'estagiario'), (6, '600', 'Bia', 'advogado');
INSERT INTO teams (id, name) VALUES (10, 'Cível'), (11, 'Trabalhista'), (12, 'Vazia'),
    (13, '<Sócios & Cia>');
INSERT INTO team_members (team_id, staff_id) VALUES (10, 4), (10, 5), (11, 6), (12, 4),
    (13, 4);
INSERT INTO documents (id, title, doc_type, matter, full_text, uploaded_by,
                       visibility, team_id, content_sha256) VALUES
    (101, 'Lei do Inquilinato', 'lei', NULL, '', 1, 'firm', NULL, 'a1'),
    (102, 'Contrato Acme', 'contrato', 'Acme', '', 4, 'private', NULL, 'a2'),
    (103, 'Parecer Beta', 'parecer', 'Beta', '', 6, 'private', NULL, 'a3'),
    (104, 'Petição Cível', 'peticao', 'Gama', '', 5, 'team', 10, 'a4'),
    (105, 'Reclamação', 'peticao', 'Delta', '', 6, 'team', 11, 'a5'),
    (106, 'Ata da assembleia', 'ata', 'Acme Holdings', '', 4, 'firm', NULL, 'a6'),
    (107, 'Acordo de confidencialidade', 'contrato', 'Acme', '', 4, 'firm', NULL, 'a7'),
    (108, 'Percentual', 'nota', '%', '', 4, 'firm', NULL, 'a8'),
    (109, 'Sem nome', 'nota', '  ', '', 4, 'firm', NULL, 'a9'),
    (110, 'Símbolos', 'nota', 'symbols', '', 4, 'firm', NULL, 'a10'),
    (111, 'Longo', 'nota', 'long', '', 4, 'firm', NULL, 'a11'),
    (112, '<script>alert(1)</script>', '<b>tipo</b>', 'X<y>', '', 4, 'firm', NULL, 'a12'),
    (113, 'Memorando', 'nota', 'Sócios', '', 4, 'team', 13, 'a13');
INSERT INTO documents (id, title, doc_type, matter, full_text, uploaded_by,
                       visibility, team_id, content_sha256, created_at)
SELECT 1000 + i, 'Volume ' || i, 'volume', 'Volume', '', 4, 'firm', NULL, 'v' || i,
       TIMESTAMP '2026-01-01 09:00'
FROM generate_series(0, 119) AS i;
"""
FIRM = {101, 106, 107, 108, 109, 110, 111, 112, *VOLUME}


def _in_schema(statement: LiteralString) -> sql.Composed:
    return sql.SQL(statement).format(schema=sql.Identifier(SCHEMA))


STATEMENTS: list[str] = []


def _recording_cursor():
    import psycopg

    class Recording(psycopg.AsyncCursor):
        """Keeps every statement, so a test can say what the pages never read."""

        async def execute(self, query: Any, params: Any = None,  # type: ignore[override]
                          **kwargs: Any):
            STATEMENTS.append(query if isinstance(query, str) else repr(query))
            return await super().execute(cast(Any, query), params, **kwargs)
    return Recording


async def _connect():
    import psycopg

    assert URL
    conn = await psycopg.AsyncConnection.connect(URL, cursor_factory=_recording_cursor())
    async with conn.cursor() as cur:
        await cur.execute(_in_schema("SET search_path TO {schema}, public"))
    await conn.commit()
    return conn


class _Pool:
    """A connection per request: TestClient runs each one on its own loop."""

    def connection(self):
        import contextlib

        @contextlib.asynccontextmanager
        async def opened():
            conn = await _connect()
            try:
                yield conn
                await conn.commit()
            finally:
                await conn.close()
        return opened()


def _db(fn):
    async def main():
        conn = await _connect()
        try:
            return await fn(conn)
        finally:
            await conn.close()
    return asyncio.run(main())


async def _vector_in_public(conn) -> None:
    """The extension belongs to the database, not this run (see test_web_files)."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT pg_advisory_lock(hashtext('test-vector-extension'))")
        try:
            await cur.execute(
                "SELECT n.nspname FROM pg_extension e "
                "JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'vector'"
            )
            row = await cur.fetchone()
            if row is None:
                await cur.execute("CREATE EXTENSION vector SCHEMA public")
            elif row[0] != "public":
                await cur.execute("ALTER EXTENSION vector SET SCHEMA public")
            await conn.commit()
        finally:
            await cur.execute("SELECT pg_advisory_unlock(hashtext('test-vector-extension'))")
            await conn.commit()


async def _build(conn) -> None:
    await _vector_in_public(conn)
    async with conn.cursor() as cur:
        await cur.execute(_in_schema("DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await cur.execute(_in_schema("CREATE SCHEMA {schema}"))
        await cur.execute(_in_schema("SET search_path TO {schema}, public"))
        # The repository's own file, not input: safe to run as SQL.
        await cur.execute(cast(LiteralString,
                               Path("infra/init.sql").read_text(encoding="utf-8")))
    await conn.commit()
    for bootstrap in (ensure_access_schema, ensure_document_storage_schema,
                      ensure_team_schema, web_access.ensure_web_access_schema):
        await bootstrap(conn)
    async with conn.cursor() as cur:
        await cur.execute(_SEED)
        for document_id, name in ((110, SYMBOLS), (111, LONG)):
            await cur.execute("UPDATE documents SET matter = %s WHERE id = %s",
                              (name, document_id))
    await conn.commit()


@pytest.fixture
def archive(monkeypatch):
    import psycopg

    async def build():
        assert URL
        async with await psycopg.AsyncConnection.connect(URL) as conn:
            await _build(conn)

    async def drop():
        assert URL
        async with await psycopg.AsyncConnection.connect(URL) as conn:
            async with conn.cursor() as cur:
                await cur.execute(_in_schema("DROP SCHEMA IF EXISTS {schema} CASCADE"))
            await conn.commit()

    asyncio.run(build())

    async def get_pool():
        return _Pool()

    monkeypatch.setattr(settings, "web_enabled", True)
    monkeypatch.setattr(settings, "web_explorer", "on")
    monkeypatch.setattr(settings, "web_explorer_staff", "")
    monkeypatch.setattr(staff_web, "get_pool", get_pool)
    monkeypatch.setattr(files, "get_pool", get_pool)
    monkeypatch.setattr(explorer, "get_pool", get_pool)

    app = FastAPI()
    app.include_router(staff_web.router)
    app.include_router(files.router)
    app.include_router(explorer.router)
    client = TestClient(app, base_url="https://testserver", follow_redirects=False)
    try:
        yield client
    finally:
        client.close()
        asyncio.run(drop())


def _cookie(who: int) -> dict[str, str]:
    token = _db(lambda conn: web_access.open_session(conn, who, idle_seconds=3600))
    return {"cookie": f"{staff_web.COOKIE}={token}"}


def _as(archive, who: int, path: str):
    return archive.get(path, headers=_cookie(who))


def _sql(statement: LiteralString) -> None:
    async def run(conn):
        async with conn.cursor() as cur:
            await cur.execute(statement)
        await conn.commit()
    _db(run)


_TILE = re.compile(
    r"<a class='tile(?: none)?' href='([^']*)'><span class=kind>[^<]*</span>"
    r"<strong>([^<]*)</strong><span class=count>([0-9.]+) documentos?</span></a>"
)


def _tiles(page: str) -> dict[str, tuple[str, int]]:
    """Each tile's name, with where it leads and its count."""
    return {html.unescape(name): (html.unescape(href), int(count.replace(".", "")))
            for href, name, count in _TILE.findall(page)}


def _counts(page: str) -> dict[str, int]:
    return {name: count for name, (_href, count) in _tiles(page).items()}


def _cards(page: str) -> list[int]:
    return [int(n) for n in re.findall(r"class=title href='/web/explorar/documento/(\d+)'",
                                       page)]


def _all_pages(archive, who: int, path: str) -> list[int]:
    """Every card, following "Mais antigos" to the end."""
    seen: list[int] = []
    for _ in range(20):
        page = _as(archive, who, path).text
        seen += _cards(page)
        more = re.search(r"<a class=quiet href='([^']*)'>Mais antigos</a>", page)
        if not more:
            return seen
        path = html.unescape(more.group(1))
    raise AssertionError("the pages never ended")


# ── the folders each person has ─────────────────────────────────────────────


@pytest.mark.parametrize("who, expected", [
    (RUI, {"Minha pasta": 1, "Cível": 1, "Vazia": 0, "<Sócios & Cia>": 1,
           "Escritório": len(FIRM)}),
    (ANA, {"Minha pasta": 0, "Cível": 1, "Escritório": len(FIRM)}),
    (BIA, {"Minha pasta": 1, "Trabalhista": 1, "Escritório": len(FIRM)}),
    # The owner sees no private folder but his own, and no team he is not in.
    (PAULO, {"Minha pasta": 0, "Escritório": len(FIRM)}),
])
def test_each_person_sees_their_folders_and_only_their_counts(archive, who, expected):
    response = _as(archive, who, "/web/explorar/")

    assert response.status_code == 200
    assert _counts(response.text) == expected


def test_a_private_folder_is_only_ever_its_owners(archive):
    assert _counts(_as(archive, RUI, "/web/explorar/pessoal/").text) == {
        "Legislação": 0, "Acme": 1}
    assert _cards(_as(archive, RUI, "/web/explorar/pessoal/?cliente=Acme").text) == [102]

    owner = _as(archive, PAULO, "/web/explorar/pessoal/").text
    assert _counts(owner) == {"Legislação": 0}
    assert "Esta pasta está vazia" in owner
    assert _cards(_as(archive, PAULO, "/web/explorar/pessoal/?cliente=Acme").text) == []


@pytest.mark.parametrize("path", [
    "/web/explorar/equipe-10/",            # a team Bia is not in
    "/web/explorar/equipe-10/?cliente=Gama",
    "/web/explorar/equipe-999/",           # a team that does not exist
    "/web/explorar/arquivo/",              # not a folder at all
])
def test_a_folder_that_is_not_yours_is_unavailable(archive, path):
    response = _as(archive, BIA, path)

    assert response.status_code == 404
    assert "Pasta indisponível" in response.text
    assert "Petição Cível" not in response.text and _cards(response.text) == []


def test_a_team_folder_shows_its_clients_to_its_members(archive):
    assert _counts(_as(archive, ANA, "/web/explorar/equipe-10/").text) == {
        "Legislação": 0, "Gama": 1}
    assert _cards(_as(archive, ANA, "/web/explorar/equipe-10/?cliente=Gama").text) == [104]


def test_leaving_a_team_closes_its_folder_on_the_next_request(archive):
    """The same session throughout: what it may see is re-read every time."""
    cookie = _cookie(RUI)
    assert archive.get("/web/explorar/equipe-10/", headers=cookie).status_code == 200

    _sql("DELETE FROM team_members WHERE team_id = 10 AND staff_id = 4")

    assert archive.get("/web/explorar/equipe-10/", headers=cookie).status_code == 404
    assert "Cível" not in _counts(archive.get("/web/explorar/", headers=cookie).text)
    assert 104 not in _cards(archive.get("/web/explorar/?q=peticao", headers=cookie).text)


def test_a_deactivated_person_is_signed_out_on_the_next_request(archive):
    cookie = _cookie(RUI)
    assert archive.get("/web/explorar/", headers=cookie).status_code == 200

    _sql("UPDATE staff SET active = FALSE WHERE id = 4")

    assert archive.get("/web/explorar/", headers=cookie).status_code == 401
    assert archive.get("/web/explorar/escritorio/", headers=cookie).status_code == 401


def test_an_empty_team_folder_still_has_its_tile_and_says_it_is_empty(archive):
    page = _as(archive, RUI, "/web/explorar/equipe-12/").text

    assert "Esta pasta está vazia" in page


# ── clients inside a folder ─────────────────────────────────────────────────


def test_the_firms_clients_with_sem_cliente_last(archive):
    page = _as(archive, RUI, "/web/explorar/escritorio/").text
    counts = _counts(page)

    # The law (101) is in "Legislação", by its type, not in "Sem cliente".
    assert counts == {"Legislação": 1, "%": 1, SYMBOLS: 1, "Acme": 1, "Acme Holdings": 1,
                      LONG: 1, "Volume": 120, "X<y>": 1, "Sem cliente": 1}
    assert list(counts)[0] == "Legislação" and list(counts)[-1] == "Sem cliente"


@pytest.mark.parametrize("client, expected", [
    ("Acme", [107]),             # not Acme Holdings
    ("Acme Holdings", [106]),
    ("%", [108]),                # a literal %, not a wildcard
    (SYMBOLS, [110]),            # & + # ' and %20 survive the address
    (LONG, [111]),               # the whole stored name
    ("X<y>", [112]),
    ("Sem cliente", [109]),      # blanks, as NULL; the law 101 is in "Legislação"
])
def test_each_client_tile_leads_to_exactly_its_documents(archive, client, expected):
    tiles = _tiles(_as(archive, RUI, "/web/explorar/escritorio/").text)
    href, count = tiles[client]

    cards = _cards(_as(archive, RUI, href).text)

    assert sorted(cards) == sorted(expected)
    assert len(cards) == count


def test_a_client_no_longer_here_says_so(archive):
    page = _as(archive, RUI, "/web/explorar/escritorio/?cliente=Inexistente").text

    assert _cards(page) == []
    assert "Nenhum documento de “Inexistente” aqui" in page


def test_an_empty_client_is_the_folder_itself(archive):
    page = _as(archive, RUI, "/web/explorar/escritorio/?cliente=").text

    assert "Volume" in _counts(page)


def test_a_folder_of_120_is_reachable_whole_in_a_stable_order(archive):
    first = _as(archive, RUI, "/web/explorar/escritorio/?cliente=Volume").text

    assert len(_cards(first)) == explorer.PER_PAGE
    assert _all_pages(archive, RUI, "/web/explorar/escritorio/?cliente=Volume") == VOLUME


def test_a_page_past_the_end_is_empty_and_says_so(archive):
    page = _as(archive, RUI, "/web/explorar/escritorio/?cliente=Volume&pagina=9").text

    assert _cards(page) == []
    assert "Nenhum documento" in page


# ── search ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("who, text, expected", [
    (RUI, "acme", {102, 106, 107}),   # titles and clients
    (BIA, "acme", {106, 107}),        # never Rui's private contract
    (RUI, "PETICAO", {104}),          # type, folding case and accents
    (BIA, "peticao", {105}),
    (RUI, "%", {108, 110}),           # a literal % (110's client has one), not everything
    (RUI, "_", set()),                # a literal _, not any character
])
def test_search_reaches_every_folder_the_person_sees_and_no_other(archive, who, text,
                                                                   expected):
    page = _as(archive, who, f"/web/explorar/?q={text.replace('%', '%25')}").text

    assert set(_cards(page)) == expected


def test_a_result_in_a_team_folder_names_and_links_the_team(archive):
    page = _as(archive, RUI, "/web/explorar/?q=Petição").text

    assert "<a class=where href='/web/explorar/equipe-10/'>Equipe Cível</a>" in page


def test_a_result_says_whose_document_it_is(archive):
    page = _as(archive, RUI, "/web/explorar/?q=Petição").text

    assert "<span class=meta>Gama · peticao · " in page


def test_search_results_page_too(archive):
    assert _all_pages(archive, RUI, "/web/explorar/?q=volume") == VOLUME


def test_nothing_found_says_what_the_search_looks_at(archive):
    page = _as(archive, RUI, "/web/explorar/?q=inexistente").text

    assert "Nada encontrado" in page and "o título, o cliente ou caso e o tipo" in page


# ── output ──────────────────────────────────────────────────────────────────


def test_every_value_is_escaped(archive):
    home = _as(archive, RUI, "/web/explorar/").text
    team = _as(archive, RUI, "/web/explorar/equipe-13/").text
    folder = _as(archive, RUI, "/web/explorar/escritorio/").text
    listing = _as(archive, RUI, "/web/explorar/escritorio/?cliente=X%3Cy%3E").text
    found = _as(archive, RUI, "/web/explorar/?q=%3Cscript").text
    in_team = _as(archive, RUI, "/web/explorar/?q=memorando").text
    _sql("UPDATE documents SET full_text = '<img src=x onerror=alert(2)> texto' WHERE id = 112")
    document = _as(archive, RUI, "/web/explorar/documento/112").text
    moving = _as(archive, RUI, "/web/explorar/documento/112/mover").text

    for page in (home, team, folder, listing, found, in_team, document, moving):
        for raw in ("<y>", "<script>", "<b>", "<Sócios"):
            assert raw not in page
    assert "&lt;Sócios &amp; Cia&gt;" in home and "&lt;Sócios &amp; Cia&gt;" in team
    assert ("<a class=where href='/web/explorar/equipe-13/'>Equipe &lt;Sócios &amp; Cia&gt;"
            "</a>") in in_team
    assert "X&lt;y&gt;" in folder
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in listing
    assert "&lt;b&gt;tipo&lt;/b&gt;" in listing
    assert "&lt;script" in found and "&lt;b&gt;tipo&lt;/b&gt;" in found
    assert "<h1>&lt;script&gt;alert(1)&lt;/script&gt;</h1>" in document
    assert "<img" not in document and "&lt;img src=x onerror=alert(2)&gt; texto" in document
    assert "&lt;b&gt;tipo&lt;/b&gt;" in document and "X&lt;y&gt;" in document
    assert "“&lt;script&gt;alert(1)&lt;/script&gt;”" in moving


def test_the_breadcrumb_says_where_the_person_is(archive):
    page = _as(archive, RUI, "/web/explorar/escritorio/?cliente=Acme").text

    assert ("<div class=crumbs><a href='/web/explorar/'>Arquivos</a>"
            "<span aria-hidden=true>›</span><a href='/web/explorar/escritorio/'>Escritório</a>"
            "<span aria-hidden=true>›</span><span aria-current=page>Acme</span></div>") in page


# ── the switch ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/web/explorar/", "/web/explorar/escritorio/",
                                  "/web/explorar/?q=acme"])
def test_switched_off_the_explorer_does_not_exist(archive, monkeypatch, path):
    monkeypatch.setattr(settings, "web_explorer", "off")

    response = _as(archive, RUI, path)

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_with_the_web_off_the_explorer_does_not_exist(archive, monkeypatch):
    monkeypatch.setattr(settings, "web_enabled", False)

    assert archive.get("/web/explorar/").status_code == 404


def test_in_the_pilot_only_the_listed_people_have_it(archive, monkeypatch):
    monkeypatch.setattr(settings, "web_explorer", "pilot")
    monkeypatch.setattr(settings, "web_explorer_staff", " 4, 9")

    assert _as(archive, RUI, "/web/explorar/").status_code == 200
    assert _as(archive, BIA, "/web/explorar/").status_code == 404
    assert _as(archive, BIA, "/web/explorar/escritorio/").status_code == 404


def test_the_pilot_list_is_matched_by_whole_ids(archive, monkeypatch):
    monkeypatch.setattr(settings, "web_explorer", "pilot")
    monkeypatch.setattr(settings, "web_explorer_staff", "14,40")

    assert _as(archive, RUI, "/web/explorar/").status_code == 404


def test_signed_out_is_asked_to_sign_in(archive):
    response = archive.get("/web/explorar/")

    assert response.status_code == 401
    assert "Você não está conectado" in response.text


@pytest.mark.parametrize("switch, staff_list, who, lands", [
    ("on", "", RUI, "/web/explorar/"),
    ("pilot", "4", RUI, "/web/explorar/"),
    ("pilot", "4", BIA, "/web/arquivos/"),
    ("off", "4", RUI, "/web/arquivos/"),
])
def test_arquivos_leads_to_the_explorer_for_whoever_has_it(archive, monkeypatch, switch,
                                                           staff_list, who, lands):
    monkeypatch.setattr(settings, "web_explorer", switch)
    monkeypatch.setattr(settings, "web_explorer_staff", staff_list)

    page = _as(archive, who, "/web/arquivos/").text
    home = _as(archive, who, "/web/")

    assert f"<a href='{lands}'>Arquivos</a>" in page
    assert home.status_code == 303 and home.headers["location"] == lands, (
        "without the assistant, /web/ opens the files")


def test_a_card_opens_the_explorers_document_page(archive):
    page = _as(archive, RUI, "/web/explorar/escritorio/?cliente=Acme").text

    assert "href='/web/explorar/documento/107'" in page
    assert _as(archive, RUI, "/web/explorar/documento/107").status_code == 200


def test_no_page_reads_a_documents_full_text(archive):
    """The archive is a source, not a workbench: listing never reads the text."""
    STATEMENTS.clear()
    for path in ("/web/explorar/", "/web/explorar/escritorio/",
                 "/web/explorar/escritorio/?cliente=Volume", "/web/explorar/?q=acme",
                 "/web/explorar/escritorio/?sem_cliente=1"):
        assert _as(archive, RUI, path).status_code == 200

    reads = [s for s in STATEMENTS if "documents d" in s]
    assert len(reads) >= 5
    assert not [s for s in reads if "full_text" in s]


def test_switched_off_no_connection_is_taken(archive, monkeypatch):
    async def no_pool():
        raise AssertionError("a switched-off page took a connection")

    monkeypatch.setattr(settings, "web_explorer", "off")
    monkeypatch.setattr(explorer, "get_pool", no_pool)

    assert archive.get("/web/explorar/").status_code == 404
    assert archive.get("/web/explorar/escritorio/?cliente=Acme").status_code == 404
    assert archive.get("/web/explorar/documento/107").status_code == 404
    assert _post(archive, RUI, "/web/explorar/documento/102/mover",
                 {"destino": "escritorio"}).status_code == 404


# ── one document ────────────────────────────────────────────────────────────


def _audit(action: str) -> list[tuple[int, list[int], str | None]]:
    async def rows(conn):
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT staff_id, document_ids, query FROM document_access_log "
                "WHERE action = %s ORDER BY id", (action,))
            return [(r[0], list(r[1] or []), r[2]) for r in await cur.fetchall()]
    return _db(rows)


def _where(document_id: int) -> tuple[str, int | None]:
    async def row(conn):
        async with conn.cursor() as cur:
            await cur.execute("SELECT visibility, team_id FROM documents WHERE id = %s",
                              (document_id,))
            found = await cur.fetchone()
        assert found
        return found[0], found[1]
    return _db(row)


def test_the_document_page_shows_the_start_of_the_text_and_records_it(archive):
    _sql("UPDATE documents SET full_text = repeat('a', 1000) || 'FIM' WHERE id = 107")

    page = _as(archive, RUI, "/web/explorar/documento/107").text

    assert "<h1>Acordo de confidencialidade</h1>" in page
    assert "a" * explorer.PREVIEW_CHARS + "…" in page
    assert "a" * (explorer.PREVIEW_CHARS + 1) not in page and "FIM" not in page
    assert _audit("view_text") == [(RUI, [107], "web explorador")]


def test_the_preview_is_cut_by_the_database(archive):
    STATEMENTS.clear()
    assert _as(archive, RUI, "/web/explorar/documento/107").status_code == 200

    reads = [s for s in STATEMENTS if "full_text" in s]
    assert reads
    for statement in reads:
        rest = (statement.replace("substring(d.full_text FROM 1 FOR %s)", "")
                .replace("char_length(d.full_text)", ""))
        assert "full_text" not in rest


def test_a_document_without_text_says_so(archive):
    page = _as(archive, RUI, "/web/explorar/documento/107").text

    assert "não tem texto extraído" in page


def test_the_document_page_says_where_it_is(archive):
    page = _as(archive, ANA, "/web/explorar/documento/104").text

    assert "<a href='/web/explorar/equipe-10/'>Equipe Cível</a>" in page
    assert "<a href='/web/explorar/equipe-10/?cliente=Gama'>Gama</a>" in page


@pytest.mark.parametrize("who, raw", [
    (BIA, "102"),      # Rui's private folder
    (PAULO, "102"),    # not even the owner's
    (BIA, "104"),      # a team she is not in
    (RUI, "99999"),
    (RUI, "abc"),
    (RUI, "１２"),      # full-width digits are not an id
])
def test_a_document_not_yours_is_not_found_and_nothing_is_recorded(archive, who, raw):
    response = _as(archive, who, f"/web/explorar/documento/{raw}")

    assert response.status_code == 404
    assert "Documento não encontrado" in response.text
    assert "Contrato Acme" not in response.text and "Petição" not in response.text
    assert _audit("view_text") == []


def test_moving_is_offered_only_to_who_may_move_it(archive):
    assert "/web/explorar/documento/107/mover" in _as(archive, RUI, "/web/explorar/documento/107").text
    # An intern, a document somebody else sent.
    assert "/mover" not in _as(archive, ANA, "/web/explorar/documento/107").text
    # An intern, her own.
    assert "/web/explorar/documento/104/mover" in _as(archive, ANA, "/web/explorar/documento/104").text


def test_the_original_is_offered_to_lawyers_only(archive):
    _sql("UPDATE documents SET source_storage_key = 'k', source_filename = 'p.pdf', "
         "source_size_bytes = 10 WHERE id = 104")

    assert "/web/arquivos/104/original" in _as(archive, RUI, "/web/explorar/documento/104").text
    assert "/original" not in _as(archive, ANA, "/web/explorar/documento/104").text


# ── moving ──────────────────────────────────────────────────────────────────


def _offered(page: str) -> dict[str, bool]:
    """Each destination offered, and whether it warns the document will vanish."""
    found = re.findall(r"<input type=radio name=destino value='([^']*)' required>"
                       r"<span>[^<]*(<small class=bad>)?", page)
    return {value: bool(warn) for value, warn in found}


def test_the_move_page_offers_only_allowed_folders(archive):
    page = _as(archive, RUI, "/web/explorar/documento/102/mover").text

    # His own document, in his own folder: his teams and the firm. Not
    # Trabalhista, which he is not in, and not where it already is.
    assert _offered(page) == {"equipe-10": False, "equipe-12": False, "equipe-13": False,
                              "escritorio": False}


def test_an_owner_is_warned_where_the_document_would_vanish(archive):
    page = _as(archive, PAULO, "/web/explorar/documento/101/mover").text

    assert _offered(page) == {"pessoal": False, "equipe-10": True, "equipe-11": True,
                              "equipe-12": True, "equipe-13": True}
    assert "Você deixará de ver este documento." in page


def test_the_move_page_refuses_who_may_not_move_it(archive):
    response = _as(archive, ANA, "/web/explorar/documento/107/mover")

    assert response.status_code == 403
    assert "Este documento está na pasta do escritório" in response.text


def _post(archive, who: int | None, path: str, data: dict[str, str], *,
          site: str = "same-origin", csrf: str | None = None):
    headers = {"sec-fetch-site": site}
    form = dict(data)
    if who is not None:
        token = _db(lambda conn: web_access.open_session(conn, who, idle_seconds=3600))
        headers["cookie"] = f"{staff_web.COOKIE}={token}"
        form["csrf"] = staff_web.csrf_for(token) if csrf is None else csrf
    return archive.post(path, data=form, headers=headers)


def test_a_move_happens_is_recorded_and_lands_on_the_document(archive):
    response = _post(archive, RUI, "/web/explorar/documento/102/mover",
                     {"destino": "escritorio"})

    assert response.status_code == 303
    assert response.headers["location"] == "/web/explorar/documento/102?movido=1"
    assert _where(102) == ("firm", None)
    assert _audit("share") == [(RUI, [102], "escritório")]
    shown = _as(archive, RUI, response.headers["location"]).text
    assert "Documento movido para a pasta Escritório." in shown


def test_a_move_out_of_sight_lands_where_the_document_was(archive):
    response = _post(archive, PAULO, "/web/explorar/documento/101/mover",
                     {"destino": "equipe-10"})

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/web/explorar/escritorio/?sem_cliente=1&movido=1")
    assert _where(101) == ("team", 10)
    shown = _as(archive, PAULO, response.headers["location"]).text
    assert "por isso não aparece mais aqui" in shown and 101 not in _cards(shown)


def test_a_move_out_of_sight_keeps_the_client_in_the_address(archive):
    _sql("UPDATE staff SET role = 'admin' WHERE id = 4")

    response = _post(archive, RUI, "/web/explorar/documento/110/mover",
                     {"destino": "equipe-11"})

    assert response.headers["location"] == (
        "/web/explorar/escritorio/?cliente=A%20%26%20B%20%2B%20C%20%231%20%27x%27%20%2520"
        "&movido=1")


def test_the_move_is_decided_again_when_it_is_sent(archive):
    """The page offered Cível; he left Cível before pressing Mover."""
    assert "equipe-10" in _offered(_as(archive, RUI, "/web/explorar/documento/102/mover").text)
    _sql("DELETE FROM team_members WHERE team_id = 10 AND staff_id = 4")

    response = _post(archive, RUI, "/web/explorar/documento/102/mover",
                     {"destino": "equipe-10"})

    assert response.status_code == 403
    assert "Você não está na equipe Cível" in response.text
    assert _where(102) == ("private", None)
    assert _audit("share") == []


@pytest.mark.parametrize("who, document_id, choice, status", [
    (BIA, 102, "escritorio", 404),       # a document she cannot see
    (RUI, 102, "equipe-11", 403),        # a team he is not in
    (RUI, 102, "equipe-999", 403),       # no such team
    (RUI, 102, "qualquer", 403),
    (ANA, 107, "equipe-10", 403),        # an intern, somebody else's
])
def test_refused_moves_change_nothing(archive, who, document_id, choice, status):
    before = _where(document_id)

    response = _post(archive, who, f"/web/explorar/documento/{document_id}/mover",
                     {"destino": choice})

    assert response.status_code == status
    assert _where(document_id) == before
    assert _audit("share") == []


@pytest.mark.parametrize("kwargs", [
    {"csrf": ""},
    {"csrf": "0" * 40},
    {"csrf": "é"},              # not ASCII: refused, not a crash
    {"site": "cross-site"},
])
def test_a_move_needs_the_form_token_and_this_site(archive, kwargs):
    response = _post(archive, RUI, "/web/explorar/documento/102/mover",
                     {"destino": "escritorio"}, **kwargs)

    assert response.status_code == 403
    assert _where(102) == ("private", None)


def test_a_move_without_a_session_changes_nothing(archive):
    response = _post(archive, None, "/web/explorar/documento/102/mover",
                     {"destino": "escritorio", "csrf": "x"})

    assert response.status_code == 403
    assert _where(102) == ("private", None)


def test_switched_off_a_move_does_not_exist(archive, monkeypatch):
    monkeypatch.setattr(settings, "web_explorer", "off")

    response = _post(archive, RUI, "/web/explorar/documento/102/mover",
                     {"destino": "escritorio"})

    assert response.status_code == 404
    assert _where(102) == ("private", None)


def test_outside_the_pilot_a_move_does_not_exist(archive, monkeypatch):
    monkeypatch.setattr(settings, "web_explorer", "pilot")
    monkeypatch.setattr(settings, "web_explorer_staff", "1")

    response = _post(archive, RUI, "/web/explorar/documento/102/mover",
                     {"destino": "escritorio"})

    assert response.status_code == 404
    assert _where(102) == ("private", None)


# ── the script and the policy that allows it ────────────────────────────────


EXPLORER_PAGES = ["/web/explorar/", "/web/explorar/escritorio/",
                  "/web/explorar/escritorio/?cliente=Acme", "/web/explorar/?q=acme",
                  "/web/explorar/documento/107", "/web/explorar/documento/107/mover"]


@pytest.mark.parametrize("path", EXPLORER_PAGES)
def test_explorer_pages_allow_a_script_from_this_host_only(archive, path):
    response = _as(archive, RUI, path)
    policy = response.headers["content-security-policy"]

    assert response.status_code == 200
    assert "default-src 'none'" in policy and "frame-ancestors 'none'" in policy
    assert "script-src 'self'" in policy and "connect-src 'self'" in policy
    assert "unsafe-eval" not in policy and "script-src 'self';" in policy + ";"
    # The script by address, and nothing inline.
    assert f"<script type=module src='{explorer.SCRIPT}'></script>" in response.text
    assert re.findall(r"<script(?![^>]*\bsrc=)", response.text) == []


@pytest.mark.parametrize("path", ["/web/arquivos/", "/web/arquivos/107"])
def test_every_other_page_still_allows_no_script(archive, path):
    from core.web import pages

    before = dict(pages.HEADERS)
    _as(archive, RUI, "/web/explorar/")
    response = _as(archive, RUI, path)

    assert pages.HEADERS == before, "the shared policy was changed in place"
    assert "script-src" not in response.headers["content-security-policy"]
    assert "connect-src" not in response.headers["content-security-policy"]
    assert "<script" not in response.text


def test_the_script_is_served_as_javascript(archive):
    response = archive.get(explorer.SCRIPT)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "openDrawer" in response.text


def test_switched_off_the_script_does_not_exist(archive, monkeypatch):
    monkeypatch.setattr(settings, "web_explorer", "off")

    assert archive.get(explorer.SCRIPT).status_code == 404


def test_the_script_writes_no_markup_of_its_own():
    """What the server sends goes in as parsed nodes; what the script writes
    is text. None of the ways a string becomes markup or code appear."""
    source = (Path("core/web/static/explorer.js")).read_text(encoding="utf-8")

    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                      "eval(", "new Function", "setTimeout(\"", "http://", "https://"):
        assert forbidden not in source, forbidden


def test_cards_say_which_document_and_folder_they_are(archive):
    found = _as(archive, RUI, "/web/explorar/?q=Petição").text

    assert "<article class=card data-id=104 data-folder=equipe-10>" in found


def test_the_drop_bar_offers_only_the_persons_own_folders(archive):
    def targets(page: str) -> list[str]:
        return re.findall(r"<div class=target data-slug=([a-z0-9-]+)>", page)

    assert targets(_as(archive, BIA, "/web/explorar/escritorio/?cliente=Acme").text) == [
        "pessoal", "equipe-11", "escritorio"]
    assert targets(_as(archive, RUI, "/web/explorar/?q=acme").text) == [
        "pessoal", "equipe-10", "equipe-13", "equipe-12", "escritorio"]
    assert "<div id=drop class=drop hidden>" in _as(archive, RUI, "/web/explorar/").text


def test_the_parts_the_script_swaps_are_marked(archive):
    assert "<div id=results><div class=tiles>" in _as(archive, RUI, "/web/explorar/").text
    assert "<div id=results><div class=cards>" in _as(archive, RUI, "/web/explorar/?q=acme").text
    document = _as(archive, RUI, "/web/explorar/documento/107").text
    assert "<article id=document><h1>Acordo de confidencialidade</h1>" in document


def test_a_drag_never_takes_a_document_out_of_the_movers_sight(archive):
    """Rui, made an admin, may move into a team he is not in, but only on the
    page that warns him. A drag (visivel=1), say onto a team he left after
    the page loaded, is refused and changes nothing."""
    _sql("UPDATE staff SET role = 'admin' WHERE id = 4")
    _sql("DELETE FROM team_members WHERE team_id = 10 AND staff_id = 4")

    dragged = _post(archive, RUI, "/web/explorar/documento/107/mover",
                    {"destino": "equipe-10", "visivel": "1"})

    assert dragged.status_code == 403
    assert "Você não está mais nesta equipe" in dragged.text
    assert _where(107) == ("firm", None)
    assert _audit("share") == []

    warned = _post(archive, RUI, "/web/explorar/documento/107/mover", {"destino": "equipe-10"})
    assert warned.status_code == 303
    assert _where(107) == ("team", 10)


def test_a_drag_into_the_movers_own_folder_goes_through(archive):
    dragged = _post(archive, RUI, "/web/explorar/documento/107/mover",
                    {"destino": "equipe-10", "visivel": "1"})

    assert dragged.status_code == 303
    assert _where(107) == ("team", 10)


# ── editing how a document is described ─────────────────────────────────────


def _described(document_id: int) -> tuple[str, str | None, str | None]:
    async def row(conn):
        async with conn.cursor() as cur:
            await cur.execute("SELECT title, doc_type, matter FROM documents WHERE id = %s",
                              (document_id,))
            found = await cur.fetchone()
        assert found
        return found[0], found[1], found[2]
    return _db(row)


def _sphere(document_id: int) -> str | None:
    async def row(conn):
        async with conn.cursor() as cur:
            await cur.execute("SELECT sphere FROM documents WHERE id = %s", (document_id,))
            found = await cur.fetchone()
        assert found
        return found[0]
    return _db(row)


def _opened(document_id: int) -> dict[str, str]:
    """What the edit page shows for each field, as it shows it."""
    title, kind, client = _described(document_id)
    shown = {"titulo": title, "tipo": kind or "",
             "cliente": client if client and client.strip() else "",
             "esfera": _sphere(document_id) or ""}
    return {**shown, **{f"antes_{name}": value for name, value in shown.items()}}


def _edit(archive, who: int, document_id: int, *, opened: dict[str, str] | None = None,
          **fields: str):
    """The form as the page sends it: every field, the untouched ones as they
    were, and what each held when the page opened (now, unless given)."""
    form = dict(opened or _opened(document_id))
    form.update(fields)
    return _post(archive, who, f"/web/explorar/documento/{document_id}/editar", form)


def test_editing_is_offered_to_lawyers(archive):
    assert "/web/explorar/documento/107/editar'>Editar</a>" in _as(
        archive, RUI, "/web/explorar/documento/107").text
    assert "/editar'" not in _as(archive, ANA, "/web/explorar/documento/104").text


def test_the_edit_page_shows_what_is_there_and_suggests_what_exists(archive):
    page = _as(archive, RUI, "/web/explorar/documento/107/editar").text

    assert "id=titulo name=titulo required autocomplete=off value='Acordo de confidencialidade'" in page
    assert "value='contrato'" in page and "value='Acme'>" in page
    assert "<option value='Acme Holdings'></option>" in page
    assert "<option value='X&lt;y&gt;'></option>" in page           # a client, escaped
    assert "<option value='&lt;b&gt;tipo&lt;/b&gt;'></option>" in page  # a type, escaped
    assert "<y>" not in page and "<b>tipo" not in page
    # The browser would count a maxlength in UTF-16 units and cut a name the
    # column holds; the server's limits are the ones that apply.
    assert "maxlength" not in page
    assert "type=checkbox" not in page


def test_a_document_without_a_client_shows_an_empty_field_not_a_ticked_box(archive):
    page = _as(archive, RUI, "/web/explorar/documento/101/editar").text

    assert "placeholder='Sem cliente' value=''>" in page


def test_an_intern_is_refused_the_edit_page(archive):
    assert _as(archive, ANA, "/web/explorar/documento/104/editar").status_code == 403


def test_a_document_without_a_client_is_filed_under_one(archive):
    response = _edit(archive, RUI, 109, cliente="Beta Ltda")

    assert response.status_code == 303
    assert response.headers["location"] == "/web/explorar/documento/109?atualizado=1"
    assert _described(109) == ("Sem nome", "nota", "Beta Ltda")
    # Only what changed is written and recorded.
    assert _audit("edit_document") == [(RUI, [109], "matter=Beta Ltda")]
    assert "Documento atualizado." in _as(archive, RUI, response.headers["location"]).text
    assert _where(109) == ("firm", None), "the folder must not change"


def test_name_type_and_client_change_together(archive):
    _edit(archive, RUI, 107, titulo="  Acordo   de sigilo ", tipo="nda", cliente="Beta Ltda")

    assert _described(107) == ("Acordo de sigilo", "nda", "Beta Ltda")
    assert _audit("edit_document") == [
        (RUI, [107], "title=Acordo de sigilo; doc_type=nda; matter=Beta Ltda")]


def test_emptying_the_type_or_the_client_leaves_none(archive):
    _edit(archive, RUI, 107, tipo="", cliente="")

    assert _described(107) == ("Acordo de confidencialidade", None, None)
    assert _audit("edit_document") == [
        (RUI, [107], "doc_type=(sem tipo); matter=(sem cliente)")]
    assert 107 in _cards(_as(archive, RUI, "/web/explorar/escritorio/?sem_cliente=1").text)


def test_nothing_changed_writes_nothing(archive):
    STATEMENTS.clear()
    response = _edit(archive, RUI, 107)

    assert response.status_code == 303
    assert response.headers["location"] == "/web/explorar/documento/107"
    assert _audit("edit_document") == []
    assert [s for s in STATEMENTS if "UPDATE documents" in s] == []


def test_the_page_carries_what_each_field_held_when_it_opened(archive):
    page = _as(archive, RUI, "/web/explorar/documento/107/editar").text

    assert "<input type=hidden name=antes_titulo value='Acordo de confidencialidade'>" in page
    assert "<input type=hidden name=antes_tipo value='contrato'>" in page
    assert "<input type=hidden name=antes_cliente value='Acme'>" in page


def test_a_field_left_alone_is_neither_written_nor_tidied(archive):
    _sql("UPDATE documents SET title = 'Acordo  de   sigilo' WHERE id = 107")

    _edit(archive, RUI, 107, cliente="Beta")

    assert _described(107) == ("Acordo  de   sigilo", "contrato", "Beta")
    assert _audit("edit_document") == [(RUI, [107], "matter=Beta")]


def test_fixing_the_spacing_of_a_name_on_purpose_is_a_change(archive):
    _sql("UPDATE documents SET title = 'Acordo  de   sigilo' WHERE id = 107")

    _edit(archive, RUI, 107, titulo="Acordo de sigilo")

    assert _described(107)[0] == "Acordo de sigilo"


def test_a_colleagues_edit_since_the_page_opened_is_not_undone(archive):
    """Rui opened the page; Lia renamed the document; Rui changes the client."""
    opened = _opened(107)
    _sql("UPDATE documents SET title = 'Nome da Lia' WHERE id = 107")

    response = _edit(archive, RUI, 107, opened=opened, cliente="Beta")

    assert response.status_code == 303
    assert _described(107) == ("Nome da Lia", "contrato", "Beta")


def test_a_field_two_people_changed_keeps_the_first_and_tells_the_second(archive):
    opened = _opened(107)
    _sql("UPDATE documents SET matter = 'Escolha da Lia' WHERE id = 107")

    response = _edit(archive, RUI, 107, opened=opened, cliente="Escolha do Rui")

    assert response.status_code == 409
    assert "Não foi salvo" in response.text and "Outra pessoa alterou" in response.text
    assert _described(107)[2] == "Escolha da Lia"
    assert _audit("edit_document") == []


def test_a_form_without_what_it_opened_with_writes_nothing(archive):
    response = _post(archive, RUI, "/web/explorar/documento/107/editar",
                     {"titulo": "Outro", "tipo": "", "cliente": ""})

    assert response.status_code == 409
    assert "Esta página está desatualizada" in response.text
    assert _described(107) == ("Acordo de confidencialidade", "contrato", "Acme")


def test_a_suggestion_is_kept_exactly_and_a_new_name_is_tidied(archive):
    _sql("UPDATE documents SET matter = 'Acme  Ltda', doc_type = 'nota  interna' WHERE id = 106")

    _edit(archive, RUI, 107, cliente="Acme  Ltda", tipo="nota  interna")
    assert _described(107)[1:] == ("nota  interna", "Acme  Ltda")

    _edit(archive, RUI, 107, cliente=" Nova   Firma ", tipo=" novo   tipo ")
    assert _described(107)[1:] == ("novo tipo", "Nova Firma")


@pytest.mark.parametrize("fields, says", [
    ({"titulo": "   "}, "precisa de um nome"),
    ({"titulo": "x" * 501}, "O nome tem 501 caracteres"),
    ({"tipo": "x" * 51}, "O tipo tem 51 caracteres"),
    ({"cliente": "x" * 256}, "O cliente ou caso tem 256 caracteres"),
])
def test_refused_edits_change_nothing(archive, fields, says):
    before = _described(107)

    response = _edit(archive, RUI, 107, **fields)

    assert response.status_code == 403
    assert says in response.text
    assert _described(107) == before
    assert _audit("edit_document") == []


@pytest.mark.parametrize("who, document_id, status", [
    (ANA, 104, 403),     # an intern
    (BIA, 102, 404),     # a document she cannot see
])
def test_who_may_not_edit_changes_nothing(archive, who, document_id, status):
    before = _described(document_id)

    response = _edit(archive, who, document_id, titulo="Outro", cliente="Outro")

    assert response.status_code == status
    assert _described(document_id) == before


@pytest.mark.parametrize("letter", ["á", "€", "𝄞"])
def test_the_longest_names_fit_in_any_alphabet(archive, letter):
    """Encoded, these are kilobytes: the form still arrives whole, token included."""
    response = _edit(archive, RUI, 107, titulo=letter * 500, tipo=letter * 50,
                     cliente=letter * 255)

    assert response.status_code == 303
    assert _described(107) == (letter * 500, letter * 50, letter * 255)


@pytest.mark.parametrize("kwargs", [{"csrf": ""}, {"csrf": "é"}, {"site": "cross-site"}])
def test_an_edit_needs_the_form_token_and_this_site(archive, kwargs):
    response = _post(archive, RUI, "/web/explorar/documento/107/editar",
                     {**_opened(107), "titulo": "Outro"}, **kwargs)

    assert response.status_code == 403
    assert _described(107) == ("Acordo de confidencialidade", "contrato", "Acme")


def test_switched_off_an_edit_does_not_exist(archive, monkeypatch):
    monkeypatch.setattr(settings, "web_explorer", "off")

    assert _edit(archive, RUI, 107, cliente="Outro").status_code == 404
    assert archive.get("/web/explorar/documento/107/editar").status_code == 404
    assert _described(107)[2] == "Acme"


# ── clients shown with a capital, stored as typed ───────────────────────────


def test_a_client_typed_in_lowercase_is_shown_with_a_capital_and_linked_as_stored(archive):
    _sql("UPDATE documents SET matter = 'beta comércio' WHERE id = 109")

    folder = _as(archive, RUI, "/web/explorar/escritorio/").text
    href, count = _tiles(folder)["Beta comércio"]
    listing = _as(archive, RUI, href).text
    document = _as(archive, RUI, "/web/explorar/documento/109").text
    found = _as(archive, RUI, "/web/explorar/?q=sem%20nome").text

    assert href == "/web/explorar/escritorio/?cliente=beta%20com%C3%A9rcio"
    assert count == 1 and _cards(listing) == [109]
    assert "<h1>Beta comércio</h1>" in listing
    assert "<dd>Beta comércio</dd>" in document
    assert "<span class=meta>Beta comércio · nota · " in found
    assert _described(109)[2] == "beta comércio", "the stored name is untouched"


def test_a_document_already_at_its_longest_can_still_be_edited(archive):
    """The form carries every field twice, as typed and as it opened: at the
    longest the columns hold, in four-byte characters, it still arrives whole."""
    _sql("UPDATE documents SET title = repeat('𝄞', 500), doc_type = repeat('𝄞', 50), "
         "matter = repeat('𝄞', 255) WHERE id = 107")

    response = _edit(archive, RUI, 107, cliente="🎼" * 255)

    assert response.status_code == 303
    assert _described(107) == ("𝄞" * 500, "𝄞" * 50, "🎼" * 255)


# ── legislation: common use, filed by kind, never under a client ────────────


# What each type must be, written out, so the three folds are checked against
# the answer and not only against each other.
_CLASSIFIED = [
    ("lei", "leis"), (" LEI  Complementar ", "leis"), ("lei-complementar", "leis"),
    ("lei\u00a0ordinária", "leis"), ("lei\tordinária", "leis"),
    ("le\u1ab0i", "leis"),                      # an uncommon combining mark
    ("leilão", None), ("leis", None),
    ("Constituição", "constituicao"), ("CONSTITUIÇÃO ESTADUAL", "constituicao"),
    ("Co\u0301digo Civil", "codigos"),           # the accent decomposed, as a .docx may give
    ("Código Civil", "codigos"), ("Decreto-Lei", "decretos"), ("decretos", None),
    ("Medida  Provisoria", "decretos"), ("Súmula Vinculante", "sumulas"),
    ("contrato", None), ("", None),
    ("\u2003lei", None),            # a space outside the folded ones is kept, in all three
]


def test_python_and_the_database_file_each_type_where_it_belongs(archive):
    from core.legislation import group_of, group_sql

    async def classify(conn):
        found = []
        async with conn.cursor() as cur:
            for sample, _expected in _CLASSIFIED + [(None, None)]:
                await cur.execute(f"SELECT {group_sql('x.t')} FROM (SELECT %s::text AS t) x",
                                  [sample])
                row = await cur.fetchone()
                assert row is not None
                found.append(row[0])
        return found

    expected = [group for _sample, group in _CLASSIFIED] + [None]
    assert [group_of(sample) for sample, _ in _CLASSIFIED] + [group_of(None)] == expected
    assert _db(classify) == expected


def test_the_page_script_folds_types_as_python_does():
    """explorer.js's foldType, run in Node, against core.legislation.fold."""
    import shutil
    import subprocess

    from core.legislation import PREFIXES, fold

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = Path("core/web/static/explorer.js").read_text(encoding="utf-8")
    found = re.search(r"^function foldType\(text\) \{.*?^\}", source, re.S | re.M)
    assert found, "foldType is gone from explorer.js"
    samples = [sample for sample, _group in _CLASSIFIED]
    script = (found.group(0) + "\nconst starts = " + json.dumps(list(PREFIXES)) +
              ";\nconst samples = " + json.dumps(samples) + ";\n"
              "console.log(JSON.stringify(samples.map((t) => { const f = foldType(t); "
              "return [f, starts.some((s) => f === s || f.startsWith(`${s} `) "
              "|| f.startsWith(`${s}-`))]; })));")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True,
                            check=True, timeout=30)
    folded = json.loads(result.stdout)

    assert [f for f, _law in folded] == [fold(sample) for sample in samples]
    assert [law for _f, law in folded] == [group is not None for _s, group in _CLASSIFIED]


def test_every_folder_has_legislation_first_counted_by_type(archive):
    _sql("UPDATE documents SET doc_type = 'Súmula Vinculante', matter = NULL WHERE id = 109")

    counts = _counts(_as(archive, RUI, "/web/explorar/escritorio/").text)

    assert list(counts)[0] == "Legislação" and counts["Legislação"] == 2
    assert "Sem cliente" not in counts  # 109 was its only document


def test_legislation_is_never_under_a_client_whatever_its_matter_says(archive):
    _sql("UPDATE documents SET matter = 'legislação federal' WHERE id = 101")

    counts = _counts(_as(archive, RUI, "/web/explorar/escritorio/").text)
    listing = _as(archive, RUI, "/web/explorar/escritorio/?cliente=legisla%C3%A7%C3%A3o%20federal")

    assert counts["Legislação"] == 1 and "Legislação federal" not in counts
    assert _cards(listing.text) == []


def test_legislation_has_one_tile_per_kind_even_empty(archive):
    page = _as(archive, RUI, "/web/explorar/escritorio/?legislacao=1").text

    assert _counts(page) == {"Constituição": 0, "Leis": 1, "Códigos": 0,
                             "Decretos e MPs": 0, "Súmulas": 0}
    assert "<span aria-current=page>Legislação</span>" in page
    assert _tiles(page)["Leis"][0] == "/web/explorar/escritorio/?legislacao=leis"


def test_a_kind_lists_its_documents_with_their_sphere(archive):
    _sql("UPDATE documents SET sphere = 'federal' WHERE id = 101")

    page = _as(archive, RUI, "/web/explorar/escritorio/?legislacao=leis").text

    assert _cards(page) == [101]
    assert "<span class=meta>lei · federal · " in page
    assert ("<a href='/web/explorar/escritorio/?legislacao=1'>Legislação</a>"
            "<span aria-hidden=true>›</span><span aria-current=page>Leis</span>") in page


def test_an_empty_kind_says_how_a_document_gets_there(archive):
    page = _as(archive, RUI, "/web/explorar/escritorio/?legislacao=sumulas").text

    assert _cards(page) == [] and "Um documento entra aqui pelo tipo" in page


def test_an_unknown_kind_shows_the_kinds(archive):
    page = _as(archive, RUI, "/web/explorar/escritorio/?legislacao=%3Cscript%3E").text

    assert "Decretos e MPs" in page and "<script>" not in page.replace(
        f"<script type=module src='{explorer.SCRIPT}'></script>", "")


def test_a_laws_page_shows_its_sphere_and_where_it_is_filed(archive):
    _sql("UPDATE documents SET sphere = 'estadual' WHERE id = 101")

    page = _as(archive, RUI, "/web/explorar/documento/101").text

    assert "<dt>Esfera</dt><dd>Estadual</dd>" in page and "Cliente ou caso" not in page
    assert "<a href='/web/explorar/escritorio/?legislacao=leis'>Leis</a>" in page


def test_the_edit_page_knows_which_types_are_legislation(archive):
    from core.legislation import PREFIXES

    page = _as(archive, RUI, "/web/explorar/documento/107/editar").text

    assert f"data-legislacao='{html.escape(json.dumps(list(PREFIXES)))}'" in page
    assert "<div data-campo=cliente>" in page and "<div data-campo=esfera>" in page
    assert "<option value='federal'>Federal</option>" in page
    # The usual kinds come first among the type suggestions.
    assert page.index("<option value='constituição'>") < page.index("<option value='contrato'>")


def test_retyping_a_document_as_law_drops_its_client_and_takes_a_sphere(archive):
    response = _edit(archive, RUI, 107, tipo="lei", esfera="federal")

    assert response.status_code == 303
    assert _described(107) == ("Acordo de confidencialidade", "lei", None)
    assert _sphere(107) == "federal"
    assert _audit("edit_document") == [
        (RUI, [107], "doc_type=lei; sphere=federal; matter=(sem cliente)")]
    assert 107 in _cards(_as(archive, RUI, "/web/explorar/escritorio/?legislacao=leis").text)


def test_a_client_sent_for_a_law_is_ignored(archive):
    """Without the script both fields show; the rule is the server's."""
    _edit(archive, RUI, 101, cliente="Acme", esfera="municipal")

    assert _described(101)[2] is None and _sphere(101) == "municipal"
    assert _audit("edit_document") == [(RUI, [101], "sphere=municipal")]


def test_renaming_a_law_touches_nothing_else(archive):
    _sql("UPDATE documents SET matter = 'legislação federal' WHERE id = 101")

    _edit(archive, RUI, 101, titulo="Lei 8.245/1991")

    assert _described(101) == ("Lei 8.245/1991", "lei", "legislação federal")
    assert _audit("edit_document") == [(RUI, [101], "title=Lei 8.245/1991")]


def test_retyping_a_law_as_something_else_drops_its_sphere(archive):
    _sql("UPDATE documents SET sphere = 'federal' WHERE id = 101")

    _edit(archive, RUI, 101, tipo="parecer", cliente="Acme", esfera="federal")

    assert _described(101) == ("Lei do Inquilinato", "parecer", "Acme")
    assert _sphere(101) is None


def test_a_sphere_that_is_not_one_of_the_three_is_refused(archive):
    response = _edit(archive, RUI, 101, esfera="internacional")

    assert response.status_code == 403
    assert "federal, estadual ou municipal" in response.text
    assert _sphere(101) is None


def test_the_sphere_reaches_a_database_created_before_it(archive):
    """init.sql runs only on an empty database: the column must come from the
    startup path too (lessons learned, 17)."""
    from core.repositories.documents import ensure_document_storage_schema

    async def rebuilt(conn):
        async with conn.cursor() as cur:
            await cur.execute("ALTER TABLE documents DROP COLUMN sphere")
        await conn.commit()
        await ensure_document_storage_schema(conn)
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM information_schema.columns WHERE table_schema = %s "
                "AND table_name = 'documents' AND column_name = 'sphere'", (SCHEMA,))
            row = await cur.fetchone()
        assert row is not None
        return row[0]

    assert _db(rebuilt) == 1


def test_a_type_that_stays_legislation_keeps_its_client(archive):
    """"lei" to "lei complementar" crosses no line: nothing else is touched."""
    _sql("UPDATE documents SET matter = 'legislação federal' WHERE id = 101")

    _edit(archive, RUI, 101, tipo="lei complementar")

    assert _described(101) == ("Lei do Inquilinato", "lei complementar", "legislação federal")
    assert _audit("edit_document") == [(RUI, [101], "doc_type=lei complementar")]


def test_a_type_that_stays_ordinary_keeps_its_sphere(archive):
    _sql("UPDATE documents SET sphere = 'federal' WHERE id = 107")

    _edit(archive, RUI, 107, tipo="parecer")

    assert _described(107)[1] == "parecer" and _sphere(107) == "federal"


def test_a_client_set_while_a_colleague_made_it_a_law_is_not_saved(archive):
    opened = _opened(109)                               # a note, no client
    _sql("UPDATE documents SET doc_type = 'lei' WHERE id = 109")

    response = _edit(archive, RUI, 109, opened=opened, cliente="Beta")

    assert response.status_code == 409
    assert _described(109) == ("Sem nome", "lei", "  ")


def test_a_sphere_set_while_a_colleague_made_it_a_contract_is_not_saved(archive):
    opened = _opened(101)                               # a law, no sphere
    _sql("UPDATE documents SET doc_type = 'contrato' WHERE id = 101")

    response = _edit(archive, RUI, 101, opened=opened, esfera="federal")

    assert response.status_code == 409
    assert _sphere(101) is None


def test_a_space_the_page_does_not_fold_does_not_make_a_law_on_saving(archive):
    """The script decides on the typed text; saving tidies it first. With one
    spacing rule for both, "lei<em space>ordinária" is no law in either."""
    _edit(archive, RUI, 107, tipo="lei\u2003ordinária")

    assert _described(107) == ("Acordo de confidencialidade", "lei\u2003ordinária", "Acme")


def test_a_client_a_colleague_set_meanwhile_does_not_stay_on_a_law(archive):
    opened = _opened(109)                               # a note, no client
    _sql("UPDATE documents SET matter = 'Cliente da Lia' WHERE id = 109")

    response = _edit(archive, RUI, 109, opened=opened, tipo="lei")

    assert response.status_code == 409
    assert _described(109) == ("Sem nome", "nota", "Cliente da Lia")


def test_a_sphere_a_colleague_set_meanwhile_does_not_stay_on_a_contract(archive):
    opened = _opened(101)                               # a law, no sphere
    _sql("UPDATE documents SET sphere = 'federal' WHERE id = 101")

    response = _edit(archive, RUI, 101, opened=opened, tipo="contrato")

    assert response.status_code == 409
    assert _described(101)[1] == "lei" and _sphere(101) == "federal"


@pytest.mark.parametrize('mode,pilot,who,expected', [
    ('off', '1', PAULO, False), ('pilot', '1', PAULO, True),
    ('pilot', '1', RUI, False), ('on', '', RUI, True),
])
def test_explorer_app_lock_meta_only_for_the_pilot(archive, monkeypatch,
                                                 mode, pilot, who, expected):
    monkeypatch.setattr(settings, 'web_app', mode)
    monkeypatch.setattr(settings, 'web_app_staff', pilot)
    response = _as(archive, who, '/web/explorar/')
    assert response.status_code == 200
    assert ('<meta name=app-lock content=1>' in response.text) is expected
