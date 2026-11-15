"""An attachment's text in front of the answering model: bounded, as data.

The reading of a photo or the transcript of an audio file reaches the answer
as a document block of its own, never inside the instructions. It is cut at
60,000 characters, and whatever other evidence the node gathered is cut so
that the attachment and that evidence together stay under 90,000.

Its size is also added to the call's input reservation, in UTF-8 bytes: the
providers' tokenizers are byte-level, so a text never has more tokens than
bytes, whatever its script, and `budget.settle` never meets usage above what
was reserved.
"""
from copy import deepcopy

from .rendering import document_block
from .state import LegalState

MAX_ATTACHMENT_CHARS = 60_000
MAX_CONTEXT_CHARS = 90_000
TITLE = "Anexo desta conversa"


def bounded(text: str | None) -> str | None:
    """The attachment as the answer may take it, or None for none."""
    return (text or "")[:MAX_ATTACHMENT_CHARS] or None


def content_for(state: LegalState, content: list[dict]) -> list[dict]:
    """`content` with the attachment first and the other document blocks cut
    to fit; unchanged without an attachment. The caller's blocks are not
    modified."""
    attachment = bounded(state.attachment_text)
    if attachment is None:
        return content
    text_size = sum(len(str(b.get("text", ""))) for b in content if b.get("type") == "text")
    remaining = max(0, MAX_CONTEXT_CHARS - len(attachment) - text_size)
    kept = []
    for block in content:
        if block.get("type") != "document":
            kept.append(block)
            continue
        candidate = deepcopy(block)
        parts = candidate.get("source", {}).get("content", [])
        for part in parts:
            part["text"] = str(part.get("text", ""))[:remaining]
            remaining -= len(part["text"])
        if any(part.get("text") for part in parts):
            kept.append(candidate)
    return [document_block(TITLE, attachment), *kept]


def reservation_for(state: LegalState) -> dict[str, int]:
    """The keyword `complete` takes to reserve the attachment too."""
    attachment = bounded(state.attachment_text)
    if attachment is None:
        return {}
    return {"extra_input_tokens": len(attachment.encode("utf-8"))}
