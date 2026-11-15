"""Agent 2 — internal research: the firm's own corpus, with a cache in front.

Three nodes, in the order the graph runs them:

1. `cache_lookup` — has someone already asked this? Answer costs nothing.
2. `retrieve`     — embed the query (cached too) and search pgvector. Records
                    the access in the append-only audit log.
3. `answer`       — generate the reply from the retrieved passages, with
                    native citations back to the source text.

`retrieve` is shared: the drafting and external-research agents both consume
its hits, so the corpus is searched once per request regardless of destination.
"""
import logging

from core.config import RAG_TOP_K, settings
from core.integrations import cache
from core.integrations.embeddings import embed_query
from core.integrations.llm import complete
from core.metrics import RAG_HITS
from core.repositories.documents import log_access, search_chunks

from . import attachments, prompts, sources, verification
from .rendering import document_block
from .state import LegalState

logger = logging.getLogger(__name__)

RETRIEVAL_ERROR = "Não consegui processar a busca no acervo agora. Tente novamente."
NO_RESULTS = "Não encontrei nada no acervo sobre isso."


async def _embed_cached(text: str) -> list[float] | None:
    """Embed a query, reusing the vector when the same text was embedded before.

    Embeddings for a given string never change, so this is a pure win: repeated
    questions skip the OpenAI round trip entirely.
    """
    cached_vector = await cache.get_embedding(text)
    if cached_vector is not None:
        return cached_vector

    vector = await embed_query(text)
    if vector is not None:
        await cache.set_embedding(text, vector)
    return vector


# ── Nodes ────────────────────────────────────────────────────────────────────

def answer_model(task: str) -> str:
    """The model that produces the answer for this task.

    Part of the cache identity: an answer written by a different model is a
    different answer, and must not be served from an entry the old one wrote.
    """
    if task == "external_research":
        return settings.research_model or settings.legal_model or ""
    return settings.legal_model or ""


def audience_of(staff_id) -> str:
    """The cache audience for one person's own folders."""
    return f"staff:{staff_id}" if staff_id is not None else "firm"


def audience_for_hits(staff_id, hits) -> str:
    """Who an answer built on these passages may be served to.

    Every passage from the firm's shelf: anybody may have it. One passage from
    a private or team folder: only the person who asked.
    """
    if any(getattr(hit, "visibility", "firm") != "firm" for hit in hits or []):
        return audience_of(staff_id)
    return "firm"


async def cache_lookup(state: LegalState) -> dict:
    """Short-circuit the whole pipeline on a repeated question."""
    if state.attachment_text:
        return {}
    record = None
    # Two lookups, narrow first: an answer this person's own folder produced is
    # stored under their name, and only an answer built on what the whole firm
    # can see is shared with the whole firm.
    for audience in (audience_of(state.staff_id), "firm"):
        record = await cache.get_answer(
            state.query,
            task=state.task,
            freshness_text=state.question,
            matter=state.matter,
            model=answer_model(state.task),
            audience=audience,
        )
        if record:
            break
    if not record:
        return {}
    # The sources travel with the answer. Serving the text alone would leave the
    # reply with nothing behind it and an empty /fontes — the same question,
    # answered worse, only because someone asked it first.
    cached_sources = []
    for raw in record.get("sources") or []:
        try:
            cached_sources.append(sources.Source(**raw))
        except (TypeError, ValueError):
            logger.warning("Discarding a malformed cached source")
    cached_limitations = [
        limitation.strip()
        for limitation in record.get("limitations") or []
        if isinstance(limitation, str) and limitation.strip()
    ]
    cached_answer = record["answer"]
    # A cache hit still touches the corpus from the firm's point of view, so it
    # is still an access worth recording — without it the log would understate
    # who read what.
    await log_access(
        state.conn, action="query_cached", staff_id=state.staff_id,
        chat_id=state.chat_id, query=state.query,
    )
    return {
        "answer": cached_answer,
        "sources": cached_sources,
        "limitations": cached_limitations,
        "cached": True,
    }


async def retrieve(state: LegalState) -> dict:
    """Semantic search over the corpus. Populates `hits` for downstream agents."""
    vector = await _embed_cached(state.query)
    if vector is None:
        return ({"hits": [], "limitations": [RETRIEVAL_ERROR]}
                if state.attachment_text else {"answer": RETRIEVAL_ERROR})

    hits = await search_chunks(
        state.conn, vector, top_k=RAG_TOP_K, matter=state.matter, scope=state.scope,
    )
    RAG_HITS.observe(len(hits))
    if not hits:
        logger.info("No corpus hits for the legal query")
        # Not an error: for `internal_research` this is the honest answer, and the
        # graph lets the other tasks continue without corpus context.
        return {"hits": []}

    await log_access(
        state.conn, action="query", staff_id=state.staff_id, chat_id=state.chat_id,
        query=state.query, document_ids=sorted({h.document_id for h in hits}),
    )
    logger.info("Retrieved %s passages for the legal query", len(hits))
    return {"hits": hits}


async def answer(state: LegalState) -> dict:
    """Answer strictly from the retrieved passages."""
    if not state.hits and not state.attachment_text:
        return {"answer": NO_RESULTS}

    content = [document_block(h.title, h.content, h.doc_type) for h in state.hits]
    content.append({"type": "text", "text": state.question})

    content = attachments.content_for(state, content)
    text = await complete(
        prompts.INTERNAL_RESEARCH, content,
        task="complex", model=settings.legal_model, max_tokens=4000,
        **attachments.reservation_for(state),
    )
    if not text:
        return {"answer": RETRIEVAL_ERROR}
    return {
        "answer": text,
        "sources": [sources.from_corpus_hit(hit) for hit in state.hits],
        "evidence": verification.evidence_from(content),
    }
