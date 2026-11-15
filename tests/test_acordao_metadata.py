"""Reading the fields a decision is cited by, instead of letting them be written.

The pipeline already opened the inteiro teor and handed its text to the model.
That answers a question; it does not let anybody cite one. A citation is a
process number, a relator and a date, and those three are precisely what a
language model supplies from context when they are not in front of it. An
invented relator reads exactly like a real one, and it goes out over a lawyer's
name.

The tests below are less about the happy path than about the two ways this goes
wrong silently: reading the relator of a precedent the decision quotes, and
reporting the publication date as the judgment date.
"""
import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from core.config import settings
from core.integrations import datajud
from core.services.legal_assistant import acordao, external_research

# A real STJ header shape: fixed-width labels, the class and number on top, and
# the judgment date at the foot of the acórdão section.
STJ = """
Superior Tribunal de Justiça
RECURSO ESPECIAL Nº 1.234.567 - SP (2019/0123456-7)
RELATOR : MINISTRO LUIS FELIPE SALOMÃO
RECORRENTE : FULANO DE TAL
ADVOGADOS : SICRANO DE TAL E OUTROS
RECORRIDO : BANCO BELTRANO S/A
EMENTA
DIREITO CIVIL. ALIMENTOS. TRINÔMIO NECESSIDADE-POSSIBILIDADE-PROPORCIONALIDADE.
ACÓRDÃO
Vistos, relatados e discutidos os autos em que são partes as acima indicadas.
Brasília (DF), 12 de março de 2024 (Data do Julgamento)
"""

STF = """
RECURSO EXTRAORDINÁRIO 987.654 SÃO PAULO
RELATORA : MIN. CÁRMEN LÚCIA
RECTE.(S) : ESTADO DE SÃO PAULO
Julgamento: 05/09/2023      Publicação: DJe 20/09/2023
"""

TJ = """
APELAÇÃO CÍVEL Nº 0001234-56.2020.8.26.0100
Relator(a): Des. Maria Silva
Data do Julgamento: 3 de abril de 2025
DJe 10/04/2025
"""


# ── the three fields ────────────────────────────────────────────────────────

def test_the_stj_header_gives_all_three():
    found = acordao.extract(STJ)

    assert found.process_number == "RECURSO ESPECIAL Nº 1.234.567/SP"
    assert found.relator == "MINISTRO LUIS FELIPE SALOMÃO"
    assert found.judged_on == date(2024, 3, 12)


def test_the_originating_state_is_part_of_the_citation():
    """"REsp 1.234.567" alone is ambiguous. The state is how it is written."""
    number = acordao.extract(STJ).process_number
    assert number and number.endswith("/SP")


def test_a_cnj_number_comes_back_punctuated_the_way_it_is_cited():
    """Twenty bare digits is a form an API takes, not one a person can check."""
    assert acordao.extract(TJ).process_number == "0001234-56.2020.8.26.0100"


def test_the_relator_is_read_from_either_spelling():
    assert acordao.extract(STF).relator == "MIN. CÁRMEN LÚCIA"
    assert acordao.extract(TJ).relator == "Des. Maria Silva"


@pytest.mark.parametrize("text, expected", [
    (STJ, date(2024, 3, 12)),   # "12 de março de 2024"
    (STF, date(2023, 9, 5)),    # "05/09/2023"
    (TJ, date(2025, 4, 3)),     # "3 de abril de 2025"
])
def test_dates_are_read_in_both_the_written_and_numeric_forms(text, expected):
    assert acordao.extract(text).judged_on == expected


# ── the two silent failures ─────────────────────────────────────────────────

def test_the_relator_quoted_in_the_body_is_never_read_as_the_relator():
    """An acórdão cites precedents, and each one carries its own relator.

    Searching the whole document finds the last name mentioned rather than the
    one who wrote it, and nothing about the result looks wrong: it is a real
    minister, in a real decision, attached to the wrong case.
    """
    quoting = STJ + ("\n" * 5) + (
        "No mesmo sentido, esta Corte já decidiu:\n"
        "RECURSO ESPECIAL Nº 111.222 - RJ\n"
        "RELATOR : MINISTRO OUTRO NOME COMPLETAMENTE\n"
        "Brasília (DF), 1 de janeiro de 2010 (Data do Julgamento)\n"
    ) + ("texto do voto. " * 800)

    found = acordao.extract(quoting)

    assert found.relator == "MINISTRO LUIS FELIPE SALOMÃO"
    assert found.judged_on == date(2024, 3, 12)
    assert "111.222" not in (found.process_number or "")


def test_a_decision_that_names_no_relator_gets_none_from_a_quoted_one():
    """The case the header limit actually exists for.

    When the real header does carry a relator, it is found first and reading the
    whole document happens to give the same answer, which is why the test above
    passes with the limit removed. The failure is here: a page whose own header
    names nobody, quoting precedents that do. Without the limit, the first
    quoted minister becomes this decision's relator, and every field around it
    is correct, so nothing looks wrong.
    """
    header_without_a_relator = (
        "Tribunal de Justiça do Estado de São Paulo\n"
        "APELAÇÃO CÍVEL Nº 0001234-56.2020.8.26.0100\n"
        "APELANTE: FULANO\nAPELADO: BELTRANO\n"
    )
    padding = "texto do voto. " * 400          # pushes the quotation past the header
    quoted = (
        "Precedente: RECURSO ESPECIAL Nº 111.222 - RJ\n"
        "RELATOR : MINISTRO NOME DE OUTRO PROCESSO\n"
        "Data do Julgamento: 1 de janeiro de 2010\n"
    )
    found = acordao.extract(header_without_a_relator + padding + quoted)

    assert found.process_number == "0001234-56.2020.8.26.0100"
    assert found.relator is None, "a quoted relator is not this decision's relator"
    assert found.judged_on is None


def test_the_header_boundary_is_where_it_is_claimed_to_be():
    """The limit is an argument so it can be measured, not assumed."""
    body = "\nRELATOR : MINISTRO FORA DO CABEÇALHO"
    padded = ("palavra " * 25) + body   # 200 characters, then the field

    assert acordao.extract(padded, header_chars=100).relator is None
    assert acordao.extract(padded, header_chars=500).relator is not None


def test_the_publication_date_is_never_reported_as_the_judgment_date():
    """DJe is when it was published. They are different facts and the other
    side notices when the wrong one is cited."""
    found = acordao.extract(STF)

    assert found.judged_on == date(2023, 9, 5)
    assert found.published_on == date(2023, 9, 20)
    assert found.judged_on != found.published_on


# ── what it refuses to do ───────────────────────────────────────────────────

def test_a_field_that_is_not_there_stays_empty():
    """A guessed field is worse than a missing one: the missing one is visible."""
    found = acordao.extract("Uma página institucional sobre o que é um acórdão.")

    assert found.is_empty()
    assert found.process_number is None
    assert found.relator is None
    assert found.judged_on is None


def test_an_impossible_date_is_dropped_rather_than_rounded():
    found = acordao.extract("Data do Julgamento: 31 de fevereiro de 2024")
    assert found.judged_on is None


def test_nothing_is_read_from_an_empty_document():
    assert acordao.extract("").is_empty()


def test_the_description_names_every_field_it_reports():
    described = acordao.extract(TJ).describe()

    assert "Processo:" in described
    assert "Relator(a):" in described
    assert "Data do julgamento:" in described
    assert "DJe" in described, "the publication date is labelled as such"


# ── the formatter this needed ───────────────────────────────────────────────

def test_a_cnj_number_survives_the_round_trip():
    written = "0001234-56.2020.8.26.0100"
    assert datajud.format_process_number(written) == written
    assert datajud.format_process_number(
        datajud.normalize_process_number(written)
    ) == written


# ── the seam into the research pipeline ─────────────────────────────────────
# The extractor can be perfect and still reach nothing. These read the block and
# the footnote the pipeline actually builds.

class _Page:
    def __init__(self, text, url="https://scon.stj.jus.br/x.pdf", title="Inteiro teor"):
        self.text = text
        self.url = url
        self.title = title
        self.content_type = "application/pdf"
        self.truncated = False


def test_a_decision_reaches_the_model_with_its_fields_in_front_of_the_text():
    title, body, kind = external_research._page_block(_Page(STJ))

    assert "MINISTRO LUIS FELIPE SALOMÃO" in body
    assert "12/03/2024" in body
    assert body.index("Relator(a):") < body.index("URL:"), (
        "the verified fields belong before the raw text, not buried in it"
    )
    assert "não deve ser afirmado" in body, (
        "offering the fields still leaves inventing a missing one available; "
        "the block has to say so"
    )
    assert "verified" in kind
    assert "Decision" in title


def test_an_ordinary_page_is_not_dressed_up_as_a_decision():
    title, body, kind = external_research._page_block(
        _Page("Uma página institucional explicando o que é um acórdão.")
    )

    assert "Official page" in title
    assert "verified" not in kind
    assert "Cite apenas" not in body


def test_the_footnote_leads_with_what_makes_the_decision_citable():
    detail = external_research._page_detail(_Page(STJ))

    assert detail.startswith("Decisão aberta e lida")
    assert "RECURSO ESPECIAL Nº 1.234.567/SP" in detail
    assert "Início do conteúdo:" in detail


def test_an_ordinary_page_keeps_the_footnote_it_always_had():
    detail = external_research._page_detail(_Page("Página institucional qualquer."))

    assert detail.startswith("Página oficial aberta")


def test_the_pipeline_actually_uses_the_reader(monkeypatch):
    """The seam nothing above covers.

    Everything before this calls _page_block directly, so _read_official_pages
    could stop calling it and every one of those tests would stay green while
    no decision reached the model with a verified field again.

    The page sits on a TJ's portal: the STJ's SCON refuses automated reading,
    so the pipeline no longer tries it.
    """
    page = _Page(STJ, url="https://www.tjpa.jus.br/CMSPortal/VisualizarArquivo?idArquivo=1")

    async def fetch_pages(_urls):
        return [page]

    monkeypatch.setattr(settings, "fetch_enabled", True)
    monkeypatch.setattr(external_research.web_fetch, "fetch_pages", fetch_pages)

    result = SimpleNamespace(reference=page.url, title=page.title)
    blocks, found_sources, missed = asyncio.run(
        external_research._read_official_pages([result])
    )

    assert not missed
    text = blocks[0]["source"]["content"][0]["text"]
    assert "MINISTRO LUIS FELIPE SALOMÃO" in text, (
        "the decision reached the model without its relator"
    )
    assert "RECURSO ESPECIAL Nº 1.234.567/SP" in found_sources[0].detail



# ── court, ementa and the lawyers' format ───────────────────────────────────
# Asked for by the firm's lawyers on 2026-09-11: the court, the process, the
# relator, the judgment date, what was understood, and the decision transcribed.
# The fixtures copy the structure of two real TJPA acórdãos, one of each layout,
# with the parties replaced.

OLD_TJPA = """Poder Judiciário
Tribunal de Justiça do Estado do Pará
BELÉM
ACÓRDÃO Nº 187430
2ª. TURMA DE DIREITO PRIVADO
APELAÇÃO CÍVEL Nº 0010050-42.2016.8.14.0040
APELANTE: FULANO DE TAL
APELADO: SICRANA DE TAL
RELATORA: DESA. FULANA DE SOUZA
EMENTA: AÇÃO DE DIVÓRCIO. PARTILHA DE BENS. APELO PARCIALMENTE
PROVIDO À UNANIMIDADE.
1 - Restando incontroversa a aquisição dos imóveis, não há provas a produzir.
2 - Apelação parcialmente provida à unanimidade.
A  C  Ó  R  D  Ã  O
Vistos, relatados e discutidos,
Sessão Ordinária realizada em 13 de março de 2018, presidido pela Presidente.
"""

PJE_TJPA = """25/05/2022
Número: 0009782-32.2012.8.14.0006
Classe: APELAÇÃO CÍVEL
TJPA - 2º Grau
Documentos
Id. Data Documento Tipo
7972262 03/03/2022 15:21 Ementa Ementa
APELAÇÃO CÍVEL (198) - 0009782-32.2012.8.14.0006
RELATOR(A): Desembargador FULANO DE SOUZA
EMENTA

DIREITO CIVIL. AÇÃO DE GUARDA. SENTENÇA MANTIDA.

1. Primeiro item da ementa.
Num. 7972262 - Pág. 1
Assinado eletronicamente por: FULANO DE SOUZA - 03/03/2022 15:21:15
https://pje-consultas.tjpa.jus.br/pje-1g-consultas/Processo/ConsultaDocumento/listView.seam?x=2203
Número do documento: 22030315211549200000007753512
TRIBUNAL DE JUSTIÇA DO ESTADO DO PARÁ
2. Desprovimento do recurso, à unanimidade.

RELATÓRIO
Texto do relatório.
"""


def test_the_older_tjpa_layout_gives_every_field():
    found = acordao.extract(OLD_TJPA, url="https://www.tjpa.jus.br/CMSPortal/x")

    assert found.court == "TJPA"
    assert found.process_number == "0010050-42.2016.8.14.0040"
    assert found.relator == "DESA. FULANA DE SOUZA"
    assert found.judged_on == date(2018, 3, 13), "the session formula this layout uses"
    assert found.ementa == (
        "AÇÃO DE DIVÓRCIO. PARTILHA DE BENS. APELO PARCIALMENTE PROVIDO À UNANIMIDADE. "
        "1 - Restando incontroversa a aquisição dos imóveis, não há provas a produzir. "
        "2 - Apelação parcialmente provida à unanimidade."
    )
    assert found.ementa_truncated is False


def test_the_pje_ementa_comes_out_without_the_page_it_crossed():
    """Footer and running header both land mid-ementa when it crosses a page.

    Measured on the real acórdão this fixture copies: the ementa came out with
    the court's page header between two of its items. A transcription the lawyer
    signs as literal cannot carry it, nor a PJe link, nor a signature line.
    """
    found = acordao.extract(PJE_TJPA)

    assert found.ementa == (
        "DIREITO CIVIL. AÇÃO DE GUARDA. SENTENÇA MANTIDA. "
        "1. Primeiro item da ementa. 2. Desprovimento do recurso, à unanimidade."
    )


def test_the_table_of_documents_is_not_the_ementa():
    """The PJe layout lists "Ementa Ementa" above the real heading."""
    found = acordao.extract(PJE_TJPA)

    assert found.ementa and not found.ementa.startswith("Ementa")


def test_a_layout_that_states_no_judgment_date_gets_none_not_a_guess():
    """The PJe acórdão has the signature date and the date of the vote's closing
    line, and neither says it is the judgment date. Reporting one as the other is
    the error this module exists to prevent."""
    assert acordao.extract(PJE_TJPA).judged_on is None


@pytest.mark.parametrize("header, court", [
    ("Superior Tribunal de Justiça\nREsp 1", "STJ"),
    ("SUPREMO TRIBUNAL FEDERAL\nRE 1", "STF"),
    ("Tribunal de Justiça do Estado de São Paulo", "TJSP"),
    ("Tribunal de Justiça do Estado do Paraná", "TJPR"),
    ("Tribunal de Justiça do Estado de Mato Grosso do Sul", "TJMS"),
    ("Tribunal de Justiça do Estado de Mato Grosso", "TJMT"),
    ("Tribunal Regional Federal da 1ª Região", "TRF1"),
    ("TJPA - 2º Grau", "TJPA"),
])
def test_the_court_is_read_from_how_it_names_itself(header, court):
    assert acordao.extract(header).court == court


def test_the_cnj_number_decides_the_court_before_anything_else():
    """A header can name another court, quoting its precedent. The number
    encodes the court that judged and cannot be misread."""
    header = "Processo 0001234-56.2020.8.26.0100\nConforme o Superior Tribunal de Justiça"

    assert acordao.extract(header).court == "TJSP"


def test_a_court_named_inside_the_ementa_stays_in_the_transcription():
    """Only the running header, printed in capitals, is page furniture. The same
    name in mixed case on a line of its own is the ementa's sentence, broken
    where the PDF broke it."""
    text = (
        "RELATOR: FULANO DE SOUZA\n"
        "EMENTA: APELAÇÃO. Precedente do\n"
        "Tribunal de Justiça do Estado de São Paulo\n"
        "aplicado ao caso.\n"
        "ACÓRDÃO\n"
    )

    assert acordao.extract(text).ementa == (
        "APELAÇÃO. Precedente do Tribunal de Justiça do Estado de São Paulo aplicado ao caso."
    )


def test_page_furniture_is_dropped_whatever_its_case():
    text = "RELATOR: FULANO\nEMENTA: PRIMEIRA PARTE.\nnum. 12 - pág. 3\nSEGUNDA PARTE.\nACÓRDÃO\n"

    assert acordao.extract(text).ementa == "PRIMEIRA PARTE. SEGUNDA PARTE."


def test_the_address_is_the_last_resort_for_the_court():
    assert acordao.extract("RELATOR: FULANO", url="https://www.tjmg.jus.br/x").court == "TJMG"
    assert acordao.extract("RELATOR: FULANO").court is None


def test_an_ementa_whose_end_is_not_found_says_it_may_be_incomplete():
    found = acordao.extract("RELATOR: FULANO\nEMENTA: TEXTO QUE NÃO TERMINA NO TRECHO LIDO")

    assert found.ementa_truncated is True
    assert "possivelmente incompleta" in found.describe()


def test_an_ementa_deep_in_the_body_belongs_to_a_quoted_precedent():
    body = "RELATOR: FULANO\n" + ("texto do voto. " * 700) + "\nEMENTA: PRECEDENTE CITADO.\nACÓRDÃO\n"

    assert acordao.extract(body).ementa is None


def test_a_bill_with_an_ementa_is_not_a_decision():
    """A legislative page carries an EMENTA heading too. Only a relator, a
    process or a date marks a page as a decision."""
    assert acordao.extract("PROJETO DE LEI Nº 1/2026\nEMENTA: Altera a lei tal.").is_empty()


def test_the_footnote_carries_the_citation_not_the_whole_ementa():
    found = acordao.extract(OLD_TJPA)

    assert "Tribunal: TJPA" in found.citation()
    assert "AÇÃO DE DIVÓRCIO" not in found.citation()
    assert "Ementa (transcrição literal do documento):" in found.describe()


def test_the_decision_block_binds_the_quotation_to_the_ementa():
    title, body, _kind = external_research._page_block(_Page(OLD_TJPA, url="https://www.tjpa.jus.br/x"))

    assert "copiada sem alterar palavras" in body
    assert "AÇÃO DE DIVÓRCIO. PARTILHA DE BENS." in body
    assert "Tribunal: TJPA" in body
    assert "\u2014" not in title, "the firm asked for no em-dash in what it reads"


def test_the_blocked_scon_is_not_read_and_the_answer_is_told_why(monkeypatch):
    """The STJ answers automated reading with a bot challenge. Trying it spends
    the reading slots on pages that cannot open, while a TJ's acórdão waits."""
    requested = []

    async def fetch_pages(urls):
        requested.extend(urls)
        return [_Page(OLD_TJPA, url=urls[0])] if urls else []

    monkeypatch.setattr(settings, "fetch_enabled", True)
    monkeypatch.setattr(external_research.web_fetch, "fetch_pages", fetch_pages)
    scon = SimpleNamespace(reference="https://scon.stj.jus.br/SCON/GetInteiroTeorDoAcordao?num_registro=1", title="REsp")
    tj = SimpleNamespace(reference="https://www.tjpa.jus.br/CMSPortal/VisualizarArquivo?idArquivo=1", title="Apelação")

    blocks, _sources, notes = asyncio.run(external_research._read_official_pages([scon, tj]))

    assert requested == [tj.reference], "the SCON link must not take a reading slot"
    assert blocks
    assert any("O STJ bloqueia a leitura automatizada" in note for note in notes)


def test_only_scon_results_read_nothing_and_still_say_why(monkeypatch):
    async def fetch_pages(_urls):  # pragma: no cover
        raise AssertionError("nothing readable was left to fetch")

    monkeypatch.setattr(settings, "fetch_enabled", True)
    monkeypatch.setattr(external_research.web_fetch, "fetch_pages", fetch_pages)
    scon = SimpleNamespace(reference="https://scon.stj.jus.br/SCON/x", title="REsp")

    blocks, _sources, notes = asyncio.run(external_research._read_official_pages([scon]))

    assert blocks == []
    assert any("STJ" in note for note in notes)


def test_the_prompt_asks_for_the_fields_the_lawyers_asked_for():
    from core.services.legal_assistant import prompts

    for field in ("Tribunal:", "Processo:", "Relator(a):", "Data do julgamento:",
                  "Entendimento:", "Transcrição da ementa:", "Fonte:"):
        assert field in prompts.EXTERNAL_RESEARCH, field
    assert "não localizado na fonte consultada" in prompts.EXTERNAL_RESEARCH
    assert "sem alterar" in prompts.EXTERNAL_RESEARCH
