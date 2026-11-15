"""A law ingested by URL is the whole law, and comes back as a readable file.

`/ingerir` used to index what the page reader returns beside a question, which
is cut at `fetch_max_chars`: the Constitution arrived as its first 20,000
characters, the corpus held that fragment as if it were the law, and the file
sent back was the fragment too. These tests pin the three things that fixes:
the whole document is read, the whole of it is indexed, and what goes back is
the piece itself.
"""
import asyncio
from types import SimpleNamespace

import pytest

from core.config import settings
from core.handlers import legal_message
from core.services import channels
from core.integrations import web_fetch
from core.models import StaffMember
from core.repositories import documents
from core.services import document_storage, ingestion

pytestmark = pytest.mark.usefixtures(
    "isolate_message_rate_limits", "pending_store", "callback_store",
)

_LAWYER = StaffMember(id=6, chat_id="600", name="Lia", role="advogado")
_URL = "https://www.planalto.gov.br/ccivil_03/leis/l8245.htm"

# Longer than fetch_max_chars, which is what the old path would have cut it to.
_BODY = "<p>Art. 1º A locação de imóvel urbano regula-se por esta Lei.</p>" * 900


def _page(markup: str = _BODY) -> bytes:
    return f"<html><body>{markup}</body></html>".encode("utf-8")


@pytest.fixture
def chat(monkeypatch):
    state = SimpleNamespace(
        sent=[], files=[], ingested=[], fetched=[],
        document=SimpleNamespace(document_id=12, chunk_count=61, text="Art. 1º " * 5000),
        content_type="text/html", data=_page(), fail=None,
    )

    async def get_staff(_conn, chat_id):
        return _LAWYER if str(chat_id) == "600" else None

    async def send(_chat_id, text, reply_markup=None):
        state.sent.append(text)
        return 1

    async def typing(*_args, **_kwargs):
        return None

    async def fetch_document(url):
        state.fetched.append(url)
        if state.fail:
            raise state.fail
        return web_fetch.FetchedDocument(url=url, content_type=state.content_type, data=state.data)

    async def fetch_page(_url):  # pragma: no cover - must never be reached
        raise AssertionError("the whole document is read, not the page excerpt")

    async def ingest(_conn, data, **kwargs):
        state.ingested.append((data, kwargs))
        return state.document

    async def send_document(_chat_id, data, filename, caption=None, base_url=None):
        state.files.append((filename, data, caption))
        return True

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(channels, "send_typing", typing)
    monkeypatch.setattr(legal_message, "ingest_document", ingest)
    monkeypatch.setattr(channels, "send_document", send_document)
    monkeypatch.setattr(legal_message.web_fetch, "fetch_document", fetch_document)
    monkeypatch.setattr(legal_message.web_fetch, "fetch_page", fetch_page)
    return state


def _ingerir(text: str = f"/ingerir {_URL}"):
    update = {"update_id": 1, "message": {
        "chat": {"id": 600, "type": "private"}, "from": {"id": 600}, "text": text,
    }}
    asyncio.run(legal_message.process_legal_message("connection", update))


# ── reading ─────────────────────────────────────────────────────────────────


def test_the_whole_page_is_read_and_indexed(chat):
    _ingerir()

    data, kwargs = chat.ingested[0]
    assert data == chat.data, "the bytes indexed are the bytes the page returned"
    assert len(data) > settings.fetch_max_chars, "this page is longer than an excerpt"
    assert kwargs["filename"] == "l8245.html"
    assert kwargs["source_url"] == _URL


def test_the_page_it_came_from_is_recorded_so_it_stays_current(chat):
    _ingerir()

    assert chat.ingested[0][1]["source_url"] == _URL
    assert "toda semana" in chat.sent[0], "the lawyer is told it will be kept current"
    assert "61 trechos" in chat.sent[0]


def test_a_page_that_cannot_be_read_ingests_nothing(chat):
    chat.fail = web_fetch.FetchError("the official page returned HTTP 403")

    _ingerir()

    assert chat.ingested == [] and chat.files == []
    assert "não consegui baixar" in chat.sent[0].casefold()


def test_a_url_that_is_not_https_is_refused_before_anything_is_fetched(chat):
    _ingerir("/ingerir planalto.gov.br/lei")

    assert chat.fetched == []
    assert "URL https" in chat.sent[0]


def test_an_already_ingested_page_still_records_its_source(chat):
    chat.document = SimpleNamespace(document_id=8, chunk_count=0, text="Art. 1º")

    _ingerir()

    assert "já estava no acervo" in chat.sent[0]
    assert _URL in chat.sent[0]


# ── what comes back ─────────────────────────────────────────────────────────


def test_a_page_comes_back_as_a_pdf_of_what_was_indexed(chat):
    _ingerir()

    name, data, caption = chat.files[0]
    assert name.endswith(".pdf")
    assert data[:4] == b"%PDF", "a .html attachment is not something to read on a phone"
    assert _URL in caption


def test_a_court_pdf_travels_exactly_as_it_is(chat):
    chat.content_type = "application/pdf"
    chat.data = b"%PDF-1.7 the court's own file"

    _ingerir()

    name, data, _caption = chat.files[0]
    assert data == chat.data, "a signed PDF must not be rebuilt"
    assert name == "l8245.pdf"


def test_so_indexar_sends_no_file(chat):
    _ingerir(f"/ingerir {_URL} so-indexar")

    assert chat.ingested and chat.files == []


def test_a_failed_send_points_at_the_document(chat, monkeypatch):
    async def refuse(*_args, **_kwargs):
        return False

    monkeypatch.setattr(channels, "send_document", refuse)

    _ingerir()

    assert "/arquivo 12" in chat.sent[-1]


# ── the pieces underneath ───────────────────────────────────────────────────


def test_the_file_name_comes_from_what_the_server_sent():
    assert web_fetch.filename_for(_URL, "text/html") == "l8245.html"
    assert web_fetch.filename_for("https://tj.jus.br/doc?id=9", "application/pdf") == "doc.pdf"


class _Cursor:
    def __init__(self, log):
        self.log = log
        self.rowcount = 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))


class _Conn:
    def __init__(self):
        self.statements = []
        self.commits = 0

    def cursor(self):
        return _Cursor(self.statements)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


def test_an_ingestion_never_overwrites_a_link_somebody_set():
    """A lawyer's /atualizar link, and its check time, outrank an ingestion."""
    conn = _Conn()

    asyncio.run(documents.link_official_source(conn, 7, _URL))

    sql, params = conn.statements[0]
    assert "source_url IS NULL" in sql
    assert params == (_URL, 7)


def test_ingestion_links_a_document_that_was_already_there(monkeypatch):
    linked = []

    async def existing(_conn, _sha):
        return 8

    async def source(_conn, _doc_id, scope=None):
        return SimpleNamespace(source_storage_key="k8")

    async def link(_conn, doc_id, url, commit=True):
        linked.append((doc_id, url, commit))
        return True

    monkeypatch.setattr(ingestion, "find_document_by_sha", existing)
    monkeypatch.setattr(ingestion, "get_document_source", source)
    monkeypatch.setattr(ingestion, "link_official_source", link)

    result = asyncio.run(ingestion.ingest_document(
        _Conn(), b"texto de uma lei", filename="lei.txt", source_url=_URL,
    ))

    assert result.document_id == 8 and result.chunk_count == 0
    # Written in the ingestion's own transaction, committed with it.
    assert linked == [(8, _URL, False)]


def test_a_new_document_is_created_with_the_page_it_came_from(monkeypatch):
    created = {}

    async def none(_conn, _sha):
        return None

    async def embed(chunks):
        return [[0.0] for _ in chunks]

    async def create(_conn, **kwargs):
        created.update(kwargs)
        return 30

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ingestion, "find_document_by_sha", none)
    monkeypatch.setattr(ingestion, "embed_texts", embed)
    async def _store(data, **kwargs):
        return document_storage.StoredOriginal(key="aa/bb/cc", newly_created=True)

    monkeypatch.setattr(ingestion.document_storage, "store", _store)
    monkeypatch.setattr(ingestion, "create_document", create)
    monkeypatch.setattr(ingestion, "add_chunks", nothing)
    monkeypatch.setattr(ingestion, "log_access", nothing)
    monkeypatch.setattr(ingestion.cache, "invalidate_answers", nothing)

    result = asyncio.run(ingestion.ingest_document(
        _Conn(), b"texto de uma lei nova", filename="lei.txt", source_url=_URL,
    ))

    assert result.document_id == 30 and result.chunk_count == 1
    assert created["source_url"] == _URL, "the page is what keeps this law current"
