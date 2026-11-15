"""Shared state for the legal-assistant graph.

One object flows through every node. Nodes return partial dicts; LangGraph
merges them, so a node only declares the fields it actually produces.
"""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from core.models import SearchHit

from .sources import Source

# The orchestrator picks exactly one of these. Kept as a module-level tuple so
# the router's schema, the graph's edge map, and the tests all read from one
# source — adding a task without wiring an edge then fails loudly in tests.
TASKS = (
    "internal_research",  # answerable from the firm's own corpus
    "external_research",  # needs current public sources (law, case law, news)
    "combined_research",  # corpus first, then confirm/extend externally
    "draft",              # draft a document, clause or message
    "review",             # review text the lawyer pasted into the chat
    "chat",               # greeting, thanks, or a question about the assistant
)

Task = Literal[
    "internal_research",
    "external_research",
    "combined_research",
    "draft",
    "review",
    "chat",
]


class LegalState(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # ── Request ──────────────────────────────────────────────────────────────
    conn: Any                      # borrowed DB connection; the graph is in-memory
    question: str
    attachment_text: str | None = None
    matter: str | None = None
    staff_id: int | None = None
    # What this asker may see. Built once, in `run`, and carried through the
    # graph: the retrieval filter and the answer cache both need it.
    scope: Any = None
    chat_id: Any = None
    staff_name: str = ""

    # ── Routing (orchestrator) ───────────────────────────────────────────────
    task: Task = "internal_research"
    # A task the conversational router already chose; orchestrate keeps it.
    preset_task: Task | None = None
    query: str = ""                # question reformulated for retrieval/search
    routed_by: str = ""            # model that made the call, for the logs

    # ── Retrieval (internal research) ────────────────────────────────────────
    # Populated once by `retrieve` and read by whichever agent runs next — the
    # internal answer, the external researcher, or the drafter.
    hits: list[SearchHit] = []

    # The text of the document blocks whichever agent ran put in front of the
    # model. The citation check compares against exactly this, rather than
    # against `sources`, whose detail is trimmed for display: a citation that
    # is in the page body but not in a 300-character excerpt would otherwise
    # be reported as invented.
    evidence: list[str] = []

    # ── Output ───────────────────────────────────────────────────────────────
    answer: str = ""
    cached: bool = False
    cacheable: bool = True
    # What the answer was built on. Collected by whichever agent ran; the
    # handler decides how much of it to show.
    sources: list[Source] = []
    # Failures and coverage gaps that constrain how the answer may be used.
    limitations: list[str] = []
