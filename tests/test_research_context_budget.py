"""The external-research context has a size budget, and something gives way.

Every individual source was already capped on its own (DataJud, DJEN, DJe,
the web search, and up to three fully-read pages), but nothing capped their
sum, and together they can pass 90,000 characters on one question. Stage 4
calls this "an external-research planner with a budget and stop rules"; this
is its first slice: never send an unbounded context, and when something has
to be left out, leave out the weakest evidence first, never the strongest,
and never by cutting into the middle of a block.
"""
import asyncio
from types import SimpleNamespace

import pytest

from core.config import settings
from core.services.legal_assistant import external_research as er
from core.services.legal_assistant.state import LegalState


def _doc(chars: int, label: str = "x") -> dict:
    """A document block whose text is exactly `chars` characters long, so the
    arithmetic in a test is exact regardless of how long `label` itself is."""
    return er.document_block(label, "x" * chars)


def _chars(blocks: list[dict]) -> int:
    return sum(er._block_chars(b) for b in blocks)


# ── _bounded_evidence, the pure function ─────────────────────────────────────

def test_under_budget_nothing_is_dropped_and_order_is_priority_order():
    groups = {
        "acervo": [_doc(100, "a")],
        "DataJud": [_doc(100, "d")],
        "DJEN": [],
        "páginas lidas": [_doc(100, "p")],
        "DJe": [],
        "busca web": [_doc(100, "w")],
    }

    kept, limitations = er._bounded_evidence(groups, max_chars=10_000)

    assert [b["title"] for b in kept] == ["a", "d", "p", "w"]
    assert limitations == []


def test_the_weakest_evidence_is_dropped_first():
    """busca web is the lowest priority, DataJud and acervo the highest: the
    weak search-index snippet gives way before a compact official source does."""
    groups = {
        "acervo": [_doc(10, "a")],
        "DataJud": [_doc(10, "d")],
        "DJEN": [],
        "páginas lidas": [],
        "DJe": [],
        "busca web": [_doc(500, "w")],
    }

    kept, limitations = er._bounded_evidence(groups, max_chars=25)

    assert [b["title"] for b in kept] == ["a", "d"]
    assert len(limitations) == 1
    assert "busca web" in limitations[0]
    assert "1 bloco" in limitations[0]


def test_stops_as_soon_as_it_fits_and_does_not_over_drop():
    groups = {
        "acervo": [],
        "DataJud": [],
        "DJEN": [],
        "páginas lidas": [],
        "DJe": [],
        "busca web": [_doc(10, "w1"), _doc(10, "w2"), _doc(10, "w3")],
    }

    kept, limitations = er._bounded_evidence(groups, max_chars=25)

    # 3 blocks of 10 chars = 30 > 25; dropping the last one (10) leaves 20 <= 25,
    # so exactly one is dropped, not all three.
    assert [b["title"] for b in kept] == ["w1", "w2"]
    assert "1 bloco" in limitations[0]


def test_the_limitation_counts_every_block_dropped_not_just_one():
    groups = {
        "acervo": [], "DataJud": [], "DJEN": [], "páginas lidas": [], "DJe": [],
        "busca web": [_doc(10, "w1"), _doc(10, "w2"), _doc(10, "w3"), _doc(10, "w4")],
    }

    kept, limitations = er._bounded_evidence(groups, max_chars=15)

    assert [b["title"] for b in kept] == ["w1"]
    assert len(limitations) == 1
    assert "3 bloco(s)" in limitations[0]


def test_within_a_group_the_tail_is_dropped_first():
    """A fetched page arrives in _reading_order: decisions first. The one
    dropped when 'páginas lidas' must give ground is the one already ranked
    worst, never the one the ranking put first."""
    groups = {
        "acervo": [],
        "DataJud": [],
        "DJEN": [],
        "páginas lidas": [_doc(10, "best decision"), _doc(500, "worst institutional page")],
        "DJe": [],
        "busca web": [],
    }

    kept, _ = er._bounded_evidence(groups, max_chars=15)

    assert [b["title"] for b in kept] == ["best decision"]


def test_djen_is_protected_ahead_of_fetched_pages_and_dje():
    """DJEN is official CNJ text with no 'may not be extrapolated' caveat,
    ranked above a fetched page from the open web and above a DJe snippet."""
    groups = {
        "acervo": [],
        "DataJud": [],
        "DJEN": [_doc(10, "djen")],
        "páginas lidas": [_doc(500, "page")],
        "DJe": [_doc(500, "dje")],
        "busca web": [],
    }

    kept, _ = er._bounded_evidence(groups, max_chars=15)

    assert [b["title"] for b in kept] == ["djen"]


def test_acervo_and_datajud_are_the_last_resort():
    """Even the firm's own archive gives way before the budget is refused
    outright, but only after every external source is already gone."""
    groups = {
        "acervo": [_doc(10, "own document")],
        "DataJud": [_doc(10, "datajud")],
        "DJEN": [_doc(500, "djen")],
        "páginas lidas": [_doc(500, "page")],
        "DJe": [_doc(500, "dje")],
        "busca web": [_doc(500, "web")],
    }

    kept, limitations = er._bounded_evidence(groups, max_chars=20)

    assert [b["title"] for b in kept] == ["own document", "datajud"]
    dropped_labels = {label for lim in limitations for label in er._EVIDENCE_PRIORITY if label in lim}
    assert dropped_labels == {"DJEN", "páginas lidas", "DJe", "busca web"}


def test_a_plain_text_block_is_sized_by_its_own_repr_not_crashed_on():
    """The acervo note is a {'type': 'text', ...} block, not a document block:
    _block_chars must not raise on it, only fall back to something safe."""
    note = {"type": "text", "text": "x" * 50}
    groups = {
        "acervo": [_doc(10, "doc"), note],
        "DataJud": [], "DJEN": [], "páginas lidas": [], "DJe": [], "busca web": [],
    }

    kept, limitations = er._bounded_evidence(groups, max_chars=10_000)

    assert kept == [_doc(10, "doc"), note]
    assert limitations == []


# ── wired into research() ────────────────────────────────────────────────────

HUGE = "EMENTA. " * 20_000  # far past external_research_max_context_chars on its own


@pytest.fixture
def pipeline(monkeypatch):
    """research() with every source faked, sized by the test."""
    state = SimpleNamespace(
        web_results=[], fetched=[], djen=[], system="", content=[], answer=None,
    )

    async def no_datajud(_text):
        return [], [], [], []

    async def no_djen(_text):
        return [], [], []

    async def web_search(_query, provider=None):
        return list(state.web_results)

    async def read_pages(sources):
        return list(state.fetched), [], []

    async def complete(system, content, **_kwargs):
        state.system, state.content = system, content
        return "resposta"

    async def log_access(*_args, **_kwargs):
        return None

    monkeypatch.setattr(settings, "web_search_provider", "serper")
    monkeypatch.setattr(er, "_datajud_context", no_datajud)
    monkeypatch.setattr(er, "_djen_context", no_djen)
    monkeypatch.setattr(er.websearch, "search", web_search)
    monkeypatch.setattr(er, "_read_official_pages", read_pages)
    monkeypatch.setattr(er, "complete", complete)
    monkeypatch.setattr(er, "log_access", log_access)
    return state


def _ask():
    return asyncio.run(er.research(LegalState(
        conn=None,
        question="Qual é o entendimento do STJ sobre prescrição?",
        query="entendimento STJ prescrição",
        task="external_research",
    )))


def test_an_ordinary_question_is_never_trimmed(pipeline):
    """The common case: a couple of small results, comfortably under budget.
    No trimming, and no size-related limitation appears: proves the budget
    does not change today's behaviour for a normal question."""
    from core.integrations import serper as serper_module

    pipeline.web_results = [serper_module.SearchResult(
        title="STJ", url="https://www.stj.jus.br/tema", snippet="Tema repetitivo.",
    )]

    result = _ask()

    assert not any("limite de tamanho do contexto" in lim for lim in result["limitations"])
    assert "Official source" in str(pipeline.content)


def test_an_oversized_result_set_is_trimmed_and_reported(pipeline, caplog):
    import logging

    from core.integrations import serper as serper_module

    # Ten results, each with a huge snippet, comfortably past the ceiling.
    pipeline.web_results = [
        serper_module.SearchResult(
            title=f"Resultado {i}", url=f"https://www.stj.jus.br/{i}", snippet=HUGE,
        )
        for i in range(10)
    ]

    with caplog.at_level(logging.WARNING):
        result = _ask()

    assert any(
        "busca web" in lim and "limite de tamanho do contexto" in lim
        for lim in result["limitations"]
    )
    assert "context exceeded" in caplog.text
    # It answered anyway: a trimmed context is a limitation, not a failure.
    assert result["answer"].startswith("resposta")


def test_the_trimmed_context_actually_fits_the_budget(pipeline):
    from core.integrations import serper as serper_module

    pipeline.web_results = [
        serper_module.SearchResult(
            title=f"Resultado {i}", url=f"https://www.stj.jus.br/{i}", snippet=HUGE,
        )
        for i in range(10)
    ]

    _ask()

    document_blocks = [b for b in pipeline.content if b.get("type") == "document"]
    assert _chars(document_blocks) <= settings.external_research_max_context_chars
