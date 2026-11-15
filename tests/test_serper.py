import asyncio
import logging
import re
from types import SimpleNamespace

import pytest

from core.config import settings
from core.integrations import exa, serper
from core.services.legal_assistant import external_research, prompts, sources
from core.services.legal_assistant.state import LegalState


def test_url_filter_accepts_only_https_official_domains(monkeypatch):
    monkeypatch.setattr(
        settings,
        "serper_allowed_domains",
        "jus.br,gov.br,leg.br,mp.br,def.br,oab.org.br",
    )

    assert serper.is_allowed_url("https://www.stj.jus.br/jurisprudencia")
    assert serper.is_allowed_url("https://legis.senado.leg.br/norma")
    assert serper.is_allowed_url("https://www.gov.br/planalto")
    assert serper.is_allowed_url("https://oab.org.br/servicos")

    assert not serper.is_allowed_url("http://www.stj.jus.br/inseguro")
    assert not serper.is_allowed_url("https://stj.jus.br:444/falso")
    assert not serper.is_allowed_url("https://stj.jus.br.evil.example/golpe")
    assert not serper.is_allowed_url("https://stj.jus.br@evil.example/golpe")
    assert not serper.is_allowed_url("https://evil.example/?site=stj.jus.br")


def test_query_is_restricted_before_leaving_the_application(monkeypatch):
    monkeypatch.setattr(settings, "serper_allowed_domains", "jus.br,gov.br")

    query = serper.restricted_query("  dano   moral por atraso de voo  ", "jus.br")

    assert query.startswith("site:jus.br ")
    assert " OR " not in query
    assert query.endswith("dano moral por atraso de voo")


def test_search_filters_untrusted_results_after_serper_response(monkeypatch):
    captured = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "organic": [
                    {
                        "title": "STJ",
                        "link": "https://www.stj.jus.br/jurisprudencia",
                        "snippet": "Resultado oficial.",
                    },
                    {
                        "title": "Domínio imitador",
                        "link": "https://stj.jus.br.evil.example/falso",
                        "snippet": "Não pode chegar ao agente.",
                    },
                    {
                        "title": "Blog",
                        "link": "https://blog.example/opiniao",
                        "snippet": "Também deve ser descartado.",
                    },
                ]
            }

    class Client:
        is_closed = False

        async def post(self, endpoint, **kwargs):
            captured.append({"endpoint": endpoint, **kwargs})
            return Response()

    monkeypatch.setattr(settings, "serper_api_key", "serper-test-key")
    monkeypatch.setattr(settings, "serper_allowed_domains", "jus.br,gov.br")
    monkeypatch.setattr(settings, "serper_max_results", 8)
    monkeypatch.setattr(settings, "serper_max_queries", 2)
    monkeypatch.setattr(serper, "_client", Client())

    results = asyncio.run(serper.search("dano moral atraso de voo"))

    assert all(request["headers"]["X-API-KEY"] == "serper-test-key" for request in captured)
    queries = [request["json"]["q"] for request in captured]
    assert any(query.startswith("site:jus.br ") for query in queries)
    assert any(query.startswith("site:gov.br ") for query in queries)
    assert all(" OR " not in query for query in queries)
    assert [result.url for result in results] == [
        "https://www.stj.jus.br/jurisprudencia"
    ]


def test_formatted_result_discloses_search_snippet_limit():
    result = serper.SearchResult(
        title="Superior Tribunal de Justiça",
        url="https://www.stj.jus.br/tema",
        snippet="Trecho retornado pelo índice.",
        date="1 de setembro de 2026",
    )

    rendered = serper.format_result(result)

    assert result.url in rendered
    assert result.snippet in rendered
    assert "does not represent a full reading" in rendered


def test_named_court_stays_inside_that_institution(monkeypatch):
    """Naming a court is the question — another court's index would answer a
    question nobody asked. The court's own decision index is still fair game.
    """
    monkeypatch.setattr(settings, "serper_allowed_domains", "jus.br,gov.br,leg.br")
    monkeypatch.setattr(settings, "serper_max_queries", 3)

    domains = serper.search_domains("jurisprudência atual do STJ")

    assert domains == ("stj.jus.br", "scon.stj.jus.br")
    assert not any("stf" in d for d in domains), "a court nobody named"
    assert "jus.br" not in domains, "the umbrella defeats naming a court"


# ── the research pipeline with Serper, the default ──────────────────────────
# The wording below is what the pipeline said before the web search became
# neutral between providers. With WEB_SEARCH_PROVIDER=serper it must not change
# by a character, so it is written out here: reading the module's own constants
# would only compare the code with itself.

SERPER_PROMPT = (
    "\n\nOs blocos identificados como fontes oficiais foram retornados "
    "pela Serper e passaram pela lista local de domínios permitidos. "
    "Eles contêm apenas título e trecho do índice de busca: não afirme "
    "ter lido o inteiro teor da página e cite a URL oficial."
)
SERPER_RULE = (
    "- Resultados da Serper contêm somente título, URL e trecho do índice de "
    "busca. Não diga que leu a página integral e não extrapole o trecho fornecido.\n"
)
SERPER_NOTE = (
    "\n\nPesquisa web realizada em tempo real via Serper, limitada a domínios "
    "oficiais autorizados. Os trechos retornados pelo buscador devem ser "
    "confirmados na página oficial antes da utilização em peça ou parecer."
)
STJ_RESULT = serper.SearchResult(
    title="STJ",
    url="https://www.stj.jus.br/tema",
    snippet="Tema repetitivo sobre a matéria consultada.",
)


@pytest.fixture
def pipeline(monkeypatch):
    """The research with the provider faked and every other source silent.

    The fake sits at the provider, under websearch, so the choice between the
    two is exercised rather than assumed. Exa answering here would be the bug.
    """
    state = SimpleNamespace(results=[STJ_RESULT], error=None, system="", content=[])

    async def serper_search(_query):
        if state.error:
            raise state.error
        return state.results

    async def exa_search(_query):  # pragma: no cover
        raise AssertionError("WEB_SEARCH_PROVIDER=serper must not reach Exa")

    async def no_datajud(_text):
        return [], [], [], []

    async def no_page_fetch(_sources):
        return [], [], []

    async def complete(system, content, **_kwargs):
        state.system, state.content = system, content
        return "Resposta baseada na fonte oficial."

    async def log_access(*_args, **_kwargs):
        return None

    monkeypatch.setattr(settings, "web_search_provider", "serper")
    monkeypatch.setattr(serper, "search", serper_search)
    monkeypatch.setattr(exa, "search", exa_search)
    monkeypatch.setattr(external_research, "_datajud_context", no_datajud)
    monkeypatch.setattr(external_research, "_read_official_pages", no_page_fetch)
    monkeypatch.setattr(external_research, "complete", complete)
    monkeypatch.setattr(external_research, "log_access", log_access)
    return state


def _ask():
    return asyncio.run(external_research.research(LegalState(
        conn=None,
        question="Qual é o entendimento atual do STJ?",
        query="entendimento atual STJ",
        task="external_research",
    )))


def _names_exa(text: str) -> bool:
    return re.search(r"\bExa\b", text) is not None


def test_serper_results_reach_the_answer_with_the_wording_they_always_had(pipeline):
    result = _ask()
    rendered = str(pipeline.content)

    assert SERPER_RULE in pipeline.system
    assert pipeline.system.endswith(SERPER_PROMPT)
    assert result["answer"] == "Resposta baseada na fonte oficial." + SERPER_NOTE
    assert "Serper result filtered by domain" in rendered
    assert "Search-engine snippet: Tema repetitivo" in rendered
    assert not any(_names_exa(text) for text in (pipeline.system, result["answer"], rendered))
    # The URL is no longer dumped under the answer: it is kept as a source, so
    # the lawyer is offered the list instead of being handed a bibliography.
    assert [source.reference for source in result["sources"]] == [
        "https://www.stj.jus.br/tema"
    ]
    assert "https://www.stj.jus.br/tema" in sources.listing(result["sources"])
    assert "Tema repetitivo" in sources.listing(result["sources"])
    assert result["limitations"] == []
    assert result["cacheable"] is True


def test_the_serper_prompt_is_the_one_it_always_was():
    assert prompts.external_research("serper") == prompts.EXTERNAL_RESEARCH
    assert SERPER_RULE in prompts.EXTERNAL_RESEARCH


def test_a_serper_failure_is_reported_and_logged_as_before(pipeline, caplog):
    pipeline.error = serper.SerperError("Serper did not respond")

    with caplog.at_level(logging.INFO, logger=external_research.logger.name):
        result = _ask()

    assert "Pesquisa web oficial: Serper did not respond" in result["limitations"]
    assert external_research.NO_EXTERNAL_EVIDENCE in result["limitations"]
    assert result["cacheable"] is False
    assert "Restricted Serper search failed: Serper did not respond" in caplog.text
    assert "serper_sources=0" in caplog.text


def test_no_serper_result_keeps_its_limitation(pipeline):
    pipeline.results = []

    result = _ask()

    assert (
        "Pesquisa web oficial: nenhum resultado aceito nos domínios autorizados"
        in result["limitations"]
    )
    assert result["cacheable"] is False


# ── a page that was read replaces its own snippet ───────────────────────────

TJPA_RESULT = serper.SearchResult(
    title="TJPA",
    url="https://www.tjpa.jus.br/acordao",
    snippet="Apelação cível sobre a matéria consultada.",
)


def test_a_page_read_in_full_does_not_arrive_as_a_snippet_too(pipeline, monkeypatch):
    """Only the first few results are opened, and an opened one used to reach the
    model twice: once as an index snippet saying the inteiro teor was not read,
    and once as the full text whose ementa may be quoted. Two instructions about
    the same URL, contradicting each other."""
    pipeline.results = [STJ_RESULT, TJPA_RESULT]

    async def read_the_stj_page(_sources):
        block = external_research.document_block(
            f"Decision read in full: {STJ_RESULT.title}",
            f"URL: {STJ_RESULT.url}\n\nEMENTA: o texto lido da própria página.",
            "decision fetched and read in real time, identifying fields verified",
        )
        source = sources.Source(
            kind="web",
            title=STJ_RESULT.title,
            reference=STJ_RESULT.url,
            detail="Decisão aberta e lida em tempo real.",
        )
        return [block], [source], []

    monkeypatch.setattr(external_research, "_read_official_pages", read_the_stj_page)

    result = _ask()
    rendered = str(pipeline.content)

    assert "Decision read in full: STJ" in rendered
    assert "Search-engine snippet: Tema repetitivo" not in rendered, (
        "the page was read, so its own snippet is not sent as well"
    )
    # The one nobody opened still arrives as a snippet: dropping it would lose
    # the only evidence there is about that result.
    assert "Official source: TJPA" in rendered
    assert "Search-engine snippet: Apelação cível" in rendered
    # And the source list keeps the read page once, as the read page.
    assert [source.reference for source in result["sources"]] == [
        STJ_RESULT.url, TJPA_RESULT.url,
    ]


# ── the query is for the law, not for the case ──────────────────────────────
# A lawyer states the facts first and asks the legal question last. Sent whole,
# the facts are what reaches the index: the party names, the amounts, the
# dates. Measured against the live API on 2026-09-16, the 304-character query
# production actually sent returned nothing at all, while 63 characters of
# legal terms returned a full page.


def test_a_word_is_never_cut_in_half():
    """The old cap cut at exactly 500 characters and landed mid-word
    ("Hackers explora"), asking the index for a fragment nothing contains."""
    assert serper.clip_to_words("Hackers exploraram essa falha", 18) == "Hackers"
    assert serper.clip_to_words("curta", 100) == "curta"
    # A single word longer than the limit still has to be cut somewhere.
    assert serper.clip_to_words("responsabilidade", 6) == "respon"


def test_the_parties_do_not_reach_the_search_engine(monkeypatch):
    """The names in a question are the firm's client, not a search term.

    They are left behind by asking a short question rather than by detecting
    names, which would erase courts and statutes along with them.
    """
    # The cap is deliberately NOT overridden here. This test is about the
    # number the firm actually runs with, so raising the default back to
    # something that lets a client's name through has to fail right here.
    monkeypatch.setattr(settings, "serper_allowed_domains", "jus.br")

    asked = (
        "Analisar a jurisprudência atual do Superior Tribunal de Justiça (STJ) "
        "sobre a validade da cláusula de limitação de responsabilidade em "
        "contratos empresariais (B2B) e identificar as hipóteses que autorizam "
        "a mitigação ou anulação dessa cláusula, aplicando ao caso da "
        "TechSegura S.A. e Banco DinheiroCerto."
    )

    query = serper.restricted_query(asked, "stj.jus.br")

    assert "TechSegura" not in query
    assert "DinheiroCerto" not in query
    assert "jurisprudência" in query, "the legal subject still has to survive"
    assert len(query) <= len("site:stj.jus.br ") + settings.serper_max_query_chars


class _Recorder:
    """A Serper that answers with whatever the test queued, and remembers."""

    is_closed = False

    def __init__(self, pages):
        self.pages = list(pages)
        self.queries = []

    async def post(self, _endpoint, **kwargs):
        self.queries.append(kwargs["json"]["q"])
        organic = self.pages.pop(0) if self.pages else []

        class Response:
            status_code = 200

            @staticmethod
            def json():
                return {"organic": organic}

        return Response()


_HIT = [{
    "title": "STJ",
    "link": "https://www.stj.jus.br/tema",
    "snippet": "Tema repetitivo.",
}]

_LONG = (
    "Analisar a jurisprudência atual do Superior Tribunal de Justiça sobre a "
    "validade da cláusula de limitação de responsabilidade em contratos "
    "empresariais e identificar as hipóteses que autorizam a mitigação"
)


def _one_domain(monkeypatch):
    monkeypatch.setattr(settings, "serper_api_key", "k")
    monkeypatch.setattr(settings, "serper_allowed_domains", "jus.br")
    monkeypatch.setattr(settings, "serper_max_queries", 1)
    monkeypatch.setattr(settings, "serper_max_query_chars", 120)
    monkeypatch.setattr(settings, "serper_retry_query_chars", 60)


def test_an_empty_result_is_asked_again_more_shortly(monkeypatch):
    """Nothing came back and nothing broke, so the question was too long."""
    _one_domain(monkeypatch)
    client = _Recorder([[], _HIT])
    monkeypatch.setattr(serper, "_client", client)

    results = asyncio.run(serper.search(_LONG))

    assert len(client.queries) == 2, "one retry, not none and not a loop"
    assert len(client.queries[1]) < len(client.queries[0])
    assert [r.url for r in results] == ["https://www.stj.jus.br/tema"]


def test_a_search_that_found_something_is_not_asked_again(monkeypatch):
    """A retry after partial results spends a call to improve an answer that
    already exists."""
    _one_domain(monkeypatch)
    client = _Recorder([_HIT])
    monkeypatch.setattr(serper, "_client", client)

    asyncio.run(serper.search(_LONG))

    assert len(client.queries) == 1


def test_a_query_already_short_is_not_asked_again(monkeypatch):
    """Shortening it would send the identical string a second time."""
    _one_domain(monkeypatch)
    client = _Recorder([[], _HIT])
    monkeypatch.setattr(serper, "_client", client)

    results = asyncio.run(serper.search("dano moral atraso de voo"))

    assert len(client.queries) == 1
    assert results == []
