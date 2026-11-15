"""Agent 4 — review of text a lawyer put in front of it.

One way in: the orchestrator routes here when somebody pastes text into the
chat. It used to have two more, `/resumo <id>` and `/revisar <id>`, which read a
stored document whole and sent every character of it to the model.

They are gone, and the reason is arithmetic rather than taste. Nothing bounded
the size of what they sent. The largest document in the firm's archive is
579,000 characters, roughly 145,000 tokens, and a legal code would be several
times that; one `/resumo` on it was one uncapped bill, repeatable by anyone with
a rank, by typing three characters and a number.

The archive stays fully usable, as a source. A question about a stored document
is answered from retrieved chunks, which are bounded by construction: the number
of passages is capped, and only the passages that matched are read. What is no
longer possible is asking for an operation whose input is an entire file.

Text under review still enters as a `document` block. Pasted text is no more
trustworthy than a stored file, since it may itself have been drafted by the
opposing side, so it gets the same data-channel treatment and the same citation
support, and its size is bounded by `max_question_chars`.
"""
import logging

from core.config import settings
from core.integrations.llm import complete
from core.repositories.documents import log_access

from . import attachments, prompts, sources
from .rendering import document_block
from .state import LegalState

logger = logging.getLogger(__name__)

REVIEW_ERROR = "Não consegui revisar o texto agora. Tente novamente."

_PASTED_TITLE = "Texto enviado no chat"


# ── Graph node: review text pasted into the message ──────────────────────────

async def review(state: LegalState) -> dict:
    await log_access(
        state.conn, action="review_pasted", staff_id=state.staff_id,
        chat_id=state.chat_id, query=state.question[:500],
    )
    content = [
        document_block(_PASTED_TITLE, state.question),
        {"type": "text", "text": (
            "Revise o texto acima: aponte ambiguidades, cláusulas frágeis, "
            "lacunas e riscos, em ordem de gravidade. Cite os trechos."
        )},
    ]
    content = attachments.content_for(state, content)
    try:
        text = await complete(
            prompts.REVIEW, content,
            task="complex", model=settings.review_model, max_tokens=8000,
            **attachments.reservation_for(state),
        )
    except Exception:
        logger.exception("Review of pasted text failed")
        return {"answer": REVIEW_ERROR}
    if not text:
        return {"answer": REVIEW_ERROR}
    return {
        "answer": text,
        "sources": [sources.Source(
            kind="corpus",
            title=_PASTED_TITLE,
            detail="Revisão feita sobre o texto que você enviou nesta conversa, "
                   "sem consultar o acervo nem fontes externas.",
        )],
    }
