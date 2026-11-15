"""Reading official pages, following a case, and who is allowed to ask.

The rules under test are the ones that keep deep research honest: only
authorized official domains are ever opened, a redirect cannot smuggle the
fetcher somewhere else, DataJud points at the court's own system rather than
pretending to hold the case file, and the corpus answers a person, not a room.
"""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from core.handlers import legal_message
from core.integrations import process_links, web_fetch

pytestmark = pytest.mark.usefixtures("isolate_message_rate_limits")


# ── Reading an official page ─────────────────────────────────────────────────

def test_html_becomes_readable_text_without_scripts():
    markup = (
        "<html><head><title>Lei 8.078</title><style>b{}</style></head>"
        "<body><script>var x=1</script><h1>Art. 6º</h1>"
        "<p>São direitos básicos do consumidor.</p></body></html>"
    )
    text = web_fetch.html_to_text(markup)

    assert "Art. 6º" in text
    assert "direitos básicos do consumidor" in text
    assert "var x" not in text, "script contents are not page text"


def _stub_transport(monkeypatch, handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    monkeypatch.setattr(web_fetch, "_http", lambda: client)
    return client


def test_only_allowlisted_https_pages_are_opened(monkeypatch):
    def handler(request):  # pragma: no cover - must never be reached
        raise AssertionError("an unauthorized domain must not be requested")

    _stub_transport(monkeypatch, handler)

    for url in (
        "https://blogjuridico.com/artigo",
        "http://www.planalto.gov.br/lei",  # not HTTPS
        "https://planalto.gov.br.evil.com/lei",
    ):
        with pytest.raises(web_fetch.FetchError):
            asyncio.run(web_fetch.fetch_page(url))


def test_a_redirect_off_the_allowlist_is_refused(monkeypatch):
    requested: list[str] = []

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://exfiltration.example/x"})

    _stub_transport(monkeypatch, handler)

    with pytest.raises(web_fetch.FetchError):
        asyncio.run(web_fetch.fetch_page("https://www.stj.jus.br/tema"))
    assert requested == ["https://www.stj.jus.br/tema"], "the redirect must not be followed"


def test_an_official_page_is_read_and_capped(monkeypatch):
    body = "<html><title>STJ</title><body><p>" + ("decisão " * 5000) + "</p></body></html>"

    def handler(request):
        return httpx.Response(
            200, text=body, headers={"content-type": "text/html; charset=utf-8"},
        )

    _stub_transport(monkeypatch, handler)
    page = asyncio.run(web_fetch.fetch_page("https://www.stj.jus.br/tema"))

    assert page.title == "STJ"
    assert "decisão" in page.text
    assert len(page.text) <= 20_000
    assert page.truncated is True


def test_pages_that_fail_are_skipped_not_fatal(monkeypatch):
    def handler(request):
        if "ok" in str(request.url):
            return httpx.Response(200, text="<p>conteúdo</p>",
                                  headers={"content-type": "text/html"})
        return httpx.Response(500)

    _stub_transport(monkeypatch, handler)
    pages = asyncio.run(web_fetch.fetch_pages([
        "https://www.stj.jus.br/erro", "https://www.stj.jus.br/ok",
    ]))

    assert [page.url for page in pages] == ["https://www.stj.jus.br/ok"]


# ── Following a case to the court's own system ───────────────────────────────

def test_the_process_number_resolves_to_its_court_consultation():
    link = process_links.consultation_link("1000000-00.2024.8.26.0100")
    assert link.court == "TJSP"
    assert "esaj.tjsp.jus.br" in link.url
    assert link.formatted_number == "1000000-00.2024.8.26.0100"

    federal = process_links.consultation_link("50012342020234036100")
    assert "trf3" in federal.url

    labour = process_links.consultation_link("00100001120225020001")
    assert "trt2" in labour.url


def test_an_unmapped_court_falls_back_to_the_national_consultation():
    # Military justice of a state: valid CNJ number, no consultation mapped.
    link = process_links.consultation_link("00100001120229130001")
    assert link.url == process_links.CNJ_CONSULTATION


def test_the_lawyer_is_told_where_to_open_it_and_what_to_do_next():
    described = process_links.describe("1000000-00.2024.8.26.0100")
    assert "e-SAJ" in described
    assert "me envie o arquivo aqui" in described


# ── Who may ask ──────────────────────────────────────────────────────────────

def _run_update(monkeypatch, message):
    sent: list[str] = []
    reached = []

    async def get_staff(*args, **kwargs):
        reached.append("staff-lookup")
        return SimpleNamespace(id=7, name="Paulo", role="owner")

    async def send(chat_id, text, reply_markup=None):
        sent.append(text)

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "_send", send)
    asyncio.run(
        legal_message.process_legal_message("connection", {"update_id": 7, "message": message})
    )
    return sent, reached


def test_a_group_chat_is_ignored_even_if_a_member_is_staff(monkeypatch):
    sent, reached = _run_update(monkeypatch, {
        "chat": {"id": -100200, "type": "group"},
        "from": {"id": 111, "first_name": "Paulo"},
        "text": "me manda o acervo",
    })
    assert not sent and not reached, "a group must not reach the corpus at all"


def test_a_message_whose_sender_is_not_the_chat_owner_is_ignored(monkeypatch):
    sent, reached = _run_update(monkeypatch, {
        "chat": {"id": 111, "type": "private"},
        "from": {"id": 222, "first_name": "Outro"},
        "text": "quais documentos temos?",
    })
    assert not sent and not reached


def test_a_normal_private_message_still_goes_through(monkeypatch):
    sent, reached = _run_update(monkeypatch, {
        "chat": {"id": 111, "type": "private"},
        "from": {"id": 111, "first_name": "Paulo"},
        "text": "/id",
    })
    assert reached == ["staff-lookup"]
    assert sent and "111" in sent[0]


# ── Retention ────────────────────────────────────────────────────────────────

def test_retention_redacts_questions_before_deleting_the_audit_trail():
    """Two windows, on purpose: what was asked ages out long before who read what."""
    executed: list[tuple] = []

    class _Cursor:
        rowcount = 3

        async def execute(self, sql, params=None):
            executed.append((" ".join(sql.split()), params))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _Conn:
        def cursor(self):
            return _Cursor()

        async def commit(self):
            executed.append(("COMMIT", None))

    from core.repositories.documents import purge_expired_access_log

    deleted = asyncio.run(purge_expired_access_log(_Conn(), 365))

    assert deleted == 3
    assert "UPDATE document_access_log SET query = NULL" in executed[0][0]
    assert executed[0][1] == (90,), "queries are redacted on the shorter window"
    assert "DELETE FROM document_access_log" in executed[1][0]
    assert executed[1][1] == (365,)
    assert executed[2][0] == "COMMIT"


def test_retention_is_a_no_op_when_disabled():
    class _Conn:
        def cursor(self):  # pragma: no cover - must never be used
            raise AssertionError("a disabled retention must not touch the database")

    from core.repositories.documents import purge_expired_access_log

    assert asyncio.run(purge_expired_access_log(_Conn(), 0)) == 0
