"""Laws in the corpus follow their official page, and nothing else moves them.

The example is the one #77 found: the Lei 8.245 limit on how often a landlord
may seek a court eviction for arrears. The page says one thing today; a copy
ingested before the change says another, and the firm would go on citing it.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from core.config import settings
from core.handlers import legal_message
from core.services import channels
from core.integrations import web_fetch
from core.models import OfficialSource, StaffMember
from core.repositories import documents
from core.services import document_storage, ingestion, law_refresh
from core.services.authorization import decide

pytestmark = pytest.mark.usefixtures(
    "isolate_message_rate_limits", "pending_store", "callback_store",
)

_URL = "https://www.planalto.gov.br/ccivil_03/leis/l8245.htm"

_BODY = "<p>Art. 1º A locação de imóvel urbano regula-se pelo disposto nesta Lei.</p>" * 40


def _page(limit: str) -> bytes:
    return (
        "<html><body>"
        f"{_BODY}"
        "<p>Art. 62. Parágrafo único. Não se admitirá a emenda da mora se o locatário "
        f"houver utilizado essa faculdade nos {limit} imediatamente anteriores à "
        "propositura da ação.</p>"
        "</body></html>"
    ).encode("utf-8")


_OLD_PAGE = _page("doze meses")
_NEW_PAGE = _page("24 (vinte e quatro) meses")


class _Conn:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@pytest.fixture
def corpus(monkeypatch):
    """Document 5, the Lei 8.245 as ingested before the page changed."""
    state = SimpleNamespace(
        full_text=ingestion.extract_text_bytes(_OLD_PAGE, "l8245.html"),
        replaced=None, original=None, marked=[], logged=None,
        invalidated=0, embedded=0, stored=[], removed=[], fail_on_write=False,
        link=None, served=_NEW_PAGE, fetched=[],
    )

    async def get_document(_conn, doc_id, scope=None):
        if doc_id != 5:
            return None
        return SimpleNamespace(id=5, title="Lei 8.245", full_text=state.full_text)

    async def get_official(_conn, doc_id, scope=None):
        if doc_id != 5:
            return None
        return OfficialSource(id=5, title="Lei 8.245", source_url=state.link)

    async def embed(chunks):
        state.embedded += len(chunks)
        return [[0.0] for _ in chunks]

    async def store(data, *, raw_sha256, filename):
        state.stored.append((raw_sha256, filename))
        return document_storage.StoredOriginal(
            key=f"aa/bb/{raw_sha256}.html", newly_created=True,
        )

    async def remove(key, wrapped_key=None):
        state.removed.append(key)

    async def replace_text(_conn, doc_id, **kwargs):
        if state.fail_on_write:
            raise RuntimeError("disk full")
        state.replaced = (doc_id, kwargs["full_text"])
        return True

    async def replace_original(_conn, doc_id, **kwargs):
        state.original = (doc_id, kwargs)

    async def mark(_conn, doc_id, *, source_url=None, commit=True):
        state.marked.append((doc_id, source_url))

    async def log(_conn, **kwargs):
        state.logged = kwargs

    async def invalidate():
        state.invalidated += 1

    async def fetch(url):
        state.fetched.append(url)
        return web_fetch.FetchedDocument(url=url, content_type="text/html", data=state.served)

    monkeypatch.setattr(ingestion, "get_document", get_document)
    monkeypatch.setattr(ingestion, "embed_texts", embed)
    monkeypatch.setattr(ingestion.document_storage, "store", store)
    monkeypatch.setattr(ingestion.document_storage, "remove", remove)
    monkeypatch.setattr(ingestion, "replace_document_text", replace_text)
    monkeypatch.setattr(ingestion, "replace_document_original", replace_original)
    monkeypatch.setattr(ingestion, "mark_source_checked", mark)
    monkeypatch.setattr(ingestion, "log_access", log)
    monkeypatch.setattr(ingestion.cache, "invalidate_answers", invalidate)
    monkeypatch.setattr(law_refresh, "get_official_source", get_official)
    monkeypatch.setattr(law_refresh, "mark_source_checked", mark)
    monkeypatch.setattr(law_refresh.web_fetch, "fetch_document", fetch)
    return state


def _refresh(conn=None, **kwargs):
    return asyncio.run(law_refresh.refresh_document(conn or _Conn(), 5, **kwargs))


# ── what an amendment does ──────────────────────────────────────────────────


def test_an_amended_law_lands_on_the_same_document(corpus):
    conn = _Conn()

    result = _refresh(conn, url=_URL)

    doc_id, text = corpus.replaced
    assert doc_id == 5, "a new id would orphan everything the audit log points at"
    assert "24 (vinte e quatro) meses" in text
    assert "doze meses" not in text
    assert result.outcome == "updated"
    assert conn.commits == 1


def test_the_update_links_the_page_replaces_the_original_and_is_audited(corpus):
    _refresh(url=_URL)

    assert (5, _URL) in corpus.marked
    stored_id, original = corpus.original
    assert stored_id == 5 and original["source_filename"] == "l8245.html"
    assert corpus.logged["action"] == "refresh"
    assert corpus.logged["document_ids"] == [5]
    assert corpus.invalidated == 1, "cached answers may quote the old limit"


def test_an_unchanged_page_writes_and_spends_nothing_but_the_check(corpus):
    corpus.served = _OLD_PAGE
    corpus.link = _URL
    conn = _Conn()

    result = _refresh(conn)

    assert result.outcome == "unchanged"
    assert corpus.replaced is None
    assert corpus.embedded == 0, "no embeddings paid for a law that did not change"
    assert corpus.marked == [(5, None)], "the check time moves, the link stays"
    assert conn.commits == 0


def test_linking_an_unchanged_page_keeps_the_link(corpus):
    corpus.served = _OLD_PAGE

    _refresh(url=_URL)

    assert corpus.marked == [(5, _URL)]


# ── what a wrong page does ──────────────────────────────────────────────────


def test_a_page_nothing_like_the_law_is_held_and_its_url_not_kept(corpus):
    """The likeliest reason a law halves overnight is a wrong link."""
    corpus.served = (
        b"<html><body>" + b"<p>Pagina em manutencao, volte mais tarde.</p>" * 30
        + b"</body></html>"
    )

    result = _refresh(url=_URL)

    assert result.outcome == "needs_review"
    assert result.change < -settings.law_refresh_max_change
    assert corpus.replaced is None and corpus.embedded == 0
    assert corpus.marked == [], "a URL that was never shown to be right is not kept"


def test_a_held_change_on_a_linked_page_waits_a_week_not_a_day(corpus):
    corpus.link = _URL
    corpus.served = _OLD_PAGE + b"<p>" + b"texto novo " * 3000 + b"</p>"

    result = _refresh()

    assert result.outcome == "needs_review"
    assert corpus.marked == [(5, None)]
    assert corpus.replaced is None


def test_forcing_applies_a_change_the_guard_held(corpus):
    corpus.link = _URL
    corpus.served = _OLD_PAGE + b"<p>" + b"texto novo " * 3000 + b"</p>"

    result = _refresh(force=True)

    assert result.outcome == "updated"
    assert corpus.replaced is not None


def test_a_failed_write_leaves_the_law_as_it_was(corpus):
    corpus.fail_on_write = True
    conn = _Conn()

    with pytest.raises(RuntimeError):
        _refresh(conn, url=_URL)

    assert conn.rollbacks == 1 and conn.commits == 0
    assert corpus.removed, "the new original written for this attempt is taken back"
    assert corpus.invalidated == 0


def test_a_document_without_a_page_is_not_fetched(corpus):
    with pytest.raises(LookupError, match="not linked"):
        _refresh()
    assert corpus.fetched == []


def test_an_unreadable_page_changes_nothing(corpus, monkeypatch):
    corpus.link = _URL

    async def refuse(_url):
        raise web_fetch.FetchError("the official page returned HTTP 503")

    monkeypatch.setattr(law_refresh.web_fetch, "fetch_document", refuse)

    with pytest.raises(web_fetch.FetchError):
        _refresh()
    assert corpus.marked == [] and corpus.replaced is None


def test_the_content_type_decides_how_the_page_is_read():
    assert web_fetch.filename_for(_URL, "text/html; charset=windows-1252") == "l8245.html"
    assert web_fetch.filename_for("https://tj.jus.br/doc?id=9", "application/pdf") == "doc.pdf"
    assert web_fetch.filename_for("https://www.gov.br/", "text/plain") == "documento.txt"


# ── the daily pass ──────────────────────────────────────────────────────────


def test_the_pass_reports_an_unreadable_page_only_once_it_is_overdue(monkeypatch):
    now = datetime.now(timezone.utc)
    due = [
        OfficialSource(id=1, title="Lida ontem", source_url=_URL, checked_at=now - timedelta(days=8)),
        OfficialSource(id=2, title="Sem leitura há um mês", source_url=_URL,
                       checked_at=now - timedelta(days=30)),
        OfficialSource(id=3, title="Funciona", source_url=_URL, checked_at=None),
    ]

    class _Pool:
        def connection(self):
            return self

        async def __aenter__(self):
            return "conn"

        async def __aexit__(self, *exc):
            return False

    async def pool():
        return _Pool()

    async def listed(_conn, *, older_than_days):
        return due

    async def refresh(_conn, doc_id, **_kwargs):
        if doc_id in (1, 2):
            raise web_fetch.FetchError("the official page returned HTTP 503")
        return ingestion.Refreshed(3, "Funciona", "updated", 100, 101, 2)

    monkeypatch.setattr(law_refresh, "get_pool", pool)
    monkeypatch.setattr(law_refresh, "list_due_for_refresh", listed)
    monkeypatch.setattr(law_refresh, "refresh_document", refresh)

    report = asyncio.run(law_refresh.refresh_due_documents())

    assert [doc_id for doc_id, _, _ in report.unreadable] == [2], (
        "an outage the next day's pass will get past is not news"
    )
    assert [r.document_id for r in report.refreshed] == [3], "one failure does not stop the rest"


def test_an_unchanged_pass_says_nothing():
    quiet = law_refresh.PassReport(
        refreshed=[ingestion.Refreshed(5, "Lei 8.245", "unchanged", 100, 100)],
        unreadable=[],
    )
    assert law_refresh.summary(quiet) is None


def test_the_report_names_what_changed_what_was_held_and_what_was_not_read():
    report = law_refresh.PassReport(
        refreshed=[
            ingestion.Refreshed(5, "Lei 8.245", "updated", 108_750, 108_900, 61),
            ingestion.Refreshed(7, "Constituição", "needs_review", 1_000_000, 600_000),
        ],
        unreadable=[(8, "Código Civil", "the official page returned HTTP 404")],
    )

    message = law_refresh.summary(report)

    assert message is not None
    assert "[5] Lei 8.245: 108.750 → 108.900 caracteres, 61 trechos" in message
    assert "[7] Constituição: texto 40% menor" in message
    assert "/atualizar <id> forçar" in message
    assert "[8] Código Civil" in message
    assert "—" not in message and "–" not in message


# ── the repository ──────────────────────────────────────────────────────────


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

    async def fetchall(self):
        return []


class _RecordingConn(_Conn):
    def __init__(self):
        super().__init__()
        self.statements = []

    def cursor(self):
        return _Cursor(self.statements)


def test_a_check_without_a_url_never_touches_the_link():
    conn = _RecordingConn()

    asyncio.run(documents.mark_source_checked(conn, 5))

    sql, params = conn.statements[0]
    assert "source_url" not in sql
    assert params == (5,)


def test_the_longest_waiting_page_is_checked_first():
    conn = _RecordingConn()

    asyncio.run(documents.list_due_for_refresh(conn, older_than_days=7))

    sql, params = conn.statements[0]
    assert "source_url IS NOT NULL" in sql
    assert "ORDER BY source_checked_at NULLS FIRST" in sql
    assert params == (7,)


# ── reading the page ────────────────────────────────────────────────────────


def _stub_transport(monkeypatch, handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    monkeypatch.setattr(web_fetch, "_http", lambda: client)


def test_a_page_too_large_is_refused_rather_than_cut(monkeypatch):
    """The first megabytes of a code indexed as if they were all of it is the
    one failure this must not have."""
    monkeypatch.setattr(settings, "max_doc_mb", 1)
    _stub_transport(monkeypatch, lambda request: httpx.Response(
        200, headers={"content-type": "text/html"}, content=b"x" * (1024 * 1024 + 1),
    ))

    with pytest.raises(web_fetch.FetchError, match="larger than 1 MB"):
        asyncio.run(web_fetch.fetch_document(_URL))


def test_a_whole_page_arrives_whole_with_the_address_it_came_from(monkeypatch):
    body = _NEW_PAGE * 200  # well past fetch_max_chars, which cuts a search page
    _stub_transport(monkeypatch, lambda request: httpx.Response(
        200, headers={"content-type": "text/html"}, content=body,
    ))

    fetched = asyncio.run(web_fetch.fetch_document(_URL))

    assert fetched.data == body
    assert fetched.url == _URL


def test_a_page_outside_the_official_domains_is_never_requested(monkeypatch):
    def handler(request):  # pragma: no cover - must never be reached
        raise AssertionError("an unauthorized domain must not be requested")

    _stub_transport(monkeypatch, handler)

    with pytest.raises(web_fetch.FetchError):
        asyncio.run(web_fetch.fetch_document("https://example.com/lei.htm"))


def test_the_agent_reads_the_planalto_as_a_browser():
    """Decided by the firm on 2026-09-21: the Planalto resets the connection
    for a named agent, and it is the only source of consolidated law."""
    assert settings.fetch_user_agent.startswith("Mozilla/5.0")


# ── the command ─────────────────────────────────────────────────────────────

_INTERN = StaffMember(id=4, chat_id="400", name="Rita", role="estagiario")
_LAWYER = StaffMember(id=6, chat_id="600", name="Lia", role="advogado")


def test_only_a_lawyer_or_above_may_rewrite_a_law():
    assert decide(_LAWYER, "document.refresh").allowed
    assert not decide(_INTERN, "document.refresh").allowed


def _command(monkeypatch, staff, text, result=None, error=None):
    legal_message._last_deliveries.clear()
    sent: list[str] = []
    calls: list[dict] = []

    async def get_staff(_conn, chat_id):
        return staff if str(chat_id) == str(staff.chat_id) else None

    async def send(_chat_id, message, reply_markup=None):
        sent.append(message)
        return 1

    async def typing(*_args, **_kwargs):
        return None

    async def refresh(_conn, doc_id, **kwargs):
        calls.append({"id": doc_id, **kwargs})
        if error:
            raise error
        return result

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(channels, "send_typing", typing)
    monkeypatch.setattr(legal_message, "refresh_document", refresh)
    update = {
        "update_id": 1,
        "message": {
            "chat": {"id": int(staff.chat_id), "type": "private"},
            "from": {"id": int(staff.chat_id), "first_name": "Pessoa"},
            "text": text,
        },
    }
    asyncio.run(legal_message.process_legal_message("connection", update))
    return sent, calls


def test_an_intern_is_refused_before_anything_is_read(monkeypatch):
    _sent, calls = _command(monkeypatch, _INTERN, f"/atualizar 5 {_URL}")
    assert calls == []


def test_the_command_passes_the_url_and_the_force(monkeypatch):
    done = ingestion.Refreshed(5, "Lei 8.245", "updated", 100, 101, 61)

    sent, calls = _command(monkeypatch, _LAWYER, f"/atualizar 5 {_URL} forçar", result=done)

    assert calls == [{"id": 5, "url": _URL, "force": True, "staff_id": 6}]
    assert "[5] Lei 8.245 atualizado" in sent[-1]


def test_something_that_is_not_a_url_is_not_read_as_one(monkeypatch):
    sent, calls = _command(monkeypatch, _LAWYER, "/atualizar 5 planalto")
    assert calls == []
    assert "URL https" in sent[-1]


def test_a_held_change_tells_the_lawyer_exactly_what_to_send(monkeypatch):
    held = ingestion.Refreshed(5, "Lei 8.245", "needs_review", 100_000, 40_000)

    sent, _calls = _command(monkeypatch, _LAWYER, f"/atualizar 5 {_URL}", result=held)

    assert "60% menor" in sent[-1]
    assert "nem guardei essa URL" in sent[-1]
    assert f"/atualizar 5 {_URL} forçar" in sent[-1]


def test_an_unlinked_law_is_told_how_to_link_it(monkeypatch):
    async def summary(_conn, doc_id, scope=None):
        return SimpleNamespace(id=doc_id, title="Lei 8.245")

    monkeypatch.setattr(legal_message, "get_document_summary", summary)

    sent, _calls = _command(monkeypatch, _LAWYER, "/atualizar 5",
                            error=LookupError("not linked"))

    assert "/atualizar 5 <url oficial>" in sent[-1]


def test_the_menu_offers_the_command_in_the_firms_words():
    menu = {entry["command"]: entry["description"] for entry in legal_message.command_menu()}
    assert menu["atualizar"] == "liga uma lei à página oficial"
