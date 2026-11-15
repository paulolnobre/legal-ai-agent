"""An answer may not cite what it did not read.

The failure this exists for is specific: a process number or a súmula that
does not exist reads exactly like one that does, and it is copied out of a
chat into a petition. The check is arithmetic on strings, so the tests are
about two things only. That an invention is caught, and, at least as
important, that a real citation written differently from its source is *not*
called an invention, because a guard that cries wolf is one the firm learns
to ignore.
"""
import asyncio

import pytest

from core.services.legal_assistant import chat as chat_agent
from core.services.legal_assistant import citations, verification
from core.services.legal_assistant import graph as graph_module
from core.services.legal_assistant.graph import build_graph
from core.services.legal_assistant.state import LegalState

# A real CNJ number: 7 digits, check digits, year, court, origin. The check
# digits are computed, not invented, so the malformed test means something.
_REAL = "0710957-85.2024.8.14.0301"


def _state(**kwargs) -> LegalState:
    return LegalState(conn=None, question="q", **kwargs)


# ── what counts as a citation ────────────────────────────────────────────────


def test_the_three_kinds_the_firm_asked_for_are_recognised():
    found = citations.find(
        f"Conforme o art. 5º da Constituição e a Súmula 331 do TST, "
        f"o processo {_REAL} discute o tema."
    )

    assert [c.kind for c in found] == ["processo", "súmula", "artigo"]


def test_a_binding_precedent_is_told_apart_from_an_ordinary_one():
    """Súmula Vinculante 13 and Súmula 13 are different texts."""
    binding = citations.keys("Súmula Vinculante nº 13")
    ordinary = citations.keys("Súmula 13")

    assert binding != ordinary


def test_the_ordinal_form_survives_the_round_trip():
    """The message quotes the citation back, and a lawyer wrote `art. 5º`."""
    found = citations.find("art. 5o da CF")

    assert found[0].described() == "art. 5º da Constituição"


def test_an_article_number_above_nine_keeps_its_plain_form():
    assert citations.find("artigo 927")[0].described() == "art. 927"


def test_text_with_nothing_cited_produces_nothing():
    assert citations.find("Bom dia, tudo bem com você?") == []
    assert citations.find("") == []


# ── the same thing, written differently ──────────────────────────────────────


@pytest.mark.parametrize("in_answer, in_source", [
    ("art. 5º da Constituição", "Art. 5o, inciso II, da CONSTITUICAO Federal"),
    ("artigo 927 do Código Civil", "ARTIGO 927 do codigo civil"),
    ("Súmula 331 do TST", "Sumula 331, TST, responsabilidade subsidiaria"),
    ("art. 20-A da Lei 8.245/91", "art. 20-a da lei 8.245, de 1991"),
    (f"processo {_REAL}", f"Processo n. {_REAL.replace('-', '').replace('.', '')}"),
])
def test_a_real_citation_is_not_called_an_invention_over_spelling(in_answer, in_source):
    """Accents, case, punctuation and the bare-digit form all collapse to one
    key, because both sides go through the same extractor."""
    assert citations.unsupported(in_answer, [in_source]) == []


def test_an_answer_more_specific_than_its_source_is_still_supported():
    """The source says "Súmula 331"; the answer says which court. Being more
    precise than the material is not inventing."""
    assert citations.unsupported("Súmula 331 do TST", ["Súmula 331"]) == []


def test_naming_a_different_court_is_not_supported_by_the_bare_number():
    """The reverse does not hold: Súmula 331 of the TST is not Súmula 331 of
    the STJ, and the evidence naming one does not cover the other."""
    missing = citations.unsupported("Súmula 331 do STJ", ["Súmula 331 do TST"])

    assert [c.key for c in missing] == ["súmula:331:stj"]


# ── what gets caught ─────────────────────────────────────────────────────────


def test_a_citation_absent_from_every_source_is_reported():
    missing = citations.unsupported(
        "Aplica-se a Súmula 9999 do STJ.", ["texto sobre locação e despejo"],
    )

    assert [c.described() for c in missing] == ["Súmula 9999 do STJ"]


def test_one_invention_repeated_is_reported_once():
    missing = citations.unsupported(
        "A Súmula 9999 decide. Reforçando, a Súmula 9999 é clara. Ver Súmula 9999.",
        [],
    )

    assert len(missing) == 1


def test_a_process_number_that_fails_its_check_digits_is_caught():
    """Resolução CNJ 65/2008. An invented number almost always fails this, and
    it is wrong even when a source happens to carry it."""
    invented = "0001234-56.2024.8.14.0301"

    assert [c.key for c in citations.malformed_process_numbers(invented)]
    assert citations.malformed_process_numbers(_REAL) == []


# ── the node ─────────────────────────────────────────────────────────────────


def test_an_answer_citing_what_it_did_not_read_is_not_sent():
    result = verification.verify(_state(
        answer="Conforme a Súmula 9999 do STJ, o pedido procede.",
        evidence=["um texto que não menciona súmula alguma"],
    ))

    assert "Súmula 9999 do STJ" in result["answer"]
    assert "não vou enviar" in result["answer"].casefold()
    assert "pedido procede" not in result["answer"], "the answer itself is withheld"


def test_a_refused_answer_is_not_cached_and_keeps_no_sources():
    """Offering the bibliography of a refused answer invites reading it as
    though it had been sent."""
    result = verification.verify(_state(
        answer="Súmula 9999 resolve.", evidence=["nada disso"],
        sources=[], cacheable=True,
    ))

    assert result["cacheable"] is False
    assert result["sources"] == []


def test_an_answer_whose_citations_are_all_in_the_evidence_passes_untouched():
    answer = "O art. 927 do Código Civil impõe o dever de indenizar."

    assert verification.verify(_state(answer=answer, evidence=[
        "Art. 927. Aquele que, por ato ilícito, causar dano a outrem...",
    ])) == {}


def test_an_answer_built_on_no_evidence_is_left_alone():
    """A greeting, or a draft written from the model's own knowledge, has
    nothing to compare against. Refusing every article it mentions would make
    those flows unusable and prove nothing."""
    assert verification.verify(_state(
        answer="Bom dia! Posso ajudar com o art. 5º da Constituição.", evidence=[],
    )) == {}


def test_a_cached_answer_is_not_checked_again():
    """It was checked when it was produced, and the evidence does not travel
    in the cache: comparing against an empty set would refuse everything that
    had already been cleared.

    The planted defect is a malformed process number rather than an unsupported
    citation, because that check runs before the one that needs evidence. With
    an empty-evidence answer the test would pass even without the cache guard,
    and prove nothing. A surviving mutation is what showed that.
    """
    assert verification.verify(_state(
        answer="Veja o processo 0001234-56.2024.8.14.0301.", evidence=[], cached=True,
    )) == {}


def test_a_malformed_process_number_is_refused_on_its_own_terms():
    result = verification.verify(_state(
        answer="Veja o processo 0001234-56.2024.8.14.0301.",
        evidence=["o processo 0001234-56.2024.8.14.0301 consta dos autos"],
    ))

    assert "dígito verificador" in result["answer"]


# ── the evidence is what the model saw ───────────────────────────────────────


def test_the_question_is_not_evidence():
    """Otherwise a lawyer who names a súmula in the question proves it exists
    by asking about it."""
    content = [
        {"type": "document", "title": "Lei",
         "source": {"type": "content", "content": [{"type": "text", "text": "art. 12"}]}},
        {"type": "text", "text": "a Súmula 9999 se aplica?"},
    ]

    collected = verification.evidence_from(content)

    assert any("art. 12" in block for block in collected)
    assert not any("9999" in block for block in collected)


def test_the_document_title_counts_as_evidence():
    """A law is often cited by the name the block carries, not by its body."""
    content = [{"type": "document", "title": "Lei 8.245/91 (locação)",
                "source": {"type": "content", "content": [{"type": "text", "text": "x"}]}}]

    assert "Lei 8.245/91 (locação)" in verification.evidence_from(content)


# ── the wiring, which is the part that rots ──────────────────────────────────


def test_every_agent_that_answers_goes_through_the_check():
    """The reason the node is in the graph rather than inside each agent: a
    new agent wired to END directly would ship unchecked, and nothing else
    would notice."""
    graph = build_graph().get_graph()
    answering = {"answer_internal", "research_external", "review", "draft", "chat"}

    straight_to_end = {
        edge.source for edge in graph.edges
        if edge.target == "__end__" and edge.source in answering
    }

    assert not straight_to_end, (
        f"{sorted(straight_to_end)} reach the end without the citation check. "
        "Route the node to 'verify' instead of END."
    )


def test_the_check_runs_for_real_inside_the_graph(monkeypatch):
    """A node being wired is not the same as it running. This drives the
    compiled graph, with one agent made to invent, and asserts the invention
    never reaches the reply.

    The router is replaced along with the agent. Left real it calls a model,
    which on a machine with no key falls back to the corpus, and the planted
    agent never runs: the first version of this test passed locally and failed
    in CI for exactly that reason, measuring the environment rather than the
    graph.
    """
    async def inventing_chat(_state):
        return {
            "answer": "A Súmula 9999 do STJ decide a questão.",
            "evidence": ["texto sem súmula nenhuma"],
        }

    async def route_to_chat(_state):
        return {"task": "chat", "query": "oi"}

    monkeypatch.setattr(graph_module, "orchestrate", route_to_chat)
    monkeypatch.setattr(chat_agent, "chat", inventing_chat)
    graph = build_graph()  # built after the patches, so the nodes are the fakes

    result = asyncio.run(graph.ainvoke(_state(task="chat")))

    assert "9999" in result["answer"]
    assert "não vou enviar" in result["answer"].casefold()
    assert "decide a questão" not in result["answer"]
