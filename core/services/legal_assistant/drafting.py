"""Agent 5 — drafting: writes new text in the firm's own voice.

The graph runs `retrieve` before this node, so whatever the corpus holds that
resembles the requested document arrives as reference material. That is the
point: a drafted clause should look like the clauses this firm already writes,
not like a generic template. With an empty corpus the agent still drafts — it
just has no house style to imitate yet.

Output is streamed: a full minuta is long enough that a non-streaming request
risks an HTTP timeout.
"""
import logging

from core.config import settings
from core.integrations.llm import complete
from core.repositories.documents import log_access

from . import attachments, prompts, sources, verification
from .rendering import document_block
from .state import LegalState

logger = logging.getLogger(__name__)

DRAFT_ERROR = "Não consegui redigir o texto agora. Tente novamente."


async def draft(state: LegalState) -> dict:
    content: list[dict] = []
    if state.hits:
        content.extend(
            document_block(h.title, h.content, h.doc_type) for h in state.hits
        )
        content.append({
            "type": "text",
            "text": (
                "Os documentos acima são do acervo do escritório e servem como "
                "referência de estilo e estrutura. Não os copie literalmente nem "
                "reaproveite dados de outros casos."
            ),
        })
    content.append({"type": "text", "text": state.question})

    await log_access(
        state.conn, action="draft", staff_id=state.staff_id, chat_id=state.chat_id,
        query=state.query[:500],
        document_ids=sorted({h.document_id for h in state.hits}) or None,
    )

    content = attachments.content_for(state, content)
    try:
        text = await complete(
            prompts.DRAFTING, content,
            task="complex", model=settings.drafting_model, max_tokens=12000,
            **attachments.reservation_for(state),
        )
    except Exception:
        logger.exception("Drafting failed")
        return {"answer": DRAFT_ERROR}

    logger.info("Drafted with %s reference passage(s)", len(state.hits))
    if not text:
        return {"answer": DRAFT_ERROR}
    return {
        "answer": text,
        "sources": [sources.from_corpus_hit(hit) for hit in state.hits],
        "evidence": verification.evidence_from(content),
    }
