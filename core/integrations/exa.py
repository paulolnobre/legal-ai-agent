"""Exa as an alternative discovery layer, behind a flag, never a replacement.

Exa answers the same question Serper answers — which official pages exist for
this query — and returns the same shape, so the two are interchangeable at the
call site. What changes is how they get there: Serper spends one Google query
per domain, because the plan cannot combine `site:` operators, while Exa takes
the whole allowlist in a single call. Measured on the same question, coverage
came out equivalent; the difference was three calls versus one, and highlights
that carry the acórdão's own ementa rather than a search index's blurb.

The allowlist and the URL check are imported from :mod:`serper` rather than
written again. Two copies of a security boundary drift, and the drift is
invisible until the day one of them lets something through. There is one
definition of what counts as an official domain, and both providers answer to
it — including the check on every URL a provider returns, because
`includeDomains` is Exa's promise, not our verification.

DataJud stays the authority for a process number and the page reader stays the
thing that confirms official content. This layer only finds candidates.
"""
import logging
import time

import httpx

from core.config import settings
from core.integrations.serper import (
    SearchResult,
    allowed_domains,
    is_allowed_url,
    search_terms,
)
from core.metrics import observe_external
from core.observability import traced

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


class ExaError(RuntimeError):
    """A safe failure raised by the Exa discovery integration."""


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        timeout = httpx.Timeout(settings.exa_timeout_seconds, connect=10.0)
        _client = httpx.AsyncClient(timeout=timeout)
    return _client


def _first_highlight(item: dict) -> str:
    """The best excerpt Exa returned, or nothing.

    Highlights are the reason this provider is worth measuring: they are text
    from the page, so a result nobody opens still carries the ementa rather
    than an index summary. Only the first is kept — the others repeat the same
    passage often enough that they cost tokens without adding evidence.
    """
    highlights = item.get("highlights")
    if isinstance(highlights, list) and highlights:
        first = highlights[0]
        if isinstance(first, str):
            return " ".join(first.split())[:600]
    return ""


def _vetted(
    items: list, domains: tuple[str, ...], max_results: int,
) -> tuple[list[SearchResult], int]:
    """Only HTTPS URLs inside `domains`, each once, at most `max_results`.

    `includeDomains` is Exa's promise; this is our verification, and the diary
    search narrows it to the diary's own domain.
    """
    results: list[SearchResult] = []
    seen: set[str] = set()
    rejected = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not is_allowed_url(url, domains):
            rejected += 1
            continue
        if url in seen:
            continue
        seen.add(url)
        published = item.get("publishedDate")
        results.append(SearchResult(
            title=str(item.get("title") or url).strip(),
            url=url,
            snippet=_first_highlight(item),
            date=str(published).strip() if published else None,
        ))
        if len(results) >= max_results:
            break
    return results, rejected


async def _request(key: str, payload: dict, started: float) -> list:
    """One call to Exa, with every failure turned into an ExaError."""
    try:
        response = await _http().post(
            settings.exa_search_url,
            headers={"x-api-key": key, "Content-Type": "application/json"},
            json=payload,
        )
    except httpx.HTTPError as exc:
        observe_external("exa", "error", time.perf_counter() - started)
        raise ExaError("Exa did not respond") from exc

    if response.status_code in {401, 403}:
        observe_external("exa", "error", time.perf_counter() - started)
        raise ExaError("Exa rejected the configured key")
    if response.status_code == 429:
        observe_external("exa", "error", time.perf_counter() - started)
        raise ExaError("the Exa query limit was reached")
    if response.status_code != 200:
        observe_external("exa", "error", time.perf_counter() - started)
        raise ExaError(f"Exa returned HTTP {response.status_code}")

    try:
        body = response.json()
    except ValueError as exc:
        observe_external("exa", "error", time.perf_counter() - started)
        raise ExaError("Exa returned an invalid response") from exc

    items = body.get("results") if isinstance(body, dict) else None
    return items if isinstance(items, list) else []


def _trace_inputs(inputs: dict) -> dict:
    return {"query_chars": len(str(inputs.get("query") or "")), "content_redacted": True}


def _trace_outputs(outputs: list[SearchResult]) -> dict:
    return {"results": len(outputs)}


@traced(
    name="exa.search",
    run_type="tool",
    process_inputs=_trace_inputs,
    process_outputs=_trace_outputs,
)
async def search(query: str) -> list[SearchResult]:
    """Search through Exa and return only vetted official URLs."""
    key = (settings.exa_api_key or "").strip()
    if not key:
        raise ExaError("the Exa API key is not configured")

    clean_query = search_terms(query)[:500]
    if not clean_query:
        raise ExaError("the external search query is empty")

    domains = allowed_domains()
    max_results = max(1, settings.exa_max_results)
    started = time.perf_counter()

    payload = {
        "query": clean_query,
        "type": settings.exa_search_type,
        "numResults": max_results,
        # Exa's own filter. The check below repeats it, because a provider's
        # promise about scope is not the same as our verification of it.
        "includeDomains": list(domains),
        "contents": {"highlights": True},
    }

    items = await _request(key, payload, started)

    results, rejected = _vetted(items, domains, max_results)

    logger.info(
        "Exa search accepted=%d rejected=%d domains=%d",
        len(results),
        rejected,
        len(domains),
    )
    observe_external("exa", "success", time.perf_counter() - started)
    return results


@traced(
    name="exa.search_site",
    run_type="tool",
    process_inputs=_trace_inputs,
    process_outputs=_trace_outputs,
)
async def search_site(query: str, domain: str) -> list[SearchResult]:
    """One search restricted to a single domain the caller chose.

    The fallback when Serper cannot find a publication in a court's diary. The
    domain must be inside the allowlist, Exa is asked to stay inside it, and
    what comes back is accepted only if it is on that domain.
    """
    key = (settings.exa_api_key or "").strip()
    if not key:
        raise ExaError("the Exa API key is not configured")
    clean_query = search_terms(query)[:500]
    if not clean_query:
        raise ExaError("the external search query is empty")
    if not is_allowed_url(f"https://{domain}/", allowed_domains()):
        raise ExaError("the search attempted to use an unauthorized domain")

    max_results = max(1, settings.exa_max_results)
    started = time.perf_counter()
    items = await _request(key, {
        "query": clean_query,
        "type": settings.exa_search_type,
        "numResults": max_results,
        "includeDomains": [domain],
        "contents": {"highlights": True},
    }, started)
    results, rejected = _vetted(items, (domain,), max_results)
    logger.info("Site-restricted Exa search accepted=%d rejected=%d", len(results), rejected)
    observe_external("exa", "success", time.perf_counter() - started)
    return results


async def canary() -> None:
    """Make exactly one fixed request, never using client data."""
    key = (settings.exa_api_key or "").strip()
    if not key:
        raise ExaError("the Exa API key is not configured")
    domains = allowed_domains()
    try:
        response = await _http().post(
            settings.exa_search_url,
            headers={"x-api-key": key, "Content-Type": "application/json"},
            json={
                "query": "constituição federal artigo 5",
                "type": settings.exa_search_type,
                "numResults": 1,
                "includeDomains": list(domains),
            },
        )
    except httpx.HTTPError as exc:
        raise ExaError("Exa did not respond") from exc
    if response.status_code != 200:
        raise ExaError(f"Exa returned HTTP {response.status_code}")


def format_result(result: SearchResult) -> str:
    """Render one result for the model, saying exactly what it is.

    The wording differs from Serper's on purpose: a highlight is text taken
    from the page, so calling it an index snippet would understate it, while
    calling it a full reading would overstate it. Neither is true, and the
    model has to be told which.
    """
    lines = [
        "Search result from an authorized official domain",
        f"Title: {result.title}",
        f"URL: {result.url}",
    ]
    if result.date:
        lines.append(f"Date reported by the provider: {result.date}")
    lines.extend([
        f"Excerpt taken from the page: {result.snippet or 'not available'}",
        "Source limitation: this excerpt comes from the page's own text, "
        "selected for the query, but it is not a full reading of the page.",
    ])
    return "\n".join(lines)


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


__all__ = ["ExaError", "canary", "close", "format_result", "search"]
