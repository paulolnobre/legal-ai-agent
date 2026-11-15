"""Embeddings via OpenAI text-embedding-3-large (reduced to EMBED_DIM), async.

Intentionally isolated: switching providers (e.g. Voyage voyage-law-2) means
reimplementing embed_texts() here only — nothing else changes.
"""
import logging
import time

import httpx

from core.config import EMBED_DIM, EMBED_MODEL, OPENAI_EMBED_URL, OPENAI_KEY
from core.metrics import observe_external
from core.observability import traced

logger = logging.getLogger(__name__)

_BATCH = 128  # inputs per request, keeps payloads bounded

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=60.0)
    return _client


def _trace_inputs(inputs: dict) -> dict:
    texts = inputs.get("texts") or []
    return {
        "items": len(texts),
        "characters": sum(len(str(item)) for item in texts),
        "content_redacted": True,
    }


def _trace_outputs(outputs: list[list[float]] | None) -> dict:
    return {
        "vectors": len(outputs or []),
        "dimensions": len(outputs[0]) if outputs else 0,
        "content_redacted": True,
    }


@traced(
    name="openai.embeddings",
    run_type="embedding",
    process_inputs=_trace_inputs,
    process_outputs=_trace_outputs,
)
async def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Generate embeddings for a list of texts. Returns None on failure."""
    if not texts:
        return []
    started = time.perf_counter()
    out: list[list[float]] = []
    for i in range(0, len(texts), _BATCH):
        batch = texts[i:i + _BATCH]
        try:
            resp = await _http().post(
                OPENAI_EMBED_URL,
                headers={
                    "Authorization": f"Bearer {OPENAI_KEY}",
                    "Content-Type": "application/json",
                },
                json={"model": EMBED_MODEL, "input": batch, "dimensions": EMBED_DIM},
            )
        except httpx.HTTPError as e:
            observe_external("openai_embeddings", "error", time.perf_counter() - started)
            logger.error("Embeddings request failed: %s", e)
            return None

        if resp.status_code != 200:
            observe_external("openai_embeddings", "error", time.perf_counter() - started)
            logger.error("Embeddings API returned status %s", resp.status_code)
            return None

        data = resp.json().get("data", [])
        # The API preserves input order, but sort by index just in case.
        data.sort(key=lambda d: d["index"])
        out.extend(d["embedding"] for d in data)
    observe_external("openai_embeddings", "success", time.perf_counter() - started)
    return out


async def embed_query(text: str) -> list[float] | None:
    """Embedding for a single query."""
    result = await embed_texts([text])
    if result is None:
        return None
    return result[0] if result else None


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
