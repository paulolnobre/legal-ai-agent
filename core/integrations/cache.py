"""Redis cache for the legal assistant: recent answers and query embeddings.

A firm with a dozen lawyers asks the same questions repeatedly. Caching the
finished answer means the second lawyer to ask pays neither the embedding call
nor the LLM call.

Two namespaces, different lifetimes:
- `legal:answer:<sha>` — a finished, already-cited answer (hours)
- `legal:embed:<sha>`  — the embedding of a query string (days; embeddings for
  the same text never change, only the model version would)

Every operation degrades to a no-op when Redis is unavailable: a cache outage
must never take the assistant down. After a failure the client backs off for
`_RETRY_AFTER` seconds instead of hammering a dead socket on every request.
"""
import hashlib
import json
import logging
import re
import time
import unicodedata
from datetime import UTC, datetime

import redis.asyncio as redis
from redis.exceptions import RedisError

from core.config import settings
from core.integrations.datajud import extract_process_numbers
from core.metrics import CACHE_REQUESTS

logger = logging.getLogger(__name__)

_RETRY_AFTER = 60  # seconds to stay offline after a connection failure

_client: redis.Redis | None = None
_offline_until: float = 0.0


def _normalize(text: str) -> str:
    """Fold a question down to its cache identity.

    Casing, accents, punctuation and spacing differ between people asking the
    same thing; none of them change the answer. This is an exact-match key
    after folding — semantic near-misses are still caught downstream by the
    vector search itself.
    """
    text = unicodedata.normalize("NFKD", str(text).casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# Bumped whenever the shape of a cached answer or the pipeline that produces it
# changes. Old entries then miss instead of being served under new rules.
ANSWER_SCHEMA_VERSION = "3"

# Bump this when code-level source-selection rules change (for example court
# routing or which type of page counts as read). Runtime settings are included
# in the fingerprint below, so changing an allowlist or coverage limit also
# invalidates external answers automatically.
EXTERNAL_SOURCE_POLICY_VERSION = "1"

_EXTERNAL_RESEARCH_TASKS = frozenset({"external_research", "combined_research"})
_FRESHNESS_MARKERS = re.compile(
    r"\b(?:agora|andamento|atual|atuais|atualmente|hoje|"
    r"recentes?|ultim[oa]s?)\b"
)


def _key(namespace: str, text: str, identity: str = "") -> str:
    """The cache identity of a question.

    `identity` carries everything besides the words that changes the answer —
    task, matter, model and pipeline version. Without it, an internal-research
    answer and an external-research answer to the same question collide, and a
    pipeline change keeps serving results built under the old rules.
    """
    normalized = _normalize(text)
    if namespace == "embed":
        normalized = f"{settings.embed_model}:{settings.embed_dim}:{normalized}"
    if identity:
        normalized = f"{identity}|{normalized}"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
    return f"legal:{namespace}:{digest}"


def _available() -> bool:
    return settings.cache_enabled and time.monotonic() >= _offline_until


def _mark_offline(op: str, exc: Exception) -> None:
    global _offline_until
    _offline_until = time.monotonic() + _RETRY_AFTER
    logger.warning("Cache %s failed, backing off %ss: %s", op, _RETRY_AFTER, exc)


def _redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    return _client


async def _get(namespace: str, text: str, identity: str = ""):
    if not _available():
        return None
    try:
        raw = await _redis().get(_key(namespace, text, identity))
    except (RedisError, OSError) as e:
        _mark_offline("get", e)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Discarding malformed cache entry in %s", namespace)
        return None


async def _set(namespace: str, text: str, value, ttl: int, identity: str = "") -> None:
    if not _available():
        return
    try:
        await _redis().set(_key(namespace, text, identity), json.dumps(value), ex=ttl)
    except (RedisError, OSError) as e:
        _mark_offline("set", e)


# ── Answers ──────────────────────────────────────────────────────────────────

def _source_policy_identity(task: str) -> str:
    """Fingerprint the evidence policy that made an external answer valid."""
    if task not in _EXTERNAL_RESEARCH_TASKS:
        return "corpus"

    domains = ",".join(sorted({
        domain.strip().casefold().rstrip(".")
        for domain in settings.serper_allowed_domains.split(",")
        if domain.strip()
    }))
    policy = "|".join([
        EXTERNAL_SOURCE_POLICY_VERSION,
        domains,
        settings.datajud_base_url.casefold().rstrip("/"),
        str(settings.datajud_max_processes),
        str(settings.datajud_max_movements),
        str(settings.serper_max_results),
        str(settings.serper_max_queries),
        settings.serper_search_url.casefold().rstrip("/"),
        str(settings.fetch_enabled).casefold(),
        str(settings.fetch_max_pages),
        str(settings.fetch_max_bytes),
        str(settings.fetch_max_chars),
        str(settings.fetch_max_pdf_pages),
        str(settings.fetch_max_redirects),
    ])
    digest = hashlib.sha256(policy.encode("utf-8")).hexdigest()[:16]
    return f"external-{EXTERNAL_SOURCE_POLICY_VERSION}-{digest}"


def _answer_identity(task: str, matter: str | None, model: str,
                     audience: str = "firm") -> str:
    """What, besides the words of the question, decides the answer.

    The audience is part of it since the archive gained folders: an answer
    built on somebody's private document belongs to that person alone, and
    serving it to the next colleague who asks the same question would hand
    over exactly what the folder exists to keep apart.
    """
    policy = _source_policy_identity(task)
    return f"v{ANSWER_SCHEMA_VERSION}:{task}:{matter or '-'}:{model}:{policy}:{audience}"


def bypass_answer_cache(text: str, task: str) -> bool:
    """Whether this request explicitly requires live external information.

    External research normally has a short cache lifetime. Process lookups and
    questions whose wording asks for the current state bypass answer caching
    completely, because even a short-lived result can be stale at the moment a
    lawyer asks. Internal research remains cacheable: it reflects the firm's
    versioned corpus, which is invalidated after ingestion.
    """
    if task not in _EXTERNAL_RESEARCH_TASKS:
        return False
    normalized = _normalize(text)
    return bool(_FRESHNESS_MARKERS.search(normalized) or extract_process_numbers(text))


def _answer_ttl(task: str) -> int:
    """Return the configured lifetime for this class of answer."""
    if task in _EXTERNAL_RESEARCH_TASKS:
        return settings.cache_external_answer_ttl
    return settings.cache_answer_ttl


async def get_answer(
    question: str,
    *,
    task: str,
    freshness_text: str | None = None,
    matter: str | None = None,
    model: str = "",
    audience: str = "firm",
) -> dict | None:
    """A recent answer to this question, with the sources it was built on.

    Returns the whole record or nothing: an answer served without its sources
    would silently break the rule that every answer carries its provenance —
    the second lawyer to ask would get a reply whose /fontes is empty.
    """
    if bypass_answer_cache(freshness_text or question, task):
        CACHE_REQUESTS.labels("answer", "bypass").inc()
        logger.info("Cache BYPASS (answer) task=%s reason=freshness", task)
        return None

    value = await _get("answer", question, _answer_identity(task, matter, model, audience))
    if not isinstance(value, dict) or not value.get("answer"):
        CACHE_REQUESTS.labels("answer", "miss").inc()
        return None
    CACHE_REQUESTS.labels("answer", "hit").inc()
    logger.info("Cache HIT (answer) task=%s", task)
    return value


async def set_answer(
    question: str,
    answer: str,
    *,
    task: str,
    sources: list | None = None,
    limitations: list[str] | None = None,
    freshness_text: str | None = None,
    matter: str | None = None,
    model: str = "",
    audience: str = "firm",
) -> None:
    """Store the answer together with its evidence and when it was produced."""
    if not answer or bypass_answer_cache(freshness_text or question, task):
        return
    record = {
        "answer": answer,
        "sources": sources or [],
        "limitations": limitations or [],
        "task": task,
        "model": model,
        "source_policy": _source_policy_identity(task),
        "cached_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    await _set(
        "answer", question, record, _answer_ttl(task),
        _answer_identity(task, matter, model, audience),
    )


async def invalidate_answers() -> int:
    """Drop every cached answer. Call after ingesting new documents.

    A cached answer was true for the corpus as it stood; new documents can
    change it, so the corpus growing invalidates the whole answer namespace.
    Embeddings are untouched — they depend only on the text.
    """
    if not _available():
        return 0
    removed = 0
    try:
        client = _redis()
        async for key in client.scan_iter(match="legal:answer:*", count=500):
            removed += await client.delete(key)
    except (RedisError, OSError) as e:
        _mark_offline("invalidate", e)
        return removed
    logger.info("Invalidated %s cached answers", removed)
    return removed


# ── Query embeddings ─────────────────────────────────────────────────────────

async def get_embedding(text: str) -> list[float] | None:
    value = await _get("embed", text)
    if not isinstance(value, list) or not value:
        CACHE_REQUESTS.labels("embedding", "miss").inc()
        return None
    if len(value) != settings.embed_dim:
        # A model or dimension change makes the stored vector unusable.
        logger.warning("Discarding cached embedding with wrong dimension")
        CACHE_REQUESTS.labels("embedding", "invalid").inc()
        return None
    CACHE_REQUESTS.labels("embedding", "hit").inc()
    logger.debug("Cache HIT (embedding)")
    return [float(x) for x in value]


async def set_embedding(text: str, embedding: list[float]) -> None:
    if embedding:
        await _set("embed", text, list(embedding), settings.cache_embed_ttl)


# ── Shared rate limiting ─────────────────────────────────────────────────────
# The in-process limiter forgets everything on a restart and cannot see a second
# worker. Redis already holds the queue, so the counter lives there too, and the
# limit stays true no matter how many workers run.

async def rate_limit_hit(key, max_events: int, window_seconds: int) -> tuple[bool, bool]:
    """Count one event. Returns (allowed, should_notify), like `RateLimiter`.

    A Redis outage returns "allowed": an unavailable cache must never lock the
    firm out of its own assistant — the in-process limiter is still in front.
    """
    if not _available():
        return True, False
    counter = f"legal:rate:{key}"
    notified = f"legal:rate-notified:{key}"
    try:
        client = _redis()
        async with client.pipeline(transaction=True) as pipe:
            pipe.incr(counter)
            pipe.expire(counter, window_seconds, nx=True)
            count, _ = await pipe.execute()
        if int(count) <= max_events:
            await client.delete(notified)
            return True, False
        first_block = bool(await client.set(notified, "1", ex=window_seconds, nx=True))
        return False, first_block
    except (RedisError, OSError) as exc:
        _mark_offline("rate_limit", exc)
        return True, False


async def close() -> None:
    """Release the connection pool on shutdown."""
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except (RedisError, OSError):
            pass
        _client = None
