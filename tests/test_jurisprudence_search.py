"""Asking for case law searches where case law is, not where portals are.

A lawyer asked for jurisprudence on the alimony trinomial and received a
tribunal newsletter, a page explaining what a judicial decision is, and a CNJ
resolution about open data. Nothing was broken: the search did what it was
told, and what it was told cannot find a decision. site:jus.br ranks
institutional pages, because a decision lives behind a query form rather than
on an indexed page.

The routing below was measured against the live index, not assumed. The
obvious guess — jurisprudencia.stf.jus.br — returns nothing at all, while
scon.stj.jus.br returns inteiro-teor PDFs.
"""
import pytest

from core.config import settings
from core.integrations import serper
from core.services.legal_assistant import external_research


@pytest.fixture(autouse=True)
def official_domains(monkeypatch):
    monkeypatch.setattr(
        settings, "serper_allowed_domains", "jus.br,gov.br,leg.br,mp.br,def.br",
    )
    monkeypatch.setattr(settings, "serper_max_queries", 3)


# ── recognizing the question ────────────────────────────────────────────────

@pytest.mark.parametrize("question", [
    "pesquisa de jurisprudência sobre pensão alimentícia",
    "quais os acórdãos recentes sobre guarda compartilhada",
    "Quero decisões de tribunais sobre esse tema",
    "qual o entendimento do STJ sobre alimentos",
    "tem súmula sobre isso?",
    "me traz o inteiro teor do REsp",
])
def test_a_request_for_decisions_is_recognized(question):
    assert serper.wants_case_law(question)


@pytest.mark.parametrize("question", [
    "o que diz o artigo 1694 do código civil",
    "qual o prazo para contestar",
    "texto da lei 8.078/90",
])
def test_a_request_for_a_rule_is_not_treated_as_case_law(question):
    """Statute questions still go to the umbrella domains, which answer them."""
    assert not serper.wants_case_law(question)


# ── where it searches ───────────────────────────────────────────────────────

def test_case_law_without_a_named_court_goes_to_the_decision_indexes():
    domains = serper.search_domains("jurisprudência sobre o trinômio alimentar")

    assert domains[0] == "scon.stj.jus.br", "STJ's inteiro-teor index comes first"
    assert "stf.jus.br" in domains
    assert domains[-1] == "jus.br", "the umbrella is the catch-all, not the target"


def test_a_named_state_court_is_searched_alone():
    """Measured: tjmg.jus.br returns espelhos do acórdão with a clean query."""
    assert serper.search_domains("acórdãos do TJMG sobre alimentos") == ("tjmg.jus.br",)


def test_a_rule_question_keeps_the_previous_routing():
    domains = serper.search_domains("o que diz o artigo 1694 do código civil")

    assert domains == ("jus.br", "gov.br", "leg.br")


# ── what it searches for ────────────────────────────────────────────────────

def test_the_conversational_wrapper_is_removed_from_the_query():
    asked = (
        "Você pode fazer uma pesquisa de jurisprudência sobre o seguinte tema: "
        "pensão alimentícia deve observar o trinômio : possibilidade x "
        "necessidade x proporcionalidade . Não invente nem crie decisões"
    )

    terms = serper.search_terms(asked)

    assert terms.startswith("pensão alimentícia")
    assert "Você pode" not in terms
    assert "Não invente" not in terms
    # The legal terms themselves are never rewritten.
    for kept in ("trinômio", "possibilidade", "necessidade", "proporcionalidade"):
        assert kept in terms


def test_a_question_with_no_wrapper_is_left_alone():
    asked = "pensão alimentícia trinômio necessidade possibilidade"

    assert serper.search_terms(asked) == asked


def test_stripping_never_empties_the_query():
    """A question that is nothing but a request must still search for something."""
    assert serper.search_terms("faça uma nova pesquisa, por favor").strip()


def test_the_site_restriction_carries_the_cleaned_terms():
    query = serper.restricted_query(
        "Você pode fazer uma pesquisa sobre o seguinte tema: alimentos compensatórios",
        "scon.stj.jus.br",
    )

    assert query.startswith("site:scon.stj.jus.br ")
    assert "Você pode" not in query
    assert "alimentos compensatórios" in query


# ── which pages get opened ──────────────────────────────────────────────────

class _Source:
    def __init__(self, reference, title=""):
        self.reference = reference
        self.title = title


def test_decisions_are_read_before_institutional_pages():
    """Only the first few results are opened, so the order decides the answer."""
    results = [
        _Source("https://www.trt4.jus.br/portais/trt4/entenda-seu-processo", "Entenda"),
        _Source("https://portal.stf.jus.br/textos/verTexto.asp", "Entenda - decisões"),
        _Source(
            "https://scon.stj.jus.br/SCON/GetInteiroTeorDoAcordao?num_registro=202502725980",
            "RECURSO ESPECIAL",
        ),
        _Source(
            "https://www5.tjmg.jus.br/jurisprudencia/relatorioEspelhoAcordao.do",
            "Tribunal de Justiça de Minas Gerais",
        ),
    ]

    ordered = external_research._reading_order(results)

    assert "GetInteiroTeorDoAcordao" in ordered[0].reference
    assert "relatorioEspelhoAcordao" in ordered[1].reference
    assert len(ordered) == len(results), "nothing is dropped, only reordered"


def test_reading_order_is_stable_when_nothing_looks_like_a_decision():
    results = [_Source("https://www.cnj.jus.br/a"), _Source("https://www.cnj.jus.br/b")]

    ordered = external_research._reading_order(results)

    assert [r.reference for r in ordered] == [r.reference for r in results]


# ── the DJEN, asked for by name ─────────────────────────────────────────────

def test_the_djen_is_searched_at_the_cnj():
    assert "cnj.jus.br" in serper.search_domains("consulta no DJEN")
    assert "cnj.jus.br" in serper.search_domains("diário de justiça eletrônico nacional")
