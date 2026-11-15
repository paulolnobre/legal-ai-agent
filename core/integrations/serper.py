"""Restricted Serper client for official Brazilian legal sources.

Every query is constrained with Google ``site:`` operators and every returned
URL is checked again locally. Search-engine snippets are reference material,
never instructions or a substitute for opening the official source.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from core.config import settings
from core.metrics import observe_external
from core.observability import traced

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None

CANARY_QUERY = "Constituição Federal"
CANARY_DOMAIN = "planalto.gov.br"

_TARGET_SITES = {
    "stf": "stf.jus.br",
    "supremo tribunal federal": "stf.jus.br",
    "stj": "stj.jus.br",
    "superior tribunal de justiça": "stj.jus.br",
    "tst": "tst.jus.br",
    "tribunal superior do trabalho": "tst.jus.br",
    "tse": "tse.jus.br",
    "tribunal superior eleitoral": "tse.jus.br",
    "cnj": "cnj.jus.br",
    "conselho nacional de justiça": "cnj.jus.br",
    "planalto": "planalto.gov.br",
    "câmara": "camara.leg.br",
    "camara": "camara.leg.br",
    "câmara dos deputados": "camara.leg.br",
    "camara dos deputados": "camara.leg.br",
    "senado": "senado.leg.br",
    "senado federal": "senado.leg.br",
    "oab": "oab.org.br",
    "ministério público": "mp.br",
    "ministerio publico": "mp.br",
    "defensoria": "def.br",
    "djen": "cnj.jus.br",
    "diário de justiça eletrônico": "cnj.jus.br",
    "diario de justica eletronico": "cnj.jus.br",
    "diario oficial": "in.gov.br",
    "diário oficial": "in.gov.br",
    "dou": "in.gov.br",
    "imprensa nacional": "in.gov.br",
    "lexml": "lexml.gov.br",
    # The TJPA's own diary. Named in a question, it is searched on its own
    # domain rather than across tjpa.jus.br, where the portal outranks it.
    "dje tjpa": "dje.tjpa.jus.br",
    "dje do tjpa": "dje.tjpa.jus.br",
    "diário da justiça do pará": "dje.tjpa.jus.br",
    "diario da justica do para": "dje.tjpa.jus.br",
}

# State and regional courts publish on their own subdomains of jus.br, so the
# institution named in the question decides which one to search: "TJSP" must
# not fall back to a generic jus.br sweep dominated by the national courts.
_COURT_PATTERNS = (
    (re.compile(r"(?<!\w)tj\s?([a-z]{2})(?!\w)"), "tj{0}.jus.br"),
    (re.compile(r"(?<!\w)trf\s?-?\s?(\d)(?!\w)"), "trf{0}.jus.br"),
    (re.compile(r"(?<!\w)trt\s?-?\s?(\d{1,2})(?!\w)"), "trt{0}.jus.br"),
    (re.compile(r"(?<!\w)tre\s?-?\s?([a-z]{2})(?!\w)"), "tre-{0}.jus.br"),
)


# Asking for case law is a different search from asking what a statute says.
# The umbrella domains answer the second well and the first not at all: what
# ranks on site:jus.br is institutional material, because a decision lives in a
# query system rather than on an indexed page.
_CASE_LAW_MARKERS = (
    "jurisprudencia", "jurisprudência", "acordao", "acórdão", "acordaos",
    "acórdãos", "precedente", "precedentes", "julgado", "julgados",
    "sumula", "súmula", "resp", "aresp", "agint", "ementa",
    "decisao do", "decisão do", "entendimento do", "entendimento dos",
    "decisoes", "decisões", "tribunais", "inteiro teor", "turma",
)

# Verified against the live index rather than assumed: scon.stj.jus.br returns
# inteiro-teor PDFs, stf.jus.br returns real pronouncements, and
# jurisprudencia.stf.jus.br — the obvious guess — returns nothing at all.
_CASE_LAW_SITES = ("scon.stj.jus.br", "stf.jus.br")

# An institution's own inteiro-teor index, used when that institution is the
# one being asked about. Only entries confirmed to carry indexed decisions
# belong here.
_CASE_LAW_INDEX = {"stj.jus.br": "scon.stj.jus.br"}

# Phrases addressed to the assistant rather than to the search engine. They cost
# recall when they reach Google: the full conversational question returned one
# irrelevant bulletin where the terms alone returned inteiro-teor decisions.
_INSTRUCTION_PHRASES = re.compile(
    r"(?:você pode|voce pode|vc pode|pode|poderia|gostaria de|preciso que|"
    r"quero que|faça|faca|fazer)\s+(?:uma\s+|um\s+)?"
    r"(?:nova\s+)?(?:pesquisa|busca|levantamento|consulta)[^:.]*[:.]?",
    re.IGNORECASE,
)
_ASIDES = re.compile(
    r"(?:não invente[^.]*\.?|nao invente[^.]*\.?|não crie[^.]*\.?|"
    r"nao crie[^.]*\.?|por favor|obrigad[oa])",
    re.IGNORECASE,
)
_TOPIC_LEAD = re.compile(
    r"^.*?\b(?:sobre o seguinte tema|sobre o tema|sobre|a respeito de|tema)\s*:\s*",
    re.IGNORECASE | re.DOTALL,
)


def wants_case_law(query: str) -> bool:
    """Whether this question is asking for decisions rather than for a rule."""
    lowered = query.casefold()
    return any(marker in lowered for marker in _CASE_LAW_MARKERS)


def search_terms(query: str) -> str:
    """The searchable part of a question asked in conversation.

    A lawyer writes to a colleague, not to an index: "Você pode fazer uma
    pesquisa de jurisprudência sobre o seguinte tema: X. Não invente decisões."
    Sent verbatim, the wrapper competes with X for ranking. Only the wrapper is
    removed — the legal terms are never rewritten, so nothing is searched that
    the person did not ask for.
    """
    text = " ".join(str(query or "").split())
    if not text:
        return ""
    topic = _TOPIC_LEAD.sub("", text, count=1)
    if topic.strip():
        text = topic
    text = _INSTRUCTION_PHRASES.sub(" ", text)
    text = _ASIDES.sub(" ", text)
    cleaned = " ".join(text.split()).strip(" .,:;-–—")
    # Stripping must never empty the query; the original still beats nothing.
    return cleaned or " ".join(str(query or "").split())


class SerperError(RuntimeError):
    """A safe failure raised by the restricted web-search integration."""


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    date: str | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        timeout = httpx.Timeout(settings.serper_timeout_seconds, connect=10.0)
        _client = httpx.AsyncClient(timeout=timeout)
    return _client


def allowed_domains() -> tuple[str, ...]:
    """Return normalized domain suffixes configured by the operator."""
    domains: list[str] = []
    for raw in settings.serper_allowed_domains.split(","):
        domain = raw.strip().lower().lstrip(".")
        if domain and domain not in domains:
            domains.append(domain)
    if not domains:
        raise SerperError("no official domain was authorized for search")
    return tuple(domains)


def is_allowed_url(url: str, domains: tuple[str, ...] | None = None) -> bool:
    """Accept only HTTPS URLs whose hostname is inside the official allowlist."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or port not in {None, 443}
        or not host
        or parsed.username
        or parsed.password
    ):
        return False
    permitted = domains or allowed_domains()
    return any(host == domain or host.endswith(f".{domain}") for domain in permitted)


def _domain_permitted(domain: str, permitted: tuple[str, ...]) -> bool:
    return any(domain == suffix or domain.endswith(f".{suffix}") for suffix in permitted)


def search_domains(
    query: str,
    domains: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """Prefer a named official institution, otherwise use broad official groups."""
    permitted = domains or allowed_domains()
    lowered = query.casefold()
    targeted: list[str] = []
    for marker, domain in _TARGET_SITES.items():
        mentioned = re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", lowered)
        if mentioned and _domain_permitted(domain, permitted):
            targeted.append(domain)
    for pattern, template in _COURT_PATTERNS:
        for match in pattern.finditer(lowered):
            domain = template.format(match.group(1))
            if _domain_permitted(domain, permitted):
                targeted.append(domain)

    maximum = max(1, settings.serper_max_queries)
    if wants_case_law(query):
        if targeted:
            # A named court is the question. Adding another court's index would
            # answer about an institution nobody asked about, so the search
            # stays inside the named one — plus its own decision index.
            ordered: list[str] = []
            for domain in targeted:
                ordered.append(domain)
                index = _CASE_LAW_INDEX.get(domain)
                if index and _domain_permitted(index, permitted):
                    ordered.append(index)
            return tuple(dict.fromkeys(ordered))[:maximum]
        # No court named: go where decisions actually are, keeping the umbrella
        # last as a catch-all for the courts with no entry here.
        ordered = [d for d in _CASE_LAW_SITES if _domain_permitted(d, permitted)]
        ordered.extend(permitted)
        return tuple(dict.fromkeys(ordered))[:maximum]
    if targeted:
        return tuple(dict.fromkeys(targeted))[:maximum]
    return permitted[:maximum]


def clip_to_words(text: str, limit: int) -> str:
    """At most ``limit`` characters, never cutting through a word.

    The old cut landed mid-word ("Hackers explora"), which asks the index for a
    fragment nothing contains. A whole word is worth more than the character
    that completes it.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    head = text[:limit].rsplit(" ", 1)[0].strip()
    return head or text[:limit].strip()


def restricted_query(
    query: str,
    domain: str,
    domains: tuple[str, ...] | None = None,
) -> str:
    """Add one free-tier-compatible ``site:`` restriction to the query."""
    clean_query = clip_to_words(search_terms(query), settings.serper_max_query_chars)
    if not clean_query:
        raise SerperError("the external search query is empty")
    permitted = domains or allowed_domains()
    normalized_domain = domain.strip().lower().lstrip(".")
    if not _domain_permitted(normalized_domain, permitted):
        raise SerperError("the search attempted to use an unauthorized domain")
    return f"site:{normalized_domain} {clean_query}"


async def _search_domain(
    query: str,
    domain: str,
    key: str,
    domains: tuple[str, ...],
) -> list[dict]:
    payload = {
        "q": restricted_query(query, domain, domains),
        "gl": "br",
        "hl": "pt",
        "num": 10,
    }
    try:
        response = await _http().post(
            settings.serper_search_url,
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
            json=payload,
        )
    except httpx.HTTPError as exc:
        raise SerperError("Serper did not respond") from exc

    if response.status_code in {401, 403}:
        raise SerperError("Serper rejected the configured key")
    if response.status_code == 429:
        raise SerperError("the Serper query limit was reached")
    if response.status_code != 200:
        raise SerperError(f"Serper returned HTTP {response.status_code}")

    try:
        organic = response.json().get("organic", [])
    except (TypeError, ValueError, AttributeError) as exc:
        raise SerperError("Serper returned an invalid response") from exc
    return organic if isinstance(organic, list) else []


def _vetted(
    organic: list, domains: tuple[str, ...], max_results: int,
) -> tuple[list[SearchResult], int]:
    """Only HTTPS URLs inside `domains`, each once, at most `max_results`.

    One definition for every search this module runs. The diary search passes a
    single-domain tuple, so a page elsewhere on jus.br is not accepted as a
    publication of that diary just because the allowlist would permit it.
    """
    results: list[SearchResult] = []
    seen: set[str] = set()
    rejected = 0
    for item in organic:
        if not isinstance(item, dict):
            continue
        url = str(item.get("link") or "").strip()
        if not is_allowed_url(url, domains):
            rejected += 1
            continue
        if url in seen:
            continue
        seen.add(url)
        results.append(SearchResult(
            title=str(item.get("title") or url).strip(),
            url=url,
            snippet=str(item.get("snippet") or "").strip(),
            date=str(item["date"]).strip() if item.get("date") else None,
        ))
        if len(results) >= max_results:
            break
    return results, rejected


def _trace_search_inputs(inputs: dict) -> dict:
    return {"query_chars": len(str(inputs.get("query") or "")), "content_redacted": True}


def _trace_search_outputs(outputs: list[SearchResult]) -> dict:
    return {"results": len(outputs)}


@traced(
    name="serper.search",
    run_type="tool",
    process_inputs=_trace_search_inputs,
    process_outputs=_trace_search_outputs,
)
async def _one_pass(
    query: str,
    selected_domains: tuple[str, ...],
    key: str,
    domains: tuple[str, ...],
) -> tuple[list[dict], list[Exception]]:
    """One search across the chosen domains: what came back, and what broke."""
    responses = await asyncio.gather(
        *(_search_domain(query, domain, key, domains) for domain in selected_domains),
        return_exceptions=True,
    )
    organic: list[dict] = []
    failures: list[Exception] = []
    for domain, response in zip(selected_domains, responses, strict=True):
        if isinstance(response, asyncio.CancelledError):
            raise response
        if isinstance(response, Exception):
            logger.warning("Serper search failed for site:%s: %s", domain, response)
            failures.append(response)
            continue
        if isinstance(response, BaseException):
            raise response
        organic.extend(response)
    return organic, failures


async def search(query: str) -> list[SearchResult]:
    """Search Google through Serper and return only vetted official URLs."""
    key = (settings.serper_api_key or "").strip()
    if not key:
        raise SerperError("the Serper API key is not configured")

    domains = allowed_domains()
    started = time.perf_counter()
    max_results = max(1, settings.serper_max_results)
    selected_domains = search_domains(query, domains)

    organic, failures = await _one_pass(query, selected_domains, key, domains)
    if not organic and failures:
        observe_external("serper", "error", time.perf_counter() - started)
        raise failures[0]
    results, rejected = _vetted(organic, domains, max_results)

    # Nothing came back, and nothing broke: the index simply had no answer for
    # the question as asked. A long query is the usual reason, because a search
    # engine stops answering well before it complains, so the one thing worth
    # trying is the same question asked shorter. Measured on the question that
    # exposed this: 0 results at 304 characters, 8 at 63.
    #
    # Only once, and only when the first attempt found nothing at all. A retry
    # after partial results would spend a call to improve an answer that
    # already exists.
    terms = search_terms(query)
    shorter = clip_to_words(terms, settings.serper_retry_query_chars)
    retried = (
        not results
        and not failures
        and bool(shorter)
        and shorter != clip_to_words(terms, settings.serper_max_query_chars)
    )
    if retried:
        organic, failures = await _one_pass(shorter, selected_domains, key, domains)
        results, rejected = _vetted(organic, domains, max_results)

    logger.info(
        "Restricted Serper search accepted=%d rejected=%d searched=%s retried=%s",
        len(results),
        rejected,
        ",".join(selected_domains),
        retried,
    )
    observe_external("serper", "success", time.perf_counter() - started)
    return results


@traced(
    name="serper.search_site",
    run_type="tool",
    process_inputs=_trace_search_inputs,
    process_outputs=_trace_search_outputs,
)
async def search_site(query: str, domain: str) -> list[SearchResult]:
    """One Google search restricted to one site the caller chose.

    For a search whose scope is decided by the code rather than inferred from
    the question: the diary of the court DataJud identified. The domain must
    still be inside the operator's allowlist, and every URL returned must be on
    that domain itself, not merely somewhere the allowlist would accept.
    """
    key = (settings.serper_api_key or "").strip()
    if not key:
        raise SerperError("the Serper API key is not configured")
    started = time.perf_counter()
    try:
        organic = await _search_domain(query, domain, key, allowed_domains())
    except SerperError:
        observe_external("serper", "error", time.perf_counter() - started)
        raise
    results, rejected = _vetted(organic, (domain,), max(1, settings.serper_max_results))
    logger.info(
        "Site-restricted Serper search accepted=%d rejected=%d", len(results), rejected,
    )
    observe_external("serper", "success", time.perf_counter() - started)
    return results


async def canary() -> None:
    """Make exactly one fixed search request, never using client data."""
    key = (settings.serper_api_key or "").strip()
    if not key:
        raise SerperError("the Serper API key is not configured")
    domains = allowed_domains()
    domain = (
        CANARY_DOMAIN
        if _domain_permitted(CANARY_DOMAIN, domains)
        else domains[0]
    )
    await _search_domain(CANARY_QUERY, domain, key, domains)


def format_result(result: SearchResult) -> str:
    lines = [
        "Search result from an authorized official domain",
        f"Title: {result.title}",
        f"URL: {result.url}",
    ]
    if result.date:
        lines.append(f"Date reported by search: {result.date}")
    lines.extend([
        f"Search-engine snippet: {result.snippet or 'not available'}",
        "Source limitation: this block contains a title and search-index snippet; "
        "it does not represent a full reading of the official page.",
    ])
    return "\n".join(lines)


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
