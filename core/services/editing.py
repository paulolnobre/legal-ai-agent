"""Reading a generated file back into something a person can edit.

The editor opens what `exporting` built, so the structure is known: a level-1
heading is the title, the other headings, lists and bold runs are the light
Markdown `exporting.parse_blocks` reads, and the closing disclaimer is
written again by the builder. Nothing here stores anything.

Limits are the editor's, checked on what is opened and on what is posted.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass

from core.services import exporting

MAX_TEXT = 200_000
MAX_ROWS = 200
MAX_COLS = 20
MAX_CELL = 2_000
MAX_TITLE = 200

_DEFAULT_TITLE = "Resposta"


class NotEditable(Exception):
    """The file is not one of ours, does not read back, or is over a limit."""


@dataclass(frozen=True)
class EditableText:
    title: str
    text: str


# What `exporting.parse_blocks` would take as syntax at the start of a line.
_LINE_SYNTAX = re.compile(r"^(#{1,6}\s|[-+]\s|(\d{1,3})([.)])\s)")
_INLINE_SYNTAX = re.compile(r"([\\*_`\[\]])")


def _escape_inline(text: str) -> str:
    return _INLINE_SYNTAX.sub(r"\\\1", text)


def _escape_line_start(line: str) -> str:
    """A line that would start a heading or a list is written escaped."""
    match = _LINE_SYNTAX.match(line)
    if match is None:
        return line
    if match.group(2):
        return line.replace(match.group(3), "\\" + match.group(3), 1)
    return "\\" + line


def _line(paragraph) -> str:
    parts = []
    for run in paragraph.runs:
        if not run.text:
            continue
        text = _escape_inline(run.text)
        parts.append(f"**{text}**" if run.bold else text)
    return _escape_line_start("".join(parts))


def read_docx(data: bytes) -> EditableText:
    import docx

    try:
        document = docx.Document(io.BytesIO(data))
        paragraphs = list(document.paragraphs)
        names = [(p.style.name if p.style is not None else "") or "" for p in paragraphs]
    except Exception as exc:
        raise NotEditable("not a readable .docx") from exc

    # The closing blank line and disclaimer are the builder's, not the text's.
    if paragraphs and paragraphs[-1].text == exporting.DISCLAIMER:
        paragraphs.pop()
        names.pop()
        if paragraphs and not paragraphs[-1].text:
            paragraphs.pop()
            names.pop()

    title = ""
    lines: list[str] = []
    counter = 0
    for paragraph, style in zip(paragraphs, names):
        if style == "Heading 1" and not title:
            title = paragraph.text.strip()[:MAX_TITLE]
            continue
        if not paragraph.text.strip():
            continue
        heading = re.fullmatch(r"Heading (\d)", style)
        if heading:
            counter = 0
            marks = "#" * max(int(heading.group(1)) - 1, 1)
            body = _escape_inline(paragraph.text.strip())
            lines.append(f"{marks} {body}")
        elif style == "List Bullet":
            counter = 0
            lines.append(f"- {_line(paragraph)}")
        elif style == "List Number":
            counter += 1
            lines.append(f"{counter}. {_line(paragraph)}")
        else:
            counter = 0
            lines.append(_line(paragraph))

    text = "\n".join(lines)
    if len(text) > MAX_TEXT:
        raise NotEditable("text over the editor's limit")
    return EditableText(title=title or _DEFAULT_TITLE, text=text)


@dataclass(frozen=True)
class EditableSheet:
    title: str
    rows: list[list[str]]


def validate_text(title: str, text: str) -> bool:
    return len(title) <= MAX_TITLE and len(text) <= MAX_TEXT


def validate_grid(title: str, rows: list[list[str]]) -> bool:
    return (len(title) <= MAX_TITLE and len(rows) <= MAX_ROWS
            and all(len(row) <= MAX_COLS and all(len(cell) <= MAX_CELL for cell in row)
                    for row in rows))


def read_xlsx(data: bytes) -> EditableSheet:
    from openpyxl import load_workbook

    try:
        sheet = load_workbook(io.BytesIO(data)).worksheets[0]
        rows = [["" if value is None else str(value) for value in row]
                for row in sheet.iter_rows(values_only=True)]
    except Exception as exc:
        raise NotEditable("not a readable .xlsx") from exc
    if not rows:
        raise NotEditable("an empty workbook")

    title = (rows[0][0] if rows[0] else "").strip()[:MAX_TITLE] or _DEFAULT_TITLE
    grid = rows[2:]
    # The closing blank row and disclaimer are the builder's, not the grid's.
    if grid and grid[-1][:1] == [exporting.DISCLAIMER]:
        grid.pop()
        if grid and not any(grid[-1]):
            grid.pop()
    width = max((index + 1 for row in grid for index, cell in enumerate(row) if cell),
                default=0)
    grid = [row[:width] + [""] * (width - len(row)) for row in grid]
    if not validate_grid(title, grid):
        raise NotEditable("grid over the editor's limits")
    return EditableSheet(title=title, rows=grid)
