"""The archive is a source, and nothing reads one of its documents whole.

The rule is not a preference, it is a bill. Nothing bounded what the removed
operations sent: the largest document in the firm's archive is 579,000
characters, roughly 145,000 tokens, and a legal code would be several times
that. `/resumo 42` was one uncapped request, repeatable by anyone with a rank,
by typing three characters and a number.

Retrieval is bounded by construction: a question reads the passages that
matched it, capped in number, never the file behind them. So the property worth
guarding is narrow and checkable: `full_text` leaves the database only to be
written, never to be sent to a model.
"""
import ast
from pathlib import Path

import pytest

from core.handlers import legal_message
from core.services.legal_assistant import intents

ROOT = Path(__file__).resolve().parents[1]

# Where a document's whole text legitimately appears: it is written on
# ingestion, selected in the repository, and modelled as a field.
_MAY_TOUCH_FULL_TEXT = {
    "core/repositories/documents.py",
    "core/services/ingestion.py",
    "core/models.py",
}


def _python_sources() -> list[Path]:
    return [
        path for path in (ROOT / "core").rglob("*.py")
        if "__pycache__" not in path.parts
    ]


def test_no_service_reads_a_stored_document_whole():
    """The one place this could regress is a new caller of get_document()."""
    offenders = []
    for path in _python_sources():
        relative = path.relative_to(ROOT).as_posix()
        if relative in _MAY_TOUCH_FULL_TEXT:
            continue
        if "full_text" in path.read_text(encoding="utf-8"):
            offenders.append(relative)

    assert not offenders, (
        "these read a document's whole text outside the repository, which is how "
        "an unbounded request reaches a model: " + ", ".join(offenders)
    )


def test_the_review_agent_only_reads_what_was_put_in_front_of_it():
    """Its remaining input is the message, capped by max_question_chars."""
    source = (ROOT / "core/services/legal_assistant/review.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    defined = {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert "review" in defined, "reviewing pasted text is still supported"
    assert not {"summarize_document", "correct_document"} & defined
    assert "get_document" not in source, "the corpus is not read from here"


def test_no_command_offers_a_whole_document_operation():
    """It used to be checked against the button labels. The buttons are gone,
    and the command list Telegram offers is where the offering lives now."""
    offered = " ".join(
        f"{entry['command']} {entry['description']}"
        for entry in legal_message.command_menu()
    ).lower()
    assert "revis" not in offered
    assert "resum" not in offered

    steps = set(legal_message._PENDING_STEPS)
    assert not {"summary", "revision"} & steps, (
        "a flow that asks which document to summarise is a flow that summarises"
    )


@pytest.mark.parametrize("text", [
    "faça um resumo do doc [2]",
    "revisar o documento 12, por favor",
    "revise o modelo_contrato_agente_ia.pdf",
])
def test_the_ask_is_still_recognised_so_it_can_be_answered(text):
    """Detection is deterministic and free, and it is what lets the reply
    explain the rule rather than guess at a narrower question."""
    request = intents.detect_document_request(text)

    assert request is not None and request.action == "analysis"


def test_the_refusal_says_where_the_capability_went():
    """A refusal that names no alternative reads as a broken feature."""
    message = legal_message._CORPUS_IS_A_SOURCE

    assert "/arquivo" in message, "the original file is still available"
    assert "pergunta" in message, "asking a question still works"
    assert "chat" in message, "sending the text is the other way"
