"""Every answer carries its sources, and can be delivered as a file.

The rule the firm asked for: never answer without knowing what the answer rests
on, offer the list instead of dumping it, explain what is in each source, and
ask which format before producing a file.
"""
import asyncio
from types import SimpleNamespace

import pytest

from core.handlers import legal_message
from core.services import channels
from core.services import exporting
from core.services.legal_assistant import sources
from core.services.legal_assistant.reply import AgentReply

pytestmark = pytest.mark.usefixtures("isolate_message_rate_limits")


# ── The sources themselves ───────────────────────────────────────────────────

def test_corpus_and_web_sources_explain_what_is_in_them():
    hit = SimpleNamespace(
        document_id=2,
        title="Contrato de prestação de serviços",
        doc_type="contrato",
        matter="Cliente X",
        content="A rescisão exige aviso prévio de 30 dias.",
    )
    result = SimpleNamespace(
        title="STJ — Tema 1.234",
        url="https://www.stj.jus.br/tema-1234",
        snippet="O STJ fixou que o prazo corre da intimação pessoal.",
        date="12/03/2025",
    )
    listing = sources.listing([sources.from_corpus_hit(hit), sources.from_web_result(result)])

    assert "documento [2] do acervo" in listing
    assert "caso Cliente X" in listing
    assert "aviso prévio de 30 dias" in listing, "the excerpt used must be shown"
    assert "https://www.stj.jus.br/tema-1234" in listing
    assert "prazo corre da intimação pessoal" in listing, "explain what is at the link"
    assert "12/03/2025" in listing


def test_the_offer_says_what_the_answer_rests_on():
    hit = SimpleNamespace(
        document_id=2, title="Contrato", doc_type=None, matter=None, content="texto",
    )
    web = SimpleNamespace(
        title="Planalto", url="https://www.planalto.gov.br/lei", snippet="Art. 1º", date=None,
    )
    offer = sources.offer([sources.from_corpus_hit(hit), sources.from_web_result(web)])

    assert "1 documento do acervo e 1 fonte pública oficial" in offer
    assert "Quer que eu liste as fontes" in offer


def test_repeated_sources_are_listed_once():
    web = SimpleNamespace(
        title="Planalto", url="https://www.planalto.gov.br/lei", snippet="Art. 1º", date=None,
    )
    listing = sources.listing([sources.from_web_result(web), sources.from_web_result(web)])
    assert listing.count("https://www.planalto.gov.br/lei") == 1


def test_an_answer_without_sources_offers_nothing():
    assert sources.offer([]) == ""
    assert "não teve fontes" in sources.listing([])


# ── The exported files ───────────────────────────────────────────────────────

def test_format_names_the_firm_actually_types_are_understood():
    assert exporting.normalize_format("PDF") == "pdf"
    assert exporting.normalize_format("word") == "docx"
    assert exporting.normalize_format("planilha") == "xlsx"
    assert exporting.normalize_format("excel") == "xlsx"
    assert exporting.normalize_format("mp3") is None


def test_each_format_produces_a_readable_file():
    text = "Cláusula 1 — do objeto.\nCláusula 2 — do prazo."
    listing = "[1] Contrato [2] do acervo"

    docx_bytes, docx_name = exporting.build("docx", "Revisão do contrato", text, listing)
    pdf_bytes, pdf_name = exporting.build("pdf", "Revisão do contrato", text, listing)
    xlsx_bytes, xlsx_name = exporting.build("xlsx", "Revisão do contrato", text, listing)

    assert docx_bytes[:2] == b"PK" and docx_name.endswith(".docx")
    assert pdf_bytes[:5] == b"%PDF-" and pdf_name.endswith(".pdf")
    assert xlsx_bytes[:2] == b"PK" and xlsx_name.endswith(".xlsx")
    assert "Revisao_do_contrato" in docx_name, "the filename must survive accents"


def test_docx_keeps_the_text_and_the_sources():
    import io
    import zipfile

    data, _ = exporting.build("docx", "Parecer", "Primeiro parágrafo.", "[1] Fonte oficial")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        document = archive.read("word/document.xml").decode()
    assert "Primeiro parágrafo." in document
    assert "Fonte oficial" in document
    assert "advogado responsável" in document, "the disclaimer travels with the file"


def test_a_table_answer_becomes_columns_in_the_spreadsheet():
    import io

    from openpyxl import load_workbook

    text = "Prazo | Base legal\nAgravo: 15 dias | Art. 1.003 CPC\nEmbargos: 5 dias | Art. 1.023 CPC"
    data, _ = exporting.build("xlsx", "Prazos", text)
    # By name rather than `.active`, which is typed as optional: this also
    # asserts the sheet is the one the exporter meant to create.
    sheet = load_workbook(io.BytesIO(data))["Resposta"]
    rows = [[cell.value for cell in row] for row in sheet.iter_rows()]

    assert ["Prazo", "Base legal"] in rows
    assert ["Agravo: 15 dias", "Art. 1.003 CPC"] in rows


def test_pdf_survives_markup_in_the_answer():
    data, _ = exporting.build("pdf", "Parecer", "Cláusula <b> e M&A não podem quebrar o PDF")
    assert data[:5] == b"%PDF-"


# ── The Markdown the model still emits ───────────────────────────────────────
# Reported in production: a downloaded .docx carried literal "###" and "**".

_MARKDOWN_ANSWER = """### LISTA DE ALTERAÇÕES

1. **Cláusula 2 - Das Obrigações da Contratada**
- **Original:** "Fornecer suporte técnico durante 30 (trinta) dias."
- **Motivo:** A falta de clareza pode gerar litígios.

**VERSÃO REVISADA**
Texto corrido com `código`, um [link](https://www.planalto.gov.br/lei) e a < b.
"""


def test_the_model_markdown_is_read_as_structure():
    kinds = [(block.kind, block.level) for block in exporting.parse_blocks(_MARKDOWN_ANSWER)]

    assert kinds[0] == ("heading", 3)
    assert ("numbered", 1) in kinds
    assert ("bullet", 0) in kinds
    # A line that is entirely bold is the heading the model wrote without "#".
    assert kinds[-2] == ("heading", 3)
    assert kinds[-1] == ("paragraph", 0)


def test_inline_markers_are_resolved_not_carried():
    blocks = exporting.parse_blocks(_MARKDOWN_ANSWER)
    prose = blocks[-1].text

    assert "`" not in prose, "inline code markers do not belong in a document"
    assert "https://www.planalto.gov.br/lei" in prose, "the link target must survive"
    assert "[link]" not in prose
    assert exporting.strip_markdown("**Original:** texto") == "Original: texto"


def test_docx_gets_real_headings_and_bold_instead_of_markers():
    import io
    import zipfile

    data, _ = exporting.build("docx", "Revisão", _MARKDOWN_ANSWER, "[1] Fonte")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        document = archive.read("word/document.xml").decode()

    assert "##" not in document, "a heading marker must never reach the file"
    assert "**" not in document, "a bold marker must never reach the file"
    assert "<w:b/>" in document, "bold must be real formatting"
    assert "LISTA DE ALTERAÇÕES" in document
    assert "ListBullet" in document or "ListParagraph" in document


def test_pdf_converts_bold_after_escaping_hostile_characters():
    # The contract's own "<" must be escaped, while the model's ** becomes a tag.
    assert exporting._rich('a < b com **negrito** e M&A') == (
        "a &lt; b com <b>negrito</b> e M&amp;A"
    )
    data, _ = exporting.build("pdf", "Revisão", _MARKDOWN_ANSWER)
    assert data[:5] == b"%PDF-"


def test_spreadsheet_cells_carry_no_markers():
    import io

    from openpyxl import load_workbook

    data, _ = exporting.build("xlsx", "Revisão", _MARKDOWN_ANSWER, "[1] Fonte")
    sheet = load_workbook(io.BytesIO(data))["Resposta"]
    cells = [(cell.value, cell.font.bold) for cell in sheet["A"] if cell.value]
    values = [value for value, _ in cells]

    assert not any("##" in value or "**" in value for value in values)
    # A spreadsheet has no headings, so the heading becomes a bold row.
    assert ("LISTA DE ALTERAÇÕES", True) in cells
    assert any(value.startswith("• Original: ") for value in values)
    assert any(value.startswith("1. Cláusula 2") for value in values)


# ── The conversation around them ─────────────────────────────────────────────

def _conversation(monkeypatch: pytest.MonkeyPatch, reply, messages):
    """Send messages to a staff chat, returning what the bot sent back."""
    sent: list[str] = []
    documents: list[tuple] = []

    async def get_staff(*args, **kwargs):
        return SimpleNamespace(id=7, name="Paulo Nobre", role="owner")

    async def send(chat_id, text, reply_markup=None):
        sent.append(text)

    async def typing(*args, **kwargs):
        pass

    async def answer(*args, **kwargs):
        return reply

    async def send_document(chat_id, data, filename, caption=None, base_url=None):
        documents.append((filename, len(data), caption))
        return True

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(channels, "send_typing", typing)
    monkeypatch.setattr(legal_message, "answer_question", answer)
    monkeypatch.setattr(channels, "send_document", send_document)
    monkeypatch.setattr(legal_message, "_limiter", SimpleNamespace(check=lambda _c: (True, False)))
    legal_message._last_deliveries.clear()

    for message in messages:
        asyncio.run(
            legal_message.process_legal_message(
                "connection",
                {"update_id": 1, "message": {"chat": {"id": 93001}, "text": message}},
            )
        )
    return sent, documents


_REPLY = AgentReply(
    text="O prazo do agravo de instrumento é de 15 dias úteis.",
    sources=[sources.Source(
        kind="web",
        title="CPC — Art. 1.003",
        reference="https://www.planalto.gov.br/cpc",
        detail="Texto do artigo que fixa a contagem do prazo recursal.",
    )],
)


def test_answer_offers_its_sources_and_lists_them_on_a_bare_yes(monkeypatch):
    sent, _ = _conversation(monkeypatch, _REPLY, ["qual o prazo do agravo?", "sim"])

    assert sent[0].startswith("O prazo do agravo")
    assert "Quer que eu liste as fontes" in sent[0]
    assert "https://www.planalto.gov.br/cpc" not in sent[0], "the list is offered, not dumped"
    assert "Fontes desta resposta:" in sent[1]
    assert "https://www.planalto.gov.br/cpc" in sent[1]
    assert "contagem do prazo recursal" in sent[1], "the link must be explained"


def test_export_asks_which_format_before_producing_the_file(monkeypatch):
    sent, documents = _conversation(
        monkeypatch, _REPLY, ["qual o prazo do agravo?", "quero isso em arquivo", "pdf"],
    )

    assert "Em qual formato" in sent[1]
    assert not documents or len(documents) == 1
    assert documents[0][0].endswith(".pdf")
    assert documents[0][1] > 0


def test_a_named_format_exports_straight_away(monkeypatch):
    sent, documents = _conversation(
        monkeypatch, _REPLY, ["qual o prazo do agravo?", "me manda em word"],
    )

    assert [name for name, _, _ in documents] and documents[0][0].endswith(".docx")
    assert "Em qual formato" not in "".join(sent[1:])


def test_export_command_without_a_recent_answer_says_so(monkeypatch):
    sent, documents = _conversation(monkeypatch, _REPLY, ["/exportar pdf"])

    assert not documents
    assert "não tenho uma resposta recente" in sent[0].casefold()


def test_sources_command_lists_the_last_answer(monkeypatch):
    sent, _ = _conversation(monkeypatch, _REPLY, ["qual o prazo do agravo?", "/fontes"])
    assert "Fontes desta resposta:" in sent[1]
