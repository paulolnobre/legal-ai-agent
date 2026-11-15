"""The archive in a browser shows what the bot shows, and nothing more.

Built on the real schema, init.sql plus the startup bootstraps, because the
property under test is a query: which rows `visibility_clause` lets through
for this reader. A double would have to re-implement that clause, and then it
would be the double being tested.

The cast: Rui and Ana share the team Cível, Bia is in another team, Paulo is
the owner. The owner is here on purpose, to show the page gives the owner no
view into a private folder: the one door through that wall is /assumir in the
bot, one named document at a time, and the person is told.

Skipped without TEST_DATABASE_URL; CI provides one.
"""
import asyncio
import os
import uuid
from urllib.parse import quote
from pathlib import Path
from typing import LiteralString, cast

import pytest
from psycopg import sql
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.config import settings
from core.repositories import web_access
from core.repositories.documents import ensure_document_storage_schema
from core.repositories.staff import ensure_access_schema
from core.repositories.teams import ensure_team_schema
from core.web import files, staff_web

URL = os.environ.get("TEST_DATABASE_URL")
# Unique per test process. Two runs against one database, a second terminal
# or another agent, would otherwise drop and rebuild each other's schema
# mid-test: a login test then met a secret it never enrolled.
SCHEMA = f"webfiles_{uuid.uuid4().hex[:12]}"


def _in_schema(statement: LiteralString) -> sql.Composed:
    """A statement naming this run's schema, quoted as an identifier."""
    return sql.SQL(statement).format(schema=sql.Identifier(SCHEMA))

pytestmark = pytest.mark.skipif(
    not URL, reason="needs a real Postgres: set TEST_DATABASE_URL",
)

PAULO, RUI, ANA, BIA = 1, 4, 5, 6
LAW, ACME, BETA, PETITION, OTHER_TEAM = 101, 102, 103, 104, 105

_SEED = """
INSERT INTO staff (id, chat_id, name, role) VALUES
    (1, '100', 'Paulo', 'owner'), (4, '400', 'Rui', 'advogado'),
    (5, '500', 'Ana', 'estagiario'), (6, '600', 'Bia', 'advogado');
INSERT INTO teams (id, name) VALUES (10, 'Cível'), (11, 'Trabalhista');
INSERT INTO team_members (team_id, staff_id) VALUES (10, 4), (10, 5), (11, 6);
INSERT INTO documents (id, title, doc_type, matter, full_text, uploaded_by,
                       visibility, team_id, source_filename, source_storage_key,
                       source_sha256, source_size_bytes, content_sha256) VALUES
    (101, 'Lei do Inquilinato', 'lei', NULL, 'Art. 1 A locação...', 1,
     'firm', NULL, NULL, NULL, NULL, NULL, 'a1'),
    (102, 'Contrato Acme', 'contrato', 'Acme', 'Cláusula primeira', 4,
     'private', NULL, 'Contrato Acme "final".pdf', 'k/acme', 'sha-acme', 2048, 'a2'),
    (103, 'Parecer Beta', 'parecer', 'Beta', 'Segredo da Bia', 6,
     'private', NULL, 'beta.pdf', 'k/beta', 'sha-beta', 1024, 'a3'),
    (104, 'Petição Cível', 'peticao', 'Gama', 'Excelentíssimo', 5,
     'team', 10, 'peticao.docx', 'k/peticao', 'sha-pet', 4096, 'a4'),
    (105, 'Reclamação', 'peticao', 'Delta', 'Da outra equipe', 6,
     'team', 11, 'rec.pdf', 'k/rec', 'sha-rec', 512, 'a5');
"""


async def _connect():
    import psycopg

    assert URL
    conn = await psycopg.AsyncConnection.connect(URL)
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
    """The extension is the database's, not this run's.

    init.sql creates it wherever the search path points first, which here is
    this run's schema. A second run would then find the extension present but
    its type invisible from its own schema, and dropping the first schema
    would take the extension with it. So it is put in public first, once,
    under a lock in case two runs start together; init.sql then finds it.
    """
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
    """This run's schema from the real init.sql and bootstraps, seeded."""
    await _vector_in_public(conn)
    async with conn.cursor() as cur:
        await cur.execute(_in_schema("DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await cur.execute(_in_schema("CREATE SCHEMA {schema}"))
        await cur.execute(_in_schema("SET search_path TO {schema}, public"))
        # The repository's own file, not input: safe to run as SQL.
        schema_sql = cast(LiteralString,
                          Path("infra/init.sql").read_text(encoding="utf-8"))
        await cur.execute(schema_sql)
    await conn.commit()
    for bootstrap in (ensure_access_schema, ensure_document_storage_schema,
                      ensure_team_schema, web_access.ensure_web_access_schema):
        await bootstrap(conn)
    async with conn.cursor() as cur:
        await cur.execute(_SEED)
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
    reads = []
    originals = {"k/acme": b"%PDF acme", "k/beta": b"%PDF beta",
                 "k/peticao": b"PK docx", "k/rec": b"%PDF rec"}

    async def get_pool():
        return _Pool()

    async def read(key, wrapped_key=None, *, raw_sha256=""):
        reads.append(key)
        return originals.get(key)

    monkeypatch.setattr(settings, "web_enabled", True)
    monkeypatch.setattr(staff_web, "get_pool", get_pool)
    monkeypatch.setattr(files, "get_pool", get_pool)
    monkeypatch.setattr(files.document_storage, "read", read)

    app = FastAPI()
    app.include_router(staff_web.router)
    app.include_router(files.router)
    client = TestClient(app, base_url="https://testserver", follow_redirects=False)
    client.reads = reads  # type: ignore[attr-defined]
    try:
        yield client
    finally:
        client.close()
        asyncio.run(drop())


def _as(archive, who: int, path: str):
    token = _db(lambda conn: web_access.open_session(conn, who, idle_seconds=3600))
    return archive.get(path, headers={"cookie": f"{staff_web.COOKIE}={token}"})


def _audit():
    async def rows(conn):
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT staff_id, action, query, document_ids FROM document_access_log "
                "ORDER BY id"
            )
            return [(r[0], r[1], r[2], list(r[3] or [])) for r in await cur.fetchall()]
    return _db(rows)


def _listed(page: str) -> set[int]:
    import re

    return {int(n) for n in re.findall(r"href='/web/arquivos/(\d+)'", page)}


# ── who sees what ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("who, expected", [
    (RUI, {LAW, ACME, PETITION}),
    (ANA, {LAW, PETITION}),
    (BIA, {LAW, BETA, OTHER_TEAM}),
    # The owner sees no private folder but their own, and no team they are not
    # in. Reaching one is /assumir, named, audited and announced.
    (PAULO, {LAW}),
])
def test_the_list_is_exactly_what_the_scope_allows(archive, who, expected):
    response = _as(archive, who, "/web/arquivos/")

    assert response.status_code == 200
    assert _listed(response.text) == expected


@pytest.mark.parametrize("folder, expected", [
    ("minha", {ACME}), ("equipe", {PETITION}), ("escritorio", {LAW}),
    ("qualquer-coisa", {LAW, ACME, PETITION}),
])
def test_the_folder_filter_narrows(archive, folder, expected):
    page = _as(archive, RUI, f"/web/arquivos/?pasta={folder}").text

    assert _listed(page) == expected


def test_the_folder_filter_never_widens(archive):
    """Asking for team documents gives your teams', not every team's."""
    page = _as(archive, BIA, "/web/arquivos/?pasta=equipe").text

    assert _listed(page) == {OTHER_TEAM}


def test_the_title_filter_folds_accents_and_case(archive):
    page = _as(archive, RUI, "/web/arquivos/?q=PETICAO").text

    assert _listed(page) == {PETITION}


def test_a_title_cannot_inject_markup(archive):
    async def rename(conn):
        async with conn.cursor() as cur:
            await cur.execute("UPDATE documents SET title = '<script>x()</script>' "
                              "WHERE id = 101")
        await conn.commit()
    _db(rename)

    page = _as(archive, RUI, "/web/arquivos/").text

    assert "<script>x()" not in page
    assert "&lt;script&gt;" in page


# ── one document ─────────────────────────────────────────────────────────────


def test_somebody_elses_private_document_reads_as_missing(archive):
    """Same answer as an id that does not exist, so ids cannot be probed."""
    token = _db(lambda conn: web_access.open_session(conn, RUI, idle_seconds=3600))
    cookie = {"cookie": f"{staff_web.COOKIE}={token}"}
    hidden = archive.get(f"/web/arquivos/{BETA}", headers=cookie)
    missing = archive.get("/web/arquivos/99999", headers=cookie)

    assert hidden.status_code == missing.status_code == 404
    assert "Parecer Beta" not in hidden.text
    # The theme link echoes the requested address back (volta=...). Each echo
    # must be exactly the address that was asked for, and only that echo is
    # set aside before comparing, so anything else the server added still
    # shows up as a difference.
    hidden_echo = f"volta={quote(f'/web/arquivos/{BETA}', safe='')}'"
    missing_echo = f"volta={quote('/web/arquivos/99999', safe='')}'"
    assert hidden_echo in hidden.text and missing_echo in missing.text

    assert hidden.text.replace(hidden_echo, "") == missing.text.replace(missing_echo, "")


def test_a_non_numeric_id_is_just_not_found(archive):
    assert _as(archive, RUI, "/web/arquivos/abc").status_code == 404


def test_the_details_name_the_folder_and_team(archive):
    page = _as(archive, ANA, f"/web/arquivos/{PETITION}").text

    assert "Petição Cível" in page and "Cível" in page and "Equipe" in page


def test_an_intern_is_not_offered_the_original(archive):
    page = _as(archive, ANA, f"/web/arquivos/{PETITION}").text

    assert "/original" not in page
    assert "/texto" in page


def test_a_lawyer_is_offered_the_original(archive):
    page = _as(archive, RUI, f"/web/arquivos/{ACME}").text

    assert f"/web/arquivos/{ACME}/original" in page


# ── reading the text ─────────────────────────────────────────────────────────


def test_reading_the_text_is_written_down(archive):
    page = _as(archive, RUI, f"/web/arquivos/{ACME}/texto")

    assert "Cláusula primeira" in page.text
    assert _audit() == [(RUI, "view_text", "web", [ACME])]


def test_text_out_of_scope_is_not_shown_nor_logged(archive):
    page = _as(archive, RUI, f"/web/arquivos/{BETA}/texto")

    assert page.status_code == 404
    assert "Segredo da Bia" not in page.text
    assert _audit() == []


def test_a_long_text_is_shown_in_part(archive, monkeypatch):
    monkeypatch.setattr(files, "TEXT_LIMIT", 5)

    page = _as(archive, RUI, f"/web/arquivos/{ACME}/texto").text

    assert "Cláus" in page and "primeira" not in page
    assert "Mostrando o começo" in page


# ── the original ─────────────────────────────────────────────────────────────


def test_a_lawyer_downloads_the_original_as_an_attachment(archive):
    response = _as(archive, RUI, f"/web/arquivos/{ACME}/original")

    assert response.status_code == 200
    assert response.content == b"%PDF acme"
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "no-store" in response.headers["cache-control"]


def test_taking_the_original_is_written_down_twice(archive):
    _as(archive, RUI, f"/web/arquivos/{ACME}/original")

    assert _audit() == [
        (RUI, "decrypt_original", "web", [ACME]),
        (RUI, "download_original", "web", [ACME]),
    ]


def test_an_intern_cannot_take_the_original(archive):
    response = _as(archive, ANA, f"/web/arquivos/{PETITION}/original")

    assert response.status_code == 403
    assert archive.reads == [], "the original was decrypted for someone refused"
    assert _audit() == []


def test_an_original_out_of_scope_is_not_even_read(archive):
    response = _as(archive, RUI, f"/web/arquivos/{BETA}/original")

    assert response.status_code == 404
    assert archive.reads == []
    assert _audit() == []


def test_the_owner_cannot_download_from_a_private_folder(archive):
    response = _as(archive, PAULO, f"/web/arquivos/{ACME}/original")

    assert response.status_code == 404
    assert archive.reads == []


def test_a_missing_original_is_said_and_not_logged_as_taken(archive):
    async def lose(conn):
        async with conn.cursor() as cur:
            await cur.execute("UPDATE documents SET source_storage_key = 'k/lost' "
                              "WHERE id = 102")
        await conn.commit()
    _db(lose)

    response = _as(archive, RUI, f"/web/arquivos/{ACME}/original")

    assert response.status_code == 404
    assert _audit() == []


# ── the session ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", [
    "/web/arquivos/", f"/web/arquivos/{LAW}", f"/web/arquivos/{LAW}/texto",
    f"/web/arquivos/{ACME}/original",
])
def test_nothing_without_a_session(archive, path):
    assert archive.get(path).status_code == 401
    assert archive.reads == []


@pytest.mark.parametrize("path", [
    "/web/arquivos/", f"/web/arquivos/{LAW}", f"/web/arquivos/{ACME}/original",
])
def test_nothing_while_the_pages_are_off(archive, monkeypatch, path):
    token = _db(lambda conn: web_access.open_session(conn, RUI, idle_seconds=3600))
    monkeypatch.setattr(settings, "web_enabled", False)

    response = archive.get(path, headers={"cookie": f"{staff_web.COOKIE}={token}"})

    assert response.status_code == 404


def test_a_deactivated_person_sees_nothing(archive):
    token = _db(lambda conn: web_access.open_session(conn, RUI, idle_seconds=3600))

    async def deactivate(conn):
        async with conn.cursor() as cur:
            await cur.execute("UPDATE staff SET active = FALSE WHERE id = 4")
        await conn.commit()
    _db(deactivate)

    response = archive.get("/web/arquivos/",
                           headers={"cookie": f"{staff_web.COOKIE}={token}"})

    assert response.status_code == 401


# ── the header a file name goes into ─────────────────────────────────────────


@pytest.mark.parametrize("name", [
    'Contrato "final".pdf', "a\r\nSet-Cookie: x=1.pdf", "x; filename=evil.html",
])
def test_a_file_name_cannot_bend_the_header(name):
    value = files.attachment_disposition(name)
    plain = value.split('filename="', 1)[1].split('"', 1)[0]

    assert "\r" not in value and "\n" not in value
    assert '"' not in plain and ";" not in plain


def test_the_real_name_survives_with_its_accents():
    value = files.attachment_disposition("Petição.pdf")

    assert "filename*=UTF-8''Peti%C3%A7%C3%A3o.pdf" in value


def test_an_unknown_folder_is_no_filter_at_all(archive):
    """Read as no filter, so the page does not claim to have left any out."""
    page = _as(archive, RUI, "/web/arquivos/?pasta=qualquer-coisa").text

    assert "3 documentos." in page and " de 3 " not in page



# ── found in review ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["²", "①", "١٢", "9999999999", "-1", "1e3"])
def test_odd_ids_are_not_found_not_an_error(archive, raw):
    """`str.isdigit` accepts ² and ①, which `int` then refuses: a 500."""
    assert _as(archive, RUI, f"/web/arquivos/{raw}").status_code == 404


def test_a_move_during_the_request_does_not_leak_the_new_holder(archive, monkeypatch):
    """The page used to read the folder and uploader after the scoped read,
    without the scope. A document moved into Bia's private folder in between
    showed Bia's name to Rui, who could no longer see it."""
    import psycopg

    real = files.document_card

    async def then_moved(conn, document_id, scope):
        card = await real(conn, document_id, scope)
        assert URL
        async with await psycopg.AsyncConnection.connect(URL) as other:
            await other.execute(_in_schema("SET search_path TO {schema}, public"))
            await other.execute("UPDATE documents SET uploaded_by = 6, "
                                "visibility = 'private' WHERE id = 102")
            await other.commit()
        return card

    monkeypatch.setattr(files, "document_card", then_moved)
    page = _as(archive, RUI, f"/web/arquivos/{ACME}").text

    assert "Bia" not in page


def test_the_text_is_cut_by_postgres_not_after_it_arrives(archive, monkeypatch):
    """The cost rule: a long text must not cross into this process whole. A
    page that fetched it all and cut it in Python would look the same, so
    what is checked is what the database handed back."""
    import psycopg

    async def lengthen(conn):
        async with conn.cursor() as cur:
            await cur.execute("UPDATE documents SET full_text = repeat('x', 50000) "
                              "WHERE id = 102")
        await conn.commit()
    _db(lengthen)

    fetched: list = []
    for name in ("fetchone", "fetchall"):
        real = getattr(psycopg.AsyncCursor, name)

        def spy(real=real):
            async def wrapped(self, *args, **kwargs):
                result = await real(self, *args, **kwargs)
                fetched.append(result)
                return result
            return wrapped
        monkeypatch.setattr(psycopg.AsyncCursor, name, spy())
    monkeypatch.setattr(files, "TEXT_LIMIT", 100)

    page = _as(archive, RUI, f"/web/arquivos/{ACME}/texto").text

    longest = max(
        (len(value) for result in fetched if result
         for row in (result if isinstance(result, list) else [result])
         for value in row if isinstance(value, str)),
        default=0,
    )
    assert longest <= 100, f"Postgres handed back {longest} characters"
    assert "Mostrando o começo" in page


def test_the_build_leaves_the_vector_extension_in_public():
    """Run on a database made for this test, which has never seen the
    extension. On the shared test database an earlier run has usually put it
    in public already, and a check there would pass whatever the build did.
    Here, a build that skipped `_vector_in_public` lets init.sql create it in
    this run's schema, and the check fails."""
    import psycopg
    from psycopg.conninfo import make_conninfo

    assert URL
    name = f"vectortest_{uuid.uuid4().hex[:12]}"

    async def where(conn):
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT n.nspname FROM pg_extension e "
                "JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'vector'"
            )
            row = await cur.fetchone()
        return row[0] if row else None

    async def main():
        assert URL
        async with await psycopg.AsyncConnection.connect(URL, autocommit=True) as admin:
            await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            async with await psycopg.AsyncConnection.connect(
                    make_conninfo(URL, dbname=name)) as conn:
                assert await where(conn) is None, "a fresh database already had it"
                await _build(conn)
                return await where(conn)
        finally:
            async with await psycopg.AsyncConnection.connect(URL, autocommit=True) as admin:
                await admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
                                    .format(sql.Identifier(name)))

    assert asyncio.run(main()) == "public"
