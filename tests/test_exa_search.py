"""Exa answers the same question as Serper, under the same boundary.

The provider is a supplier, not a policy. Whichever one answers, the allowlist
decides what may come back, the page reader confirms what a page says, and
DataJud stays authoritative for a process number.
"""
import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from core.config import settings
from core.integrations import exa, serper, websearch
from core.services.legal_assistant import external_research, prompts
from core.services.legal_assistant.state import LegalState


class _Response:
    def __init__(self, payload: Any, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Client:
    """Captures the request and returns a canned response."""

    def __init__(self, response: _Response):
        self._response = response
        self.sent: dict = {}
        self.headers: dict = {}

    async def post(self, url, headers=None, json=None):
        self.sent = json or {}
        self.headers = headers or {}
        return self._response


@pytest.fixture(autouse=True)
def official_domains(monkeypatch):
    monkeypatch.setattr(
        settings, "serper_allowed_domains", "jus.br,gov.br,leg.br",
    )
    monkeypatch.setattr(settings, "exa_api_key", "test-key")
    monkeypatch.setattr(settings, "exa_max_results", 8)


def _wire(monkeypatch, payload, status_code=200) -> _Client:
    client = _Client(_Response(payload, status_code))
    monkeypatch.setattr(exa, "_http", lambda: client)
    return client


def _result(url: str, **extra) -> dict:
    return {"title": "Acórdão", "url": url, **extra}


# ── the boundary ────────────────────────────────────────────────────────────

def test_a_url_outside_the_allowlist_is_rejected_even_though_exa_was_asked_to_filter(
    monkeypatch,
):
    """`includeDomains` is the provider's promise, not our verification.

    A supplier that widens its own filter — by bug, by policy change, or
    because a redirect resolved elsewhere — must not widen ours.
    """
    _wire(monkeypatch, {"results": [
        _result("https://www.stj.jus.br/acordao"),
        _result("https://jusbrasil.com.br/artigo"),
        _result("https://blog.exemplo.com/resumo"),
    ]})

    results = asyncio.run(exa.search("alimentos trinômio"))

    assert [r.url for r in results] == ["https://www.stj.jus.br/acordao"]


def test_the_allowlist_is_sent_to_exa_as_well(monkeypatch):
    """Asking the provider to filter is still worth doing: it saves a round
    trip full of results that would only be discarded here."""
    client = _wire(monkeypatch, {"results": []})

    asyncio.run(exa.search("alimentos"))

    assert client.sent["includeDomains"] == ["jus.br", "gov.br", "leg.br"]


def test_a_non_https_url_is_rejected(monkeypatch):
    _wire(monkeypatch, {"results": [_result("http://www.stj.jus.br/acordao")]})

    assert asyncio.run(exa.search("alimentos")) == []


def test_the_key_never_travels_in_the_query(monkeypatch):
    client = _wire(monkeypatch, {"results": []})

    asyncio.run(exa.search("alimentos"))

    assert client.headers.get("x-api-key") == "test-key"
    assert "test-key" not in str(client.sent)


# ── what it returns ─────────────────────────────────────────────────────────

def test_the_highlight_becomes_the_excerpt(monkeypatch):
    """A highlight is text from the page, which is why this provider is worth
    measuring: a result nobody opens still carries the ementa."""
    _wire(monkeypatch, {"results": [_result(
        "https://www.stj.jus.br/acordao",
        highlights=["EMENTA: AÇÃO REVISIONAL DE ALIMENTOS - TRINÔMIO..."],
    )]})

    results = asyncio.run(exa.search("alimentos"))

    assert "EMENTA" in results[0].snippet


def test_a_result_with_no_highlight_still_comes_back(monkeypatch):
    _wire(monkeypatch, {"results": [_result("https://www.stj.jus.br/a")]})

    results = asyncio.run(exa.search("alimentos"))

    assert results[0].snippet == ""
    assert results[0].url.endswith("/a")


def test_duplicate_urls_are_collapsed(monkeypatch):
    _wire(monkeypatch, {"results": [
        _result("https://www.stj.jus.br/a"),
        _result("https://www.stj.jus.br/a"),
    ]})

    assert len(asyncio.run(exa.search("alimentos"))) == 1


def test_the_conversational_wrapper_is_stripped_here_too(monkeypatch):
    """The same cleaning both providers get — it is a property of the question,
    not of the supplier."""
    client = _wire(monkeypatch, {"results": []})

    asyncio.run(exa.search(
        "Você pode fazer uma pesquisa sobre o seguinte tema: alimentos compensatórios"
    ))

    assert client.sent["query"].startswith("alimentos compensatórios")


# ── failing safely ──────────────────────────────────────────────────────────

def test_a_missing_key_is_refused_before_any_call(monkeypatch):
    monkeypatch.setattr(settings, "exa_api_key", "")

    with pytest.raises(exa.ExaError, match="not configured"):
        asyncio.run(exa.search("alimentos"))


@pytest.mark.parametrize("status,expected", [
    (401, "rejected the configured key"),
    (429, "query limit"),
    (500, "HTTP 500"),
])
def test_provider_failures_are_named_safely(monkeypatch, status, expected):
    _wire(monkeypatch, {"results": []}, status_code=status)

    with pytest.raises(exa.ExaError, match=expected):
        asyncio.run(exa.search("alimentos"))


def test_a_malformed_body_is_a_safe_failure(monkeypatch):
    _wire(monkeypatch, ValueError("not json"))

    with pytest.raises(exa.ExaError, match="invalid response"):
        asyncio.run(exa.search("alimentos"))


def test_a_body_without_results_is_empty_rather_than_an_error(monkeypatch):
    _wire(monkeypatch, {"unexpected": True})

    assert asyncio.run(exa.search("alimentos")) == []


# ── the switch ──────────────────────────────────────────────────────────────

def test_serper_is_the_default(monkeypatch):
    assert websearch.active_provider() == "serper"


def test_the_flag_chooses_the_provider(monkeypatch):
    called = []

    async def fake_exa(query):
        called.append(("exa", query))
        return []

    async def fake_serper(query):
        called.append(("serper", query))
        return []

    monkeypatch.setattr(exa, "search", fake_exa)
    monkeypatch.setattr(serper, "search", fake_serper)

    asyncio.run(websearch.search("alimentos"))
    monkeypatch.setattr(settings, "web_search_provider", "exa")
    asyncio.run(websearch.search("alimentos"))

    assert [c[0] for c in called] == ["serper", "exa"]


def test_each_provider_describes_its_own_evidence(monkeypatch):
    result = serper.SearchResult(
        title="Acórdão", url="https://www.stj.jus.br/a", snippet="EMENTA...",
    )

    as_serper = websearch.format_result(result)
    monkeypatch.setattr(settings, "web_search_provider", "exa")
    as_exa = websearch.format_result(result)

    assert "search-index snippet" in as_serper
    assert "from the page" in as_exa
    # Neither may claim the page was read in full — only the reader does that.
    for rendered in (as_serper, as_exa):
        assert "full reading" in rendered


def test_an_explicit_provider_wins_over_the_setting(monkeypatch):
    """The research resolves the provider once and passes it down, so the name
    it shows is the supplier that ran even if the setting is read again later."""
    called = []

    async def fake_exa(query):
        called.append("exa")
        return []

    async def fake_serper(query):
        called.append("serper")
        return []

    monkeypatch.setattr(exa, "search", fake_exa)
    monkeypatch.setattr(serper, "search", fake_serper)
    monkeypatch.setattr(settings, "web_search_provider", "serper")
    result = serper.SearchResult(title="Acórdão", url="https://www.stj.jus.br/a", snippet="x")

    asyncio.run(websearch.search("alimentos", "exa"))

    assert called == ["exa"]
    assert "Excerpt taken from the page" in websearch.format_result(result, "exa")


def test_an_unknown_setting_falls_back_to_serper_in_one_place(monkeypatch):
    monkeypatch.setattr(settings, "web_search_provider", "outro")

    assert websearch.active_provider() == "serper"


# ── the research pipeline with Exa ──────────────────────────────────────────
# With WEB_SEARCH_PROVIDER=exa, every place that names the provider says Exa,
# and a highlight is described as what it is: text taken from the page and
# selected for the query, which is still not a reading of the page.

HIGHLIGHT = serper.SearchResult(
    title="REsp 1.234.567/SP",
    url="https://www.stj.jus.br/acordao",
    snippet="EMENTA: AÇÃO REVISIONAL DE ALIMENTOS. TRINÔMIO.",
)


@pytest.fixture
def research(monkeypatch):
    """The research with Exa faked at the provider and every other source silent."""
    state = SimpleNamespace(results=[HIGHLIGHT], error=None, during=None, system="", content=[])

    async def exa_search(_query):
        if state.during:
            state.during()
        if state.error:
            raise state.error
        return state.results

    async def serper_search(_query):  # pragma: no cover
        raise AssertionError("WEB_SEARCH_PROVIDER=exa must not reach Serper")

    async def no_datajud(_text):
        return [], [], [], []

    async def no_page_fetch(_sources):
        return [], [], []

    async def complete(system, content, **_kwargs):
        state.system, state.content = system, content
        return "Resposta."

    async def log_access(*_args, **_kwargs):
        return None

    monkeypatch.setattr(settings, "web_search_provider", "exa")
    monkeypatch.setattr(exa, "search", exa_search)
    monkeypatch.setattr(serper, "search", serper_search)
    monkeypatch.setattr(external_research, "_datajud_context", no_datajud)
    monkeypatch.setattr(external_research, "_read_official_pages", no_page_fetch)
    monkeypatch.setattr(external_research, "complete", complete)
    monkeypatch.setattr(external_research, "log_access", log_access)
    return state


def _ask():
    return asyncio.run(external_research.research(LegalState(
        conn=None,
        question="Qual é o entendimento do STJ sobre alimentos?",
        query="entendimento STJ alimentos",
        task="external_research",
    )))


def test_exa_is_named_wherever_the_provider_is(research):
    result = _ask()
    rendered = str(research.content)

    assert "retornados pelo Exa" in research.system
    assert "Resultados do Exa" in research.system
    assert "via Exa" in result["answer"]
    assert "Exa result filtered by domain" in rendered
    for text in (research.system, result["answer"], rendered):
        assert "Serper" not in text
    assert [source.reference for source in result["sources"]] == [HIGHLIGHT.url]
    assert result["limitations"] == []
    assert result["cacheable"] is True


def test_a_highlight_is_an_excerpt_from_the_page_not_a_reading(research):
    result = _ask()
    rendered = str(research.content)

    assert "Excerpt taken from the page: EMENTA" in rendered
    assert "not a full reading of the page" in rendered
    assert "Search-engine snippet" not in rendered
    assert "trecho extraído do texto da própria página" in research.system
    assert "não afirme ter lido o inteiro teor" in research.system
    assert "índice de busca" not in research.system
    assert "não correspondem à leitura integral" in result["answer"]


def test_an_exa_failure_is_reported_and_logged_as_exa(research, caplog):
    research.error = exa.ExaError("Exa did not respond")

    with caplog.at_level(logging.INFO, logger=external_research.logger.name):
        result = _ask()

    assert "Pesquisa web oficial via Exa: Exa did not respond" in result["limitations"]
    assert "Restricted Exa search failed: Exa did not respond" in caplog.text
    assert "exa_sources=0" in caplog.text
    assert "Serper" not in caplog.text
    assert result["cacheable"] is False


def test_no_exa_result_is_reported_as_exa(research):
    research.results = []

    result = _ask()

    assert (
        "Pesquisa web oficial via Exa: nenhum resultado aceito nos domínios autorizados"
        in result["limitations"]
    )


def test_the_name_follows_the_supplier_that_ran_not_a_later_reading(research, monkeypatch):
    """The setting flips to Serper while Exa is answering. Exa's results are
    what the answer is built on, so Exa is what it has to name."""
    research.during = lambda: monkeypatch.setattr(settings, "web_search_provider", "serper")

    result = _ask()

    assert "via Exa" in result["answer"]
    assert "Excerpt taken from the page" in str(research.content)
    assert "Serper" not in result["answer"] + research.system


def test_the_exa_prompt_carries_no_serper_rule():
    exa_prompt = prompts.external_research("exa")

    assert "Serper" not in exa_prompt
    assert "índice de busca" not in exa_prompt
    assert "Resultados do Exa" in exa_prompt
    assert "trecho devolvido pelo Exa" in exa_prompt
