"""The legal assistant as a LangGraph state graph.

    START -> orchestrate -> ┬-> cache_lookup -> ┬-> (hit) ----------------> END
                            │                   └-> retrieve ─┐
                            ├-> retrieve ────────────────────┤
                            ├-> review ----------------------┼-> ... -> END
                            └-> chat ------------------------┘

    retrieve -> answer_internal          (internal_research)
             -> research_external        (combined_research — corpus + web)
             -> draft                    (draft)

Only `retrieve` fans out, and it does so on the task the orchestrator chose, so
the corpus is searched at most once per request no matter where the answer
ultimately comes from.

This module is wiring only — every node's behaviour lives in its own agent
module, and the routing predicates below are pure functions of the state, which
is what makes them testable without a database or an API key.
"""
import logging
import time

from langgraph.graph import END, START, StateGraph

from core.config import settings
from core.integrations import cache
from core.metrics import GRAPH_RUNS, GRAPH_SECONDS
from core.observability import correlation_id
from core.repositories.teams import scope_for
from core.services.budget import BudgetExceeded

from . import chat as chat_agent
from . import attachments, drafting, external_research, internal_research, review
from . import verification
from .orchestrator import orchestrate
from .reply import AgentReply
from .state import LegalState

logger = logging.getLogger(__name__)

GRAPH_ERROR = "Não consegui processar sua solicitação agora. Tente novamente."

# Reaching the pilot ceiling is not a failure, so it does not get the failure's
# wording. It is a limit the firm set, it says which window closed and when it
# reopens, and everything that needs no model keeps working.
BUDGET_REACHED = (
    "O orçamento do piloto para consultas ao modelo se esgotou {window}. "
    "Ele reabre {reopens}. Até lá continuo com o que não depende do modelo: "
    "/docs, /detalhes, /processo, /arquivo e /fontes."
)
_WINDOW_LABELS = {"day": "hoje", "month": "neste mês"}


def budget_reached(exc: BudgetExceeded) -> str:
    """The budget's message for a refused run, with its window and when it
    reopens."""
    return BUDGET_REACHED.format(window=_WINDOW_LABELS.get(exc.window, exc.window),
                                 reopens=exc.reopens)

# Only research answers are worth caching: they are the questions a dozen
# lawyers ask independently. A draft or a review is bespoke to its request.
CACHED_TASKS = frozenset({"internal_research", "external_research", "combined_research"})


# ── Routing predicates ───────────────────────────────────────────────────────

def route_after_orchestrate(state: LegalState) -> str:
    if state.task in CACHED_TASKS:
        return "cache_lookup"
    if state.task == "draft":
        return "retrieve"
    if state.task == "review":
        return "review"
    return "chat"


def route_after_cache(state: LegalState) -> str:
    if state.cached:
        return "done"
    # Pure web research needs no corpus lookup.
    if state.task == "external_research":
        return "research_external"
    return "retrieve"


def route_after_retrieve(state: LegalState) -> str:
    if state.answer:
        return "done"  # retrieval failed and already produced the message to send
    if state.task == "draft":
        return "draft"
    if state.task == "combined_research":
        return "research_external"
    return "answer_internal"


# ── Assembly ─────────────────────────────────────────────────────────────────

def build_graph():
    g = StateGraph(LegalState)

    g.add_node("orchestrate", orchestrate)
    g.add_node("cache_lookup", internal_research.cache_lookup)
    g.add_node("retrieve", internal_research.retrieve)
    g.add_node("answer_internal", internal_research.answer)
    g.add_node("research_external", external_research.research)
    g.add_node("review", review.review)
    g.add_node("draft", drafting.draft)
    g.add_node("chat", chat_agent.chat)

    g.add_edge(START, "orchestrate")
    g.add_conditional_edges("orchestrate", route_after_orchestrate, {
        "cache_lookup": "cache_lookup",
        "retrieve": "retrieve",
        "review": "review",
        "chat": "chat",
    })
    g.add_conditional_edges("cache_lookup", route_after_cache, {
        "retrieve": "retrieve",
        "research_external": "research_external",
        "done": END,
    })
    g.add_conditional_edges("retrieve", route_after_retrieve, {
        "answer_internal": "answer_internal",
        "research_external": "research_external",
        "draft": "draft",
        "done": END,
    })

    # Every agent that produces an answer goes through the citation check, and
    # none of them reaches END directly. That is the point of putting it here
    # rather than inside each agent: a new agent added later is wired to this
    # node by the same loop, so it cannot ship unchecked by omission.
    g.add_node("verify", verification.verify)
    for terminal in ("answer_internal", "research_external", "review", "draft", "chat"):
        g.add_edge(terminal, "verify")
    g.add_edge("verify", END)

    return g.compile()


_app = build_graph()


async def run(conn, question, matter=None, staff_id=None, chat_id=None,
              staff_name="", routed=None, attachment_text: str | None = None) -> AgentReply:
    """Run the full pipeline and return the reply, with the sources it used.

    `routed` is a conversation_router.TaskRoute: the task and query already
    decided, which orchestrate keeps instead of asking the model again.

    `attachment_text` is the reading or transcript of what the person
    attached (see `attachments`). An answer drawn from it belongs to that
    person alone, so with it the answer cache is neither read
    (`cache_lookup`) nor written (below).
    """
    state = LegalState(
        conn=conn, question=question, attachment_text=attachments.bounded(attachment_text),
        query=routed.query if routed is not None else question, matter=matter,
        staff_id=staff_id, chat_id=chat_id, staff_name=staff_name,
        scope=await scope_for(conn, staff_id),
        preset_task=routed.task if routed is not None else None,
    )
    started = time.perf_counter()
    try:
        out = await _app.ainvoke(
            state,
            config={
                "run_name": "legal-ai-agent",
                "tags": [settings.app_env],
                "metadata": {
                    "correlation_id": correlation_id(),
                    "environment": settings.app_env,
                    "release": settings.app_version,
                },
            },
        )
    except BudgetExceeded as exc:
        GRAPH_RUNS.labels("unknown", "budget", "false").inc()
        GRAPH_SECONDS.labels("unknown").observe(time.perf_counter() - started)
        # Logged as a warning, not an exception: nothing broke. The message
        # carries no question, client or answer content, per the pilot policy.
        logger.warning("Refused: the %s pilot budget is spent", exc.window)
        return AgentReply(text=budget_reached(exc))
    except Exception:
        GRAPH_RUNS.labels("unknown", "error", "false").inc()
        GRAPH_SECONDS.labels("unknown").observe(time.perf_counter() - started)
        logger.exception("Legal graph execution failed")
        return AgentReply(text=GRAPH_ERROR)

    task = out.get("task", "")
    cached = bool(out.get("cached"))
    GRAPH_RUNS.labels(task or "unknown", "success", str(cached).lower()).inc()
    GRAPH_SECONDS.labels(task or "unknown").observe(time.perf_counter() - started)
    answer = out.get("answer") or ""
    if not answer:
        logger.error("Graph finished with no answer (task=%s)", task)
        return AgentReply(text=GRAPH_ERROR, task=task)

    # Cache the finished answer keyed on the reformulated query, so a colleague
    # phrasing the same question differently still lands on this entry.
    if (
        task in CACHED_TASKS
        and not state.attachment_text
        and not out.get("cached")
        and out.get("cacheable", True)
    ):
        collected = out.get("sources") or []
        await cache.set_answer(
            out.get("query") or question,
            answer,
            task=task,
            sources=[source.model_dump() for source in collected],
            limitations=out.get("limitations") or [],
            freshness_text=question,
            matter=out.get("matter"),
            model=internal_research.answer_model(task),
            audience=internal_research.audience_for_hits(staff_id, out.get("hits")),
        )

    return AgentReply(
        text=answer,
        sources=out.get("sources") or [],
        limitations=out.get("limitations") or [],
        task=task,
        cached=cached,
    )
