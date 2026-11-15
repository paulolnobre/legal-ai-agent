"""The em-dash does not belong in anything this firm reads.

It is not a matter of taste that can be settled once and then drift. It has
been asked for three times, most recently on 2026-09-16 after a lawyer's own
Telegram answer came back full of them, and every time the offenders had to be
found by hand. A rule nobody can enforce mechanically is a rule that comes
back.

So this test reads the string literals of the project, not its comments, and
fails when one carries an em-dash or an en dash. Comments and docstrings are
left out deliberately: they are worth cleaning too, but they never reach the
firm, and a test that fails on an internal note trains people to silence the
test rather than fix the copy.
"""
import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SEARCHED = ("core", "clients")
_DASHES = "—–"  # em dash, en dash

# A few strings are about the dashes themselves rather than written with them:
# they match or strip punctuation in text somebody else wrote, or they name the
# character in order to forbid it. Taking the dash out of one of these breaks
# the very thing it is for. Every entry is listed whole, never as a loose
# pattern, so an exception is always a decision somebody made on purpose.
#
# Anything added here needs a reason of that kind, never a deadline.
_ALLOWED = frozenset({
    # Strips trailing punctuation from a search query.
    " .,:;-–—",
    # Reads an "EMENTA:" or "EMENTA –" heading out of a court document, and
    # strips the punctuation around it. The dash is in what the courts publish,
    # never in what this application writes.
    "(?m)^[ \\t]*EMENTA\\b[ \\t]*[:.\\-–]?[ \\t]*",
    " \t:-–.,;",
})

# The one exception that cannot be listed whole. The instruction telling the
# model not to write dashes lives inside the voice guide, and Python folds the
# adjacent literals of that guide into a single string, so the only way to name
# it is by the line itself.
_ALLOWED_LINE = "Nunca use travessão"


def _offenders(path: Path):
    """Every string literal in one file that carries a dash, minus docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        )
    }
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if node.value in docstrings:
            continue
        if not any(dash in node.value for dash in _DASHES):
            continue
        if node.value in _ALLOWED or _ALLOWED_LINE in node.value:
            continue
        yield node.lineno, node.value


def _source_files():
    return sorted(
        path for directory in _SEARCHED for path in (_ROOT / directory).rglob("*.py")
    )


@pytest.mark.parametrize(
    "path", _source_files(), ids=lambda p: str(p.relative_to(_ROOT))
)
def test_no_dashes_in_what_the_firm_reads(path: Path):
    relative = str(path.relative_to(_ROOT))
    found = list(_offenders(path))
    assert not found, "\n".join(
        f"{relative}:{lineno} has a dash where a colon, a comma, parentheses "
        f"or two sentences belong: {value.strip()[:90]!r}"
        for lineno, value in found
    )


def test_the_assistant_is_told_not_to_write_them():
    """The fixed strings are only half of it: the model writes prose too.

    Cleaning the templates while leaving the system prompt silent would fix the
    frame around an answer and not the answer, which is the half the lawyer
    actually reads. Every prompt that produces prose carries the instruction;
    ORCHESTRATOR does not, and should not, because it routes and writes to
    nobody.
    """
    from core.services.legal_assistant import prompts

    for name in ("INTERNAL_RESEARCH", "EXTERNAL_RESEARCH", "REVIEW", "DRAFTING", "CHAT"):
        assert "Nunca use travessão" in getattr(prompts, name), name
