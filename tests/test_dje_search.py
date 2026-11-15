"""The diary search: DataJud confirms the process, then its court's diary is searched.

Requested on 2026-09-11 for the TJPA: DataJud locates and identifies a process,
and an external search then looks for its publications in the court's Diário da
Justiça Eletrônico, with Serper first and Exa when Serper cannot find them.

It is the one place a process number is sent to a search engine, so most of
what follows is about keeping that narrow: only a number DataJud confirmed,
only the number in quotes, only the diary's own domain, and only while the
switch is on. The rest is about honesty: an excerpt that does not show the
number is not evidence about the process, and finding nothing is not proof that
nothing was published.
"""
import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from core.config import settings
from core.integrations import datajud, dje, exa, process_links, serper
from core.integrations.serper import SearchResult
from core.services.legal_assistant import external_research
from core.services.legal_assistant.state import LegalState

TJPA = "0801234-56.2023.8.14.0301"
TJSP = "1001234-56.2023.8.26.0100"
SITE = "dje.tjpa.jus.br"
EDITION = (
    "https://dje.tjpa.jus.br/DJEletronico/rest/DJEletronicoService/"
    "publicacao/buscarDiarioAssinado/8126-2025"
)


def _hit(url: str = EDITION, snippet: str = f"Processo nº {TJPA}. INTIMAÇÃO das partes") -> SearchResult:
    return SearchResult(title="Diário da Justiça Eletrônico", url=url, snippet=snippet)


@pytest.fixture
def providers(monkeypatch):
    """Serper and Exa's site searches, recorded rather than called."""
    state = SimpleNamespace(
        serper_calls=[], exa_calls=[], serper_found=[], exa_found=[],
        serper_error=None, exa_error=None,
    )

    async def serper_site(query, domain):
        state.serper_calls.append((query, domain))
        if state.serper_error:
            raise serper.SerperError(state.serper_error)
        return state.serper_found

    async def exa_site(query, domain):
        state.exa_calls.append((query, domain))
        if state.exa_error:
            raise exa.ExaError(state.exa_error)
        return state.exa_found

    monkeypatch.setattr(dje.serper, "search_site", serper_site)
    monkeypatch.setattr(dje.exa, "search_site", exa_site)
    monkeypatch.setattr(settings, "exa_api_key", "exa-test-key")
    return state


# ── which diary ─────────────────────────────────────────────────────────────

def test_a_para_process_is_searched_in_the_tjpa_diary():
    assert dje.site_for(TJPA) == SITE
    assert dje.site_for(TJSP) is None, "a court with no measured diary is not searched"


# ── what leaves, and the fallback ───────────────────────────────────────────

def test_only_the_quoted_number_is_sent_and_only_to_the_diary(providers):
    """No word of the question travels with the number, and no other domain."""
    providers.serper_found = [_hit()]

    found = asyncio.run(dje.search_publications(TJPA))

    assert providers.serper_calls == [(f'"{TJPA}"', SITE)]
    assert found is not None and found.provider == "serper"
    assert providers.exa_calls == [], "Exa is the fallback, not a second search"


def test_exa_is_asked_when_serper_finds_nothing(providers):
    providers.exa_found = [_hit()]

    found = asyncio.run(dje.search_publications(TJPA))

    assert found is not None and found.provider == "exa"
    assert providers.exa_calls == [(f'"{TJPA}"', SITE)]
    assert any("Serper" in problem for problem in found.problems)


def test_exa_is_asked_when_serper_fails(providers):
    providers.serper_error = "Serper did not respond"
    providers.exa_found = [_hit()]

    found = asyncio.run(dje.search_publications(TJPA))

    assert found is not None and found.provider == "exa"


def test_finding_nothing_is_reported_as_what_it_is(providers):
    found = asyncio.run(dje.search_publications(TJPA))

    assert found is not None
    assert found.results == [] and found.provider is None
    assert len(found.problems) == 2


def test_without_an_exa_key_the_fallback_is_not_attempted(providers, monkeypatch):
    monkeypatch.setattr(settings, "exa_api_key", "")

    found = asyncio.run(dje.search_publications(TJPA))

    assert providers.exa_calls == []
    assert found is not None and any("não está configurado" in p for p in found.problems)


def test_a_court_without_a_diary_is_not_searched_at_all(providers):
    assert asyncio.run(dje.search_publications(TJSP)) is None
    assert providers.serper_calls == [] and providers.exa_calls == []


# ── whether an excerpt is evidence ──────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    (f"Intimação no processo {TJPA}", True),
    ("Intimação no processo 08012345620238140301", True),
    ("Intimação no processo 0809999-11.2022.8.14.0301", False),
    ("Edição do diário sem números de processo", False),
    ("", False),
])
def test_an_excerpt_is_evidence_only_when_it_shows_the_number(text, expected):
    assert dje.mentions(TJPA, text) is expected


# ── each provider accepts only the diary's own URLs ─────────────────────────

def test_serper_site_search_accepts_only_urls_on_the_diary(monkeypatch):
    """jus.br is in the allowlist, but a portal page is not a diary edition."""
    sent = []

    async def search_domain(query, domain, key, domains):
        sent.append((query, domain))
        return [
            {"link": EDITION, "title": "Diário", "snippet": TJPA},
            {"link": "https://www.tjpa.jus.br/PortalExterno/", "title": "Portal"},
            {"link": "http://dje.tjpa.jus.br/insecure", "title": "HTTP"},
            {"link": "https://dje.tjpa.jus.br.example.com/x", "title": "Impostor"},
        ]

    monkeypatch.setattr(settings, "serper_api_key", "serper-test-key")
    monkeypatch.setattr(serper, "_search_domain", search_domain)

    results = asyncio.run(serper.search_site(f'"{TJPA}"', SITE))

    assert [r.url for r in results] == [EDITION]
    assert sent == [(f'"{TJPA}"', SITE)]


def test_exa_site_search_is_scoped_and_verified(monkeypatch):
    payloads = []

    async def request(key, payload, started):
        payloads.append(payload)
        return [
            {"url": EDITION, "title": "Diário", "highlights": [TJPA]},
            {"url": "https://www.tjpa.jus.br/PortalExterno/", "title": "Portal"},
        ]

    monkeypatch.setattr(settings, "exa_api_key", "exa-test-key")
    monkeypatch.setattr(exa, "_request", request)

    results = asyncio.run(exa.search_site(f'"{TJPA}"', SITE))

    assert [r.url for r in results] == [EDITION]
    assert payloads[0]["includeDomains"] == [SITE]
    assert payloads[0]["query"] == f'"{TJPA}"'


def test_exa_refuses_a_domain_outside_the_allowlist(monkeypatch):
    called = []

    async def request(*_args):
        called.append(True)
        return []

    monkeypatch.setattr(settings, "exa_api_key", "exa-test-key")
    monkeypatch.setattr(exa, "_request", request)

    with pytest.raises(exa.ExaError):
        asyncio.run(exa.search_site('"x"', "example.com"))
    assert called == [], "the request must not leave for an unauthorized domain"


# ── the research pipeline ───────────────────────────────────────────────────

@pytest.fixture
def pipeline(monkeypatch):
    """The external research with every outside call recorded."""
    state = SimpleNamespace(
        identified=[], diary=None, searched=[], web_queries=[],
        system="", content=[], action=None,
    )

    async def datajud_context(_text):
        return [], [], [], state.identified

    async def web_search(query, _provider=None):
        state.web_queries.append(query)
        return []

    async def search_publications(number):
        state.searched.append(number)
        return state.diary

    async def pages(_sources):
        return [], [], []

    async def complete(system, content, **_kwargs):
        state.system = system
        state.content = content
        return "resposta"

    async def log_access(_conn, **kwargs):
        state.action = kwargs.get("action")

    monkeypatch.setattr(external_research, "_datajud_context", datajud_context)
    monkeypatch.setattr(external_research.websearch, "search", web_search)
    monkeypatch.setattr(external_research.dje, "search_publications", search_publications)
    monkeypatch.setattr(external_research, "_read_official_pages", pages)
    monkeypatch.setattr(external_research, "complete", complete)
    monkeypatch.setattr(external_research, "log_access", log_access)
    monkeypatch.setattr(settings, "dje_search_enabled", True)
    return state


def _ask(question: str) -> dict:
    return asyncio.run(external_research.research(LegalState(
        conn=None, question=question, query=question, task="external_research",
    )))


def _texts(content) -> str:
    parts = []
    for block in content:
        if block.get("type") == "text":
            parts.append(block["text"])
        else:
            parts.append(block.get("title", ""))
            parts.extend(c.get("text", "") for c in block["source"]["content"])
    return "\n".join(parts)


def test_a_number_datajud_did_not_confirm_never_reaches_a_search_engine(pipeline):
    """A mistyped number, or one DataJud does not know, is not sent anywhere."""
    pipeline.identified = []

    _ask(f"quais as publicações do processo {TJPA}?")

    assert pipeline.searched == []
    assert pipeline.action and not pipeline.action.endswith("_dje")


def test_a_confirmed_number_reaches_the_diary_and_the_answer(pipeline):
    pipeline.identified = [TJPA]
    pipeline.diary = dje.DiarySearch(TJPA, SITE, [_hit()], "serper")

    result = _ask(f"quais as publicações do processo {TJPA}?")

    assert pipeline.searched == [TJPA]
    everything = _texts(pipeline.content)
    assert f"DJe {SITE}" in everything
    assert "mostra o número do processo" in everything
    assert "DJe" in pipeline.system, "the model is told what these blocks are"
    assert EDITION in [source.reference for source in result["sources"]]
    assert "Diário da Justiça Eletrônico" in result["answer"]
    assert pipeline.action.endswith("_dje"), "the audit says the number left"


def test_an_excerpt_without_the_number_is_flagged_to_the_model(pipeline):
    pipeline.identified = [TJPA]
    pipeline.diary = dje.DiarySearch(
        TJPA, SITE, [_hit(snippet="Edição 8126 do diário, página 1")], "exa",
    )

    _ask(f"publicações do processo {TJPA}")

    assert "NÃO mostra o número do processo" in _texts(pipeline.content)


def test_finding_nothing_becomes_a_limitation_that_forbids_the_conclusion(pipeline):
    pipeline.identified = [TJPA]
    pipeline.diary = dje.DiarySearch(
        TJPA, SITE, [], None, ["Serper não encontrou publicações"],
    )

    result = _ask(f"publicações do processo {TJPA}")

    assert any("não prova" in limitation for limitation in result["limitations"])
    assert pipeline.action.endswith("_dje"), "a search that found nothing still happened"


def test_the_switch_turns_the_diary_search_off(pipeline, monkeypatch):
    monkeypatch.setattr(settings, "dje_search_enabled", False)
    pipeline.identified = [TJPA]

    _ask(f"publicações do processo {TJPA}")

    assert pipeline.searched == []
    assert not pipeline.action.endswith("_dje")


def test_the_general_web_search_still_never_carries_the_number(pipeline):
    """The exception is the diary search alone. The ordinary web query keeps
    removing the number, as it always has."""
    pipeline.identified = [TJPA]

    _ask(f"prescrição intercorrente no processo {TJPA}")

    assert pipeline.web_queries, "the topic still goes to the web search"
    assert all("0801234" not in query for query in pipeline.web_queries)


# ── where a person reads the diary ──────────────────────────────────────────

def test_the_process_description_points_a_tjpa_case_at_its_diary():
    assert "https://dje.tjpa.jus.br/ClientDJEletronico/app/home.html" in process_links.describe(TJPA)
    assert "DJe" not in process_links.describe(TJSP)


def test_naming_the_tjpa_diary_in_a_question_searches_it():
    assert SITE in serper.search_domains("publicações no dje do tjpa sobre locação")


# ── what "confirmed" means ──────────────────────────────────────────────────

@pytest.mark.parametrize("records, confirmed", [
    ((), False),
    (({"numeroProcesso": "08012345620238140301"},), True),
])
def test_confirmed_means_datajud_found_the_process(monkeypatch, records, confirmed):
    """Found, not merely asked.

    Every pipeline test above stubs _datajud_context, so none of them saw the
    rule this function actually applied: a lookup that succeeded with no record
    counted as confirmed, and a mistyped number would have been sent to Serper
    and Exa. This one calls the real function, with only DataJud's reply faked.
    """
    async def lookup(number):
        return datajud.DataJudLookup(
            process_number=number,
            court_alias="tjpa",
            endpoint="https://api-publica.datajud.cnj.jus.br/api_publica_tjpa/_search",
            records=records,
            consulted_at=datetime.now(UTC),
        )

    monkeypatch.setattr(external_research.datajud, "lookup_process", lookup)

    *_, identified = asyncio.run(external_research._datajud_context(f"processo {TJPA}"))

    assert bool(identified) is confirmed
