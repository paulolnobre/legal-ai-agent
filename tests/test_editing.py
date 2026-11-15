"""Reading our own generated files back into something a person can edit.

Pure logic: a document is built by `exporting`, read back by `editing`, built
again, and the two must be the same document. The editor never stores
anything, so this round trip is the whole promise that "Baixar" gives back
what was opened.
"""
import io
import zipfile

import docx
import pytest

from core.services import editing, exporting

ANSWER = (
    "## Tese\n"
    "Texto com **negrito** no meio.\n"
    "- primeiro item\n"
    "- segundo item\n"
    "1. um\n"
    "2. dois\n"
    "\\# nao e titulo\n"
    "\\- nao e item\n"
    "1\\. nao e lista\n"
)


def _shape(data: bytes) -> list[tuple[str, str, list[bool]]]:
    document = docx.Document(io.BytesIO(data))
    return [((p.style.name if p.style else "") or "", p.text, [bool(r.bold) for r in p.runs])
            for p in document.paragraphs]


def test_docx_round_trips():
    first = exporting.build_docx("Parecer", ANSWER, "[1] Lei 8.078/90")

    read = editing.read_docx(first)
    second = exporting.build_docx(read.title, read.text)

    assert read.title == "Parecer"
    assert _shape(first) == _shape(second)
    shape = _shape(second)
    assert [p for p in shape if p[1] == exporting.DISCLAIMER] == [
        ("Normal", exporting.DISCLAIMER, [False])]
    assert ("Heading 2", "Fontes", [False]) in shape
    assert ("Normal", "# nao e titulo", [False]) in shape
    assert ("Normal", "- nao e item", [False]) in shape
    assert ("Normal", "1. nao e lista", [False]) in shape


def test_docx_numbered_items_count_in_a_run():
    data = exporting.build_docx("T", "1. a\n2. b\n3. c\nmeio\n1. d")

    text = editing.read_docx(data).text.splitlines()

    assert text == ["1. a", "2. b", "3. c", "meio", "1. d"]


def test_docx_empty_body_and_empty_title():
    read = editing.read_docx(exporting.build_docx("", ""))

    assert read.title == "Resposta" and read.text == ""
    assert exporting.build_docx(read.title, read.text)


def test_docx_literal_syntax_is_escaped_on_read():
    document = docx.Document()
    document.add_heading("T", level=1)
    document.add_paragraph("# not a heading")
    buffer = io.BytesIO()
    document.save(buffer)

    read = editing.read_docx(buffer.getvalue())

    blocks = exporting.parse_blocks(read.text)
    assert [(b.kind, b.text) for b in blocks] == [("paragraph", "# not a heading")]


def test_not_a_docx_is_refused():
    with pytest.raises(editing.NotEditable):
        editing.read_docx(b"%PDF-1.7")
    big = exporting.build_docx("T", "x" * (editing.MAX_TEXT + 1))
    with pytest.raises(editing.NotEditable):
        editing.read_docx(big)


# ── Spreadsheets ────────────────────────────────────────────────────────────


def _grid(data: bytes):
    from openpyxl import load_workbook

    return load_workbook(io.BytesIO(data)).worksheets[0]


def test_xlsx_rows_are_strings():
    rows = [["=1+1", "+1", "-1", "@x"], ["a|b", "**x**", "", "z"]]

    data = exporting.build_xlsx_rows("T", rows)
    sheet = _grid(data)

    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            cell = sheet.cell(row=r + 3, column=c + 1)
            assert cell.data_type != "f" and (cell.value or "") == value
    # The file itself holds no formula, whatever a reader makes of the cell.
    xml = zipfile.ZipFile(io.BytesIO(data)).read("xl/worksheets/sheet1.xml").decode()
    assert "<f>" not in xml and "t=\"inlineStr\"" in xml


def test_xlsx_round_trips():
    rows = [["a", "b", "c"], ["=1+1", "**x**"], ["a|b"]]

    read = editing.read_xlsx(exporting.build_xlsx_rows("Prazos", rows))

    assert read.title == "Prazos"
    assert read.rows == [["a", "b", "c"], ["=1+1", "**x**", ""], ["a|b", "", ""]]
    again = _grid(exporting.build_xlsx_rows(read.title, read.rows))
    assert [[c.value or "" for c in row] for row in again.iter_rows(min_row=3, max_row=5)] \
        == read.rows
    assert again.max_row == 7  # title, blank, three rows, blank, disclaimer


def test_xlsx_from_the_answer_reads_back():
    read = editing.read_xlsx(exporting.build_xlsx("Prazos", "a | b\nc | d"))

    assert read.rows == [["a", "b"], ["c", "d"]]
    empty = editing.read_xlsx(exporting.build_xlsx_rows("", []))
    assert empty.title == "Resposta" and empty.rows == []


def test_limits():
    cell = "x" * editing.MAX_CELL
    assert editing.validate_grid("t" * editing.MAX_TITLE, [[cell] * editing.MAX_COLS] * editing.MAX_ROWS)
    assert not editing.validate_grid("t", [["a"]] * (editing.MAX_ROWS + 1))
    assert not editing.validate_grid("t", [["a"] * (editing.MAX_COLS + 1)])
    assert not editing.validate_grid("t", [[cell + "x"]])
    assert not editing.validate_grid("t" * (editing.MAX_TITLE + 1), [["a"]])
    assert editing.validate_text("t", "x" * editing.MAX_TEXT)
    assert not editing.validate_text("t", "x" * (editing.MAX_TEXT + 1))
    assert not editing.validate_text("t" * (editing.MAX_TITLE + 1), "x")
    too_wide = exporting.build_xlsx_rows("T", [["a"] * (editing.MAX_COLS + 1)])
    with pytest.raises(editing.NotEditable):
        editing.read_xlsx(too_wide)
    with pytest.raises(editing.NotEditable):
        editing.read_xlsx(b"junk")
