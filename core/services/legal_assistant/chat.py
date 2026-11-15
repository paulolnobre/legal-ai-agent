"""The no-work-requested path: greetings, thanks, "what can you do?".

Not one of the five specialists — it exists so a "bom dia" doesn't trigger an
embedding call, a vector search and a web search. It is also the only node with
no document channel: nothing is retrieved, so there is nothing to cite and no
injected content to defend against.
"""
import logging

from core.config import settings
from core.integrations.llm import complete

from . import attachments, prompts
from .state import LegalState

logger = logging.getLogger(__name__)

FALLBACK = "Pois não. Em que posso ajudar?"


async def chat(state: LegalState) -> dict:
    try:
        text = await complete(
            prompts.CHAT, attachments.content_for(
                state, [{"type": "text", "text": state.question}]),
            task="simple", model=settings.router_model, max_tokens=300,
            **attachments.reservation_for(state),
        )
    except Exception:
        logger.exception("Chat reply failed")
        return {"answer": FALLBACK}
    return {"answer": (text or "").strip() or FALLBACK}
