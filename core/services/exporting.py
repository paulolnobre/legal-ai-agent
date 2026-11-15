"""Turning an answer into a file the firm can send, sign or fill in.

Three formats, chosen by the lawyer and never guessed:

- `docx` — a minuta or a revision that will still be edited in Word;
- `pdf`  — the closed version, for the client or the case file;
- `xlsx` — comparisons, deadline tables and risk matrices, where the answer is
  really rows and columns rather than prose.

Heavy dependencies are imported inside each builder, so importing this module
(and running the test suite) costs nothing when no export is requested.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
import unicodedata
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

FORMATS = ("docx", "pdf", "xlsx")

_ALIASES = {
    "docx": "docx", "doc": "docx", "word": "docx", "documento": "docx",
    "pdf": "pdf",
    "xlsx": "xlsx", "xls": "xlsx", "excel": "xlsx", "planilha": "xlsx",
}

_DISCLAIMER = (
    "Documento gerado pelo Assistente Jurídico Demo & Associados Advocacia. "
    "Conteúdo auxiliar: a revisão e o parecer final são do advogado responsável."
)

# Public, so the editor can tell the closing line from the document's own text.
DISCLAIMER = _DISCLAIMER

# A table in a chat answer arrives as "coluna | coluna" lines. One stray pipe is
# punctuation; several lines split the same way are a table worth real columns.
_TABLE_ROW = re.compile(r"^[^|\n]+\|[^|\n]")


def normalize_format(raw) -> str | None:
    """Map "word", "excel", "PDF", "planilha" to one of FORMATS."""
    token = re.sub(r"[^a-z]", "", str(raw or "").casefold())
    return _ALIASES.get(token)


def _safe_stem(title: str) -> str:
    """An ASCII filename: accents and punctuation travel badly between systems."""
    decomposed = unicodedata.normalize("NFKD", (title or "resposta").strip())
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    stem = re.sub(r"[^A-Za-z0-9\-]+", "_", ascii_only)[:60].strip("_")
    return stem or "resposta"


def filename_for(title: str, fmt: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{_safe_stem(title)}_{stamp}.{fmt}"


# ── Markdown the model emits, turned into real formatting ────────────────────
# The prompts ask for plain text and the Telegram path strips what survives, but
# a model writing a long revision still reaches for "### Título" and "**Cláusula
# 2**". Those markers used to land in the exported file as literal characters.
# DOCX and PDF have headings and bold of their own, so the markup is translated
# rather than deleted; XLSX has neither, so there it is stripped.

_FENCE = re.compile(r"^\s*```.*$")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*(\d{1,3})[.)]\s+(.*)$")
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]+)`")
_MD_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_ESCAPED = re.compile(r"\\([\\`*_{}\[\]()<>#+\-.!])")


class Block:
    """One rendered line of the answer, with its role in the document."""

    __slots__ = ("kind", "level", "text")

    def __init__(self, kind: str, text: str, level: int = 0):
        self.kind = kind      # heading | paragraph | bullet | numbered
        self.text = text      # inline markers still present, for each renderer
        self.level = level    # heading depth, or the number of a numbered item

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Block({self.kind!r}, {self.text!r}, level={self.level})"


def _clean_inline(text: str) -> str:
    """Resolve what no output format can represent, keeping bold markers."""
    text = _ESCAPED.sub(r"\1", text)
    text = _MD_LINK.sub(r"\1: \2", text)
    text = _INLINE_CODE.sub(r"\1", text)
    return text.strip()


def parse_blocks(text: str) -> list[Block]:
    """Read the model's loose Markdown as a sequence of typed lines."""
    blocks: list[Block] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip() or _FENCE.match(line):
            continue

        heading = _HEADING.match(line)
        if heading:
            content = _clean_inline(heading.group(2))
            if content:
                blocks.append(Block("heading", content, level=len(heading.group(1))))
            continue

        bullet = _BULLET.match(line)
        if bullet:
            content = _clean_inline(bullet.group(1))
            if content:
                blocks.append(Block("bullet", content))
            continue

        numbered = _NUMBERED.match(line)
        if numbered:
            content = _clean_inline(numbered.group(2))
            if content:
                blocks.append(Block("numbered", content, level=int(numbered.group(1))))
            continue

        content = _clean_inline(line)
        # A line that is entirely bold is a heading the model wrote without "#".
        whole_bold = re.fullmatch(r"\*\*(.+?)\*\*:?|__(.+?)__:?", content)
        if whole_bold:
            blocks.append(Block("heading", (whole_bold.group(1) or whole_bold.group(2)).strip(), level=3))
            continue
        if content:
            blocks.append(Block("paragraph", content))
    return blocks


def _bold_runs(text: str) -> list[tuple[str, bool]]:
    """Split a line into (text, is_bold) pairs for a format with real runs."""
    runs: list[tuple[str, bool]] = []
    position = 0
    for match in _BOLD.finditer(text):
        if match.start() > position:
            runs.append((text[position:match.start()], False))
        runs.append(((match.group(1) or match.group(2)), True))
        position = match.end()
    if position < len(text):
        runs.append((text[position:], False))
    return [(part, bold) for part, bold in runs if part]


bold_runs = _bold_runs


def strip_markdown(text: str) -> str:
    """The line with every marker removed — for formats that cannot show them."""
    return "".join(part for part, _ in _bold_runs(_clean_inline(text))).strip()


def _paragraphs(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _table_rows(text: str) -> list[list[str]]:
    """Rows for a spreadsheet: a real table if the answer has one, else one
    column of paragraphs, which is still useful to filter and annotate."""
    lines = _paragraphs(text)
    table = [line for line in lines if _TABLE_ROW.match(line)]
    widths = {line.count("|") for line in table}
    # Same number of separators on every line: a real table, not prose that
    # happens to contain a pipe.
    if len(table) >= 2 and len(widths) == 1:
        rows = [[cell.strip() for cell in line.strip("|").split("|")] for line in table]
        # Markdown separator rows ("---|---") carry no data.
        return [row for row in rows if not all(set(cell) <= set("-: ") for cell in row)]
    return [[line] for line in lines]


def _docx_blocks(document, text: str) -> None:
    """Write the answer into a Word document with real headings and bold."""
    for block in parse_blocks(text):
        if block.kind == "heading":
            # Level 1 is the document title, so the model's headings start below it.
            document.add_heading(strip_markdown(block.text), level=min(block.level + 1, 4))
            continue
        style = {"bullet": "List Bullet", "numbered": "List Number"}.get(block.kind)
        paragraph = document.add_paragraph(style=style) if style else document.add_paragraph()
        for part, bold in _bold_runs(block.text):
            paragraph.add_run(part).bold = bold


def build_docx(title: str, text: str, sources: str = "") -> bytes:
    import docx

    document = docx.Document()
    document.add_heading(title or "Resposta", level=1)
    _docx_blocks(document, text)
    if sources:
        document.add_heading("Fontes", level=2)
        for paragraph in _paragraphs(sources):
            document.add_paragraph(strip_markdown(paragraph))
    document.add_paragraph("")
    document.add_paragraph(_DISCLAIMER)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def build_pdf(title: str, text: str, sources: str = "") -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=2.5 * cm, rightMargin=2.5 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
        title=title or "Resposta",
    )
    styles = getSampleStyleSheet()
    flow = [Paragraph(_escape(title or "Resposta"), styles["Title"]), Spacer(1, 12)]
    for block in parse_blocks(text):
        if block.kind == "heading":
            flow.append(Spacer(1, 6))
            flow.append(Paragraph(
                _rich(block.text), styles[f"Heading{min(block.level + 1, 4)}"],
            ))
            continue
        if block.kind in ("bullet", "numbered"):
            marker = "•" if block.kind == "bullet" else f"{block.level}."
            # bulletText keeps the marker in the margin, so wrapped lines align.
            flow.append(Paragraph(
                _rich(block.text), styles["BodyText"], bulletText=marker,
            ))
            continue
        flow.append(Paragraph(_rich(block.text), styles["BodyText"]))
        flow.append(Spacer(1, 6))
    if sources:
        flow.append(Spacer(1, 12))
        flow.append(Paragraph("Fontes", styles["Heading2"]))
        for paragraph in _paragraphs(sources):
            flow.append(Paragraph(_escape(strip_markdown(paragraph)), styles["BodyText"]))
    flow.append(Spacer(1, 18))
    flow.append(Paragraph(_escape(_DISCLAIMER), styles["Italic"]))
    document.build(flow)
    return buffer.getvalue()


def _escape(text: str) -> str:
    """reportlab reads its paragraphs as mini-HTML, so markup must be escaped."""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _rich(text: str) -> str:
    """A line as reportlab's mini-HTML: escaped first, then bold applied.

    Escaping before converting is what keeps a contract that literally contains
    "<" from opening a tag, while the model's own ** markers still become bold.
    """
    return "".join(
        f"<b>{_escape(part)}</b>" if bold else _escape(part)
        for part, bold in _bold_runs(text)
    )


def build_xlsx(title: str, text: str, sources: str = "") -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    workbook = Workbook()
    # `workbook.active` is typed as optional because an empty workbook has no
    # active sheet; a new one always has exactly this sheet.
    sheet = workbook.worksheets[0]
    sheet.title = "Resposta"

    sheet.append([title or "Resposta"])
    sheet["A1"].font = Font(bold=True, size=14)
    sheet.append([])

    table = _table_rows(text)
    if len(table) > 1 and any(len(row) > 1 for row in table):
        # A real table: the cells are data, so only the markers come off.
        for row in table:
            sheet.append([strip_markdown(cell) for cell in row])
    else:
        # Prose: a spreadsheet has no headings, so a heading becomes a bold row
        # and a list item keeps its marker, which is what survives a cell.
        for block in parse_blocks(text):
            prefix = {"bullet": "• ", "numbered": f"{block.level}. "}.get(block.kind, "")
            sheet.append([prefix + strip_markdown(block.text)])
            if block.kind == "heading":
                sheet.cell(row=sheet.max_row, column=1).font = Font(bold=True)

    if sources:
        sheet.append([])
        sheet.append(["Fontes"])
        sheet.cell(row=sheet.max_row, column=1).font = Font(bold=True)
        for paragraph in _paragraphs(sources):
            sheet.append([strip_markdown(paragraph)])
    sheet.append([])
    sheet.append([_DISCLAIMER])

    return _finish_sheet(workbook, sheet)


def _finish_sheet(workbook, sheet) -> bytes:
    """Widths, wrapping and the bytes, shared by both spreadsheet builders."""
    from openpyxl.styles import Alignment
    from openpyxl.utils import get_column_letter

    # Column A holds the prose and is sized to it; the rest are table columns.
    # Widths come from the column index rather than from a cell, because a
    # merged cell carries no column letter of its own.
    widest = max((len(str(cell.value or "")) for cell in sheet["A"]), default=20)
    sheet.column_dimensions["A"].width = min(max(widest, 20), 90)
    for index in range(2, sheet.max_column + 1):
        sheet.column_dimensions[get_column_letter(index)].width = 40
    for row in sheet.iter_rows():
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def build_xlsx_rows(title: str, rows: list[list[str]]) -> bytes:
    """A spreadsheet from a grid as it is, for the editor. `build_xlsx` reads
    its text for a table and strips markers; this takes the cells as typed
    and writes every one as a string, so a cell that starts with "=" is text
    in the file and never a formula."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    workbook = Workbook()
    sheet = workbook.worksheets[0]
    sheet.title = "Resposta"
    # The title is typed too: a title that starts with "=" is text as well.
    sheet.cell(row=1, column=1, value=title or "Resposta").data_type = "s"
    sheet["A1"].font = Font(bold=True, size=14)
    # Rows are placed by number: an empty `append` leaves no cell behind, so
    # `max_row` would not move.
    first = 3
    for offset, row in enumerate(rows):
        for index, value in enumerate(row, start=1):
            sheet.cell(row=first + offset, column=index, value=value).data_type = "s"
    sheet.cell(row=first + len(rows) + 1, column=1, value=_DISCLAIMER)
    return _finish_sheet(workbook, sheet)


_BUILDERS = {"docx": build_docx, "pdf": build_pdf, "xlsx": build_xlsx}


def build(fmt: str, title: str, text: str, sources: str = "") -> tuple[bytes, str]:
    """Render an answer in `fmt`. Returns the bytes and the filename to send."""
    if fmt not in _BUILDERS:
        raise ValueError(f"unsupported export format: {fmt}")
    return _BUILDERS[fmt](title, text, sources), filename_for(title, fmt)


async def build_async(fmt: str, title: str, text: str, sources: str = "") -> tuple[bytes, str]:
    """`build` off the event loop — rendering a long PDF takes real CPU time."""
    return await asyncio.to_thread(build, fmt, title, text, sources)
