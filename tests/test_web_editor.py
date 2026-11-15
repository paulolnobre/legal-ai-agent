"""The editor, against a real Postgres.

A generated file is opened, adjusted in the page and downloaded again, and
nothing of it is kept. What these tests hold fast: only the file's owner,
before it expires, on every route; nothing stored; every value escaped; a
cell typed as "=1+1" is text in the file; and the limits are refused before
anything is built.
"""
import asyncio
import contextlib
import io
import re
import zipfile
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from core import document_crypto
from core.config import settings
from core.repositories import panel, web_access
from core.services import editing, exporting
from core.web import editor, files, staff_web

PAULO, RUI, ANA = 1, 4, 5
DOCX = exporting.build_docx("Parecer", "## Tese\n**negrito** aqui\n- item")
XLSX = exporting.build_xlsx_rows("Prazos", [["a", "b"], ["c", "d"]])


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
    monkeypatch.setattr(settings, "doc_master_key", document_crypto.new_master_key())
    monkeypatch.setattr(staff_web, "get_pool", get_pool)
    monkeypatch.setattr(editor, "get_pool", get_pool)

    app = FastAPI()
    app.include_router(staff_web.router)
    app.include_router(editor.router)
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


def _session(site, who: int) -> str:
    return site.run(lambda conn: web_access.open_session(conn, who, idle_seconds=3600))


def _get(site, who: int, path: str):
    return site.client.get(path, headers={"cookie": f"{staff_web.COOKIE}={_session(site, who)}"})


def _post(site, who: int | None, path: str, data: dict[str, str], *,
          site_header: str = "same-origin", csrf: str | None = None):
    headers = {"sec-fetch-site": site_header}
    form = dict(data)
    if who is not None:
        token = _session(site, who)
        headers["cookie"] = f"{staff_web.COOKIE}={token}"
        form["csrf"] = staff_web.csrf_for(token) if csrf is None else csrf
    return site.client.post(path, data=form, headers=headers)


def _rows(site, statement: str, params=()):
    async def query(conn):
        async with conn.cursor() as cur:
            await cur.execute(statement, params)
            rows = await cur.fetchall()
        await conn.commit()
        return rows
    return site.run(query)


def _store(site, who: int, data: bytes = DOCX, name: str = "Parecer_20261111.docx") -> str:
    token = site.run(lambda conn: panel.store_file(
        conn, staff_id=who, filename=name, media_type="application/octet-stream", data=data))
    assert token
    return token


def _expire(site) -> None:
    _rows(site, "UPDATE panel_files SET expires_at = NOW() - interval '1 second' RETURNING 1")


def _audit(site):
    return _rows(site, "SELECT staff_id, action FROM document_access_log ORDER BY id")


TEXT_FORM = {"titulo": "Parecer", "texto": "## Tese\nTexto", "formato": "docx"}


def test_open_shows_the_text_and_logs_edit_export(site):
    token = _store(site, RUI)

    response = _get(site, RUI, f"/web/editor/{token}")

    assert response.status_code == 200
    page = response.text
    assert "value='Parecer'" in page
    assert re.search(r"<textarea[^>]*>[^<]*## Tese[^<]*\*\*negrito\*\* aqui", page)
    csp = response.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "connect-src 'self'" in csp
    assert response.headers["cache-control"].startswith("no-store")
    assert _audit(site) == [(RUI, "edit_export")]
    assert "data-dirty" not in page


def test_open_a_sheet_shows_a_grid(site):
    token = _store(site, RUI, XLSX, "Prazos_20261111.xlsx")

    page = _get(site, RUI, f"/web/editor/{token}").text

    assert re.search(r"name=c0_0[^>]*value='a'", page)
    assert re.search(r"name=c1_1[^>]*value='d'", page)
    assert re.search(r"name=linhas[^>]*value=2", page) and re.search(r"name=colunas[^>]*value=2", page)


@pytest.mark.parametrize("route", ["get", "ver", "baixar"])
def test_other_people_expired_and_unknown_tokens_are_not_found(site, route):
    token = _store(site, RUI)
    path = f"/web/editor/{{}}{'' if route == 'get' else '/' + route}"

    def ask(who: int, token_: str):
        if route == "get":
            return _get(site, who, path.format(token_))
        return _post(site, who, path.format(token_), TEXT_FORM)

    assert ask(ANA, token).status_code == 404
    assert ask(PAULO, token).status_code == 404, "the firm's owner is not the file's owner"
    assert ask(RUI, "nao-existe").status_code == 404
    assert _audit(site) == []
    _expire(site)
    assert ask(RUI, token).status_code == 404
    assert _audit(site) == []


def test_a_page_open_before_expiry_stops_working_after_it(site):
    token = _store(site, RUI)
    assert _get(site, RUI, f"/web/editor/{token}").status_code == 200

    _expire(site)

    assert _post(site, RUI, f"/web/editor/{token}/ver", TEXT_FORM).status_code == 404
    assert _post(site, RUI, f"/web/editor/{token}/baixar", TEXT_FORM).status_code == 404


def test_a_file_that_is_not_ours_cannot_be_edited(site):
    pdf = _store(site, RUI, b"%PDF-1.7 x", "a.pdf")
    junk = _store(site, RUI, b"not a docx", "b.docx")

    for token in (pdf, junk):
        response = _get(site, RUI, f"/web/editor/{token}")
        assert response.status_code == 422
        assert "não pode ser editado" in response.text
    assert _post(site, RUI, f"/web/editor/{pdf}/baixar", TEXT_FORM).status_code == 404
    # Only the junk .docx was opened (decrypted), so only that was logged.
    assert _audit(site) == [(RUI, "edit_export")]


def test_preview_is_escaped_and_nothing_is_stored(site):
    token = _store(site, RUI)
    before = _rows(site, "SELECT token, sealed, size_bytes, expires_at FROM panel_files")

    response = _post(site, RUI, f"/web/editor/{token}/ver", {
        "titulo": "<script>x</script>", "texto": "# H\n**b** <img src=x onerror=1>",
        "formato": "docx"})

    assert response.status_code == 200
    page = response.text
    assert "<script>x" not in page and "<img" not in page
    assert "&lt;script&gt;x" in page and "&lt;img src=x" in page
    assert "<h2>H</h2>" in page and "<strong>b</strong>" in page
    assert "data-dirty=1" in page, "the text is back in the box, still unsaved"
    assert _rows(site, "SELECT token, sealed, size_bytes, expires_at FROM panel_files") == before
    assert _audit(site) == []


def test_download_docx_and_pdf_are_named_for_the_format_and_logged(site):
    token = _store(site, RUI)
    form = {"titulo": "Novo título", "texto": "## A\n**b** c\n- c", "formato": "docx"}

    as_docx = _post(site, RUI, f"/web/editor/{token}/baixar", form)
    as_pdf = _post(site, RUI, f"/web/editor/{token}/baixar", {**form, "formato": "pdf"})

    assert as_docx.status_code == 200 and as_pdf.status_code == 200
    assert as_docx.headers["content-disposition"] == files.attachment_disposition(
        "Parecer_20261111.docx")
    assert as_pdf.headers["content-disposition"] == files.attachment_disposition(
        "Parecer_20261111.pdf")
    read = editing.read_docx(as_docx.content)
    assert (read.title, read.text) == ("Novo título", "## A\n**b** c\n- c")
    assert as_pdf.content.startswith(b"%PDF")
    assert _audit(site) == [(RUI, "download_export")] * 2
    assert _post(site, RUI, f"/web/editor/{token}/baixar",
                 {**form, "formato": "xlsx"}).status_code == 404


def test_download_sheet_keeps_every_cell_as_text(site):
    token = _store(site, RUI, XLSX, "Prazos_20261111.xlsx")
    cells = {"c0_0": "=1+1", "c0_1": "@x", "c0_2": "+1", "c1_0": "-1", "c1_1": "a|b", "c1_2": ""}

    response = _post(site, RUI, f"/web/editor/{token}/baixar", {
        "titulo": "Prazos", "linhas": "2", "colunas": "3", "formato": "xlsx", **cells})

    assert response.status_code == 200
    assert "Prazos_20261111.xlsx" in response.headers["content-disposition"]
    sheet = load_workbook(io.BytesIO(response.content)).worksheets[0]
    got = [[c.value or "" for c in row] for row in sheet.iter_rows(min_row=3, max_row=4)]
    assert got == [["=1+1", "@x", "+1"], ["-1", "a|b", ""]]
    xml = zipfile.ZipFile(io.BytesIO(response.content)).read("xl/worksheets/sheet1.xml")
    assert b"<f>" not in xml
    assert _post(site, RUI, f"/web/editor/{token}/baixar",
                 {"titulo": "x", "linhas": "1", "colunas": "1", "formato": "docx"}
                 ).status_code == 404


def test_the_original_name_is_a_safe_header(site):
    token = _store(site, RUI, DOCX, 'Parecer "ç" ; x.docx')

    response = _post(site, RUI, f"/web/editor/{token}/baixar", {**TEXT_FORM, "formato": "pdf"})

    assert response.headers["content-disposition"] == files.attachment_disposition(
        'Parecer "ç" ; x.pdf')
    plain = response.headers["content-disposition"].split("filename=")[1].split(";")[0]
    assert '"' not in plain.strip('"') and ";" not in plain


@pytest.mark.parametrize("case", ["text", "title", "rows", "cols", "cell"])
def test_limits_are_refused_before_building(site, monkeypatch, case):
    def never(*args, **kwargs):
        raise AssertionError("built an over-limit document")

    async def never_async(*args, **kwargs):
        never()

    monkeypatch.setattr(exporting, "build_async", never_async)
    monkeypatch.setattr(exporting, "build_xlsx_rows", never)
    text_token = _store(site, RUI)
    sheet_token = _store(site, RUI, XLSX, "Prazos_20261111.xlsx")
    grid = {"titulo": "t", "formato": "xlsx", "linhas": "1", "colunas": "1", "c0_0": "x"}
    if case == "text":
        path, form = text_token, {**TEXT_FORM, "texto": "x" * (editing.MAX_TEXT + 1)}
    elif case == "title":
        path, form = text_token, {**TEXT_FORM, "titulo": "t" * (editing.MAX_TITLE + 1)}
    elif case == "rows":
        path, form = sheet_token, {**grid, "linhas": str(editing.MAX_ROWS + 1)}
    elif case == "cols":
        path, form = sheet_token, {**grid, "colunas": str(editing.MAX_COLS + 1)}
    else:
        path, form = sheet_token, {**grid, "c0_0": "x" * (editing.MAX_CELL + 1)}

    response = _post(site, RUI, f"/web/editor/{path}/baixar", form)

    assert response.status_code == 413
    assert _audit(site) == []


def test_a_body_over_the_cap_is_refused_before_it_is_read(site, monkeypatch):
    async def never_async(*args, **kwargs):
        raise AssertionError("built from an over-cap body")

    monkeypatch.setattr(exporting, "build_async", never_async)
    token = _store(site, RUI)

    response = _post(site, RUI, f"/web/editor/{token}/baixar",
                     {**TEXT_FORM, "texto": "x" * 3_000_000})

    assert response.status_code == 403, "refused at the cap, not parsed and then limited"


@pytest.mark.parametrize("route", ["ver", "baixar"])
def test_post_needs_the_token_this_site_and_a_session(site, route):
    token = _store(site, RUI)
    path = f"/web/editor/{token}/{route}"

    assert _post(site, RUI, path, TEXT_FORM, csrf="").status_code == 403
    assert _post(site, RUI, path, TEXT_FORM, csrf="forjado").status_code == 403
    assert _post(site, RUI, path, TEXT_FORM, site_header="cross-site").status_code == 403
    assert _post(site, None, path, TEXT_FORM).status_code != 200
    assert _audit(site) == []


def test_fields_ask_for_native_spelling_help(site):
    text = _get(site, RUI, f"/web/editor/{_store(site, RUI)}").text
    grid = _get(site, RUI, f"/web/editor/{_store(site, RUI, XLSX, 'p.xlsx')}").text

    assert re.search(r"<textarea[^>]*lang=pt-BR[^>]*spellcheck=true", text)
    assert re.search(r"<input[^>]*name=titulo[^>]*lang=pt-BR[^>]*spellcheck=true", text)
    assert re.search(r"<input[^>]*name=c0_0[^>]*lang=pt-BR[^>]*spellcheck=true", grid)


def test_the_script_is_served_and_the_page_has_no_inline_script(site):
    script = site.client.get("/web/editor/editor.js")
    page = _get(site, RUI, f"/web/editor/{_store(site, RUI)}").text

    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert script.headers["x-content-type-options"] == "nosniff"
    assert "src='/web/editor/editor.js'" in page
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", page)


def test_off_means_not_found(site, monkeypatch):
    token = _store(site, RUI)
    monkeypatch.setattr(settings, "web_assistant", "off")

    assert _get(site, RUI, f"/web/editor/{token}").status_code == 404
    assert site.client.get("/web/editor/editor.js").status_code == 404


def test_a_title_typed_as_a_formula_stays_text(site):
    token = _store(site, RUI, XLSX, "Prazos_20261111.xlsx")

    response = _post(site, RUI, f"/web/editor/{token}/baixar", {
        "titulo": "=1+1", "linhas": "1", "colunas": "1", "formato": "xlsx", "c0_0": "a"})

    xml = zipfile.ZipFile(io.BytesIO(response.content)).read("xl/worksheets/sheet1.xml")
    assert response.status_code == 200 and b"<f>" not in xml


def test_a_file_whose_seal_does_not_open_is_not_found_on_download(site):
    token = _store(site, RUI)
    _rows(site, "UPDATE panel_files SET content_sha = repeat('0', 64) RETURNING 1")

    assert _get(site, RUI, f"/web/editor/{token}").status_code == 404
    assert _post(site, RUI, f"/web/editor/{token}/baixar", TEXT_FORM).status_code == 404
    assert _audit(site) == []


def test_preview_is_for_a_text_document_only(site):
    sheet = _store(site, RUI, XLSX, "Prazos_20261111.xlsx")
    pdf = _store(site, RUI, b"%PDF-1.7 x", "a.pdf")

    assert _post(site, RUI, f"/web/editor/{sheet}/ver", TEXT_FORM).status_code == 404
    assert _post(site, RUI, f"/web/editor/{pdf}/ver", TEXT_FORM).status_code == 404
