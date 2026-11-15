"""Internal legal assistant — five specialist agents behind one entry point.

    1. Orchestrator       routes the message (structured output, small model)
    2. Internal research  the firm's corpus: Redis cache -> pgvector -> citations
    3. External research  official web sources and DataJud
    4. Review             review and correction of contracts and pleadings
    5. Drafting           new text in the firm's own style

Security invariants, held by every agent:
- Document and web content enters as a `document` block — a data channel — and
  is never concatenated into the system prompt. A hostile document cannot issue
  instructions to the model.
- Every corpus access is recorded in `document_access_log`, append-only.
- Question length is capped before any model or embedding call.

The public surface below is unchanged from the single-module version, so the
Telegram handler and the CLI keep working as they are.
"""
from core.config import settings

from . import graph, sources
from .reply import AgentReply

__all__ = [
    "AgentReply", "answer_question", "sources",
]


async def answer_question(conn, question, matter=None, staff_id=None, chat_id=None,
                          staff_name="", routed=None, attachment_text: str | None = None) -> AgentReply:
    """Answer a staff message: route it (unless `routed` says how already),
    then run the specialist that fits."""
    question = str(question)[: settings.max_question_chars]
    return await graph.run(
        conn, question, matter=matter, staff_id=staff_id, chat_id=chat_id,
        staff_name=staff_name, routed=routed, attachment_text=attachment_text,
    )
