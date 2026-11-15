"""Agent 1 — the orchestrator: decides which specialist handles the message.

Routing is a cheap classification, so it runs through the simple LLM route with
JSON output. The parsed object is still validated by pydantic before it reaches
the graph.

When routing fails for any reason we fall back to `internal_research` — the
safest destination, because an empty corpus makes that agent say it found
nothing rather than improvise.
"""
import logging

from pydantic import BaseModel, Field

from core.config import settings
from core.integrations.datajud import extract_process_numbers
from core.integrations.llm import complete_json

from . import prompts
from .intents import classify_local_intent
from .state import LegalState, Task

logger = logging.getLogger(__name__)

# Below this, a "review" message is a request about a stored document rather
# than text pasted for correction — the shortest real paste seen is a clause.
_MIN_REVIEW_CHARS = 240


class RoutingDecision(BaseModel):
    """The orchestrator's output — shape enforced by the API, not by parsing."""

    task: Task = Field(description="Specialist that should handle the message.")
    query: str = Field(description="The message rewritten as a search query.")


def _adjust(task: Task, question: str, routed_by: str) -> tuple[Task, str]:
    """The two rules no model decision overrides, whichever router decided."""
    if extract_process_numbers(question, limit=1) and task == "internal_research":
        return "external_research", "rule:datajud-process-number"
    # The review agent only sees the message itself. A one-line request ("revise
    # aquele contrato") carries no text to review, so reviewing it would ask the
    # lawyer to paste a document the firm already stored: search the corpus.
    if task == "review" and len(question) < _MIN_REVIEW_CHARS:
        return "internal_research", "rule:review-without-text"
    return task, routed_by


async def orchestrate(state: LegalState) -> dict:
    # Already decided by the conversational router, with the conversation in
    # view: no second model call to decide the same thing again.
    if state.preset_task is not None:
        task, routed_by = _adjust(state.preset_task, state.question, "conversation")
        logger.info("Routed to %s with %s", task, routed_by)
        return {"task": task, "query": state.query or state.question, "routed_by": routed_by}

    local_intent = classify_local_intent(state.question)
    if local_intent is not None:
        logger.info("Routed to chat with deterministic intent %s", local_intent)
        return {
            "task": "chat",
            "query": state.question,
            "routed_by": f"rule:{local_intent}",
        }

    has_cnj_number = bool(extract_process_numbers(state.question, limit=1))
    prompt = (
        f"Team message:\n{state.question}\n\n"
        "Classify the task and rewrite the query. "
        'Return JSON only: {"task": "...", "query": "..."}'
    )
    try:
        data = await complete_json(
            prompts.ORCHESTRATOR, prompt,
            task="json", model=settings.router_model, max_tokens=512,
        )
        decision = RoutingDecision.model_validate(data or {})
    except Exception:
        logger.exception("Routing failed, defaulting to the corpus")
        fallback_task = "external_research" if has_cnj_number else "internal_research"
        return {"task": fallback_task, "query": state.question}

    # The schema guarantees a valid task; an empty query would silently search
    # for nothing, so fall back to the raw message there.
    query = decision.query.strip() or state.question
    task, routed_by = _adjust(decision.task, state.question, settings.router_model or "")
    logger.info("Routed to %s with %s", task, routed_by)
    return {"task": task, "query": query, "routed_by": routed_by}
