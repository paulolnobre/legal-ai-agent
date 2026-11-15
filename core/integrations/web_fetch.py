"""Reading an official page, not just its search-engine snippet.

Serper says a page exists and shows two lines of it. That is not enough to tell
a lawyer what a decision says — so the pages the search returned are fetched and
read here, under the same rules that let them through in the first place:

- only HTTPS URLs inside the operator's official-domain allowlist;
- the allowlist is re-checked on every redirect hop, so an official page cannot
  bounce the fetch to a domain nobody authorized;
- hard caps on bytes, time and pages, because a court site can serve a 200 MB
  case file;
- HTML and PDF only, extracted in a worker thread — parsing is CPU-bound and
  this process also answers the Telegram webhook.

This is fetching named pages, not crawling: nothing here follows links.
"""
from __future__ import annotations

import asyncio
import html
import io
import logging
import re
import time
from pathlib import PurePosixPath
from urllib.parse import urlsplit
from dataclasses import dataclass

import httpx

from core.config import settings
from core.integrations.serper import allowed_domains, is_allowed_url
from core.metrics import observe_external
from core.observability import traced

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None

CANARY_URL = "https://www.planalto.gov.br/ccivil_03/constituicao/constituicao.htm"

_SCRIPT_OR_STYLE = re.compile(
    r"<(script|style|noscript|template)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
# Revoked wording, the way Brazilian official sites keep it: the Planalto
# leaves each superseded article on the page, struck through, beside the one in
# force. Read as plain text the strike disappears and the two stand side by
# side, equally authoritative, which is how the firm's own copy of the
# Constitution came to carry the 12% interest cap of art. 192 that EC 40/2003
# removed. Measured on the pages this project reads: the Constitution has 773
# <strike> blocks and seven headings struck through by inline style, the
# tenancy law 22 and one article by style, the compiled Civil Code two.
#
# Each tag is named, never matched loosely: <s\b> must not take <span>, <sup>,
# <small> or <strong> with it, and <del\b> must not take <details>.
_STRUCK_TAG = re.compile(r"<(strike|s|del)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_STRUCK_STYLE = re.compile(
    r"""<([a-z][a-z0-9]*)\b[^>]*\bstyle\s*=\s*(["'])[^"']*line-through[^"']*\2[^>]*>""",
    re.IGNORECASE,
)
_BLOCK_END = re.compile(
    r"</(p|div|section|article|h[1-6]|li|tr|table|br)\s*>|<br\s*/?>", re.IGNORECASE
)
_TAG = re.compile(r"<[^>]+>")
_BLANK_LINES = re.compile(r"\n{3,}")


class FetchError(RuntimeError):
    """A safe failure raised by the restricted page reader."""


@dataclass(frozen=True)
class FetchedPage:
    url: str
    title: str
    text: str
    content_type: str
    truncated: bool


@dataclass(frozen=True)
class FetchedDocument:
    """A whole official document, as bytes, and where it was really read from."""

    url: str
    content_type: str
    data: bytes


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        timeout = httpx.Timeout(settings.fetch_timeout_seconds, connect=10.0)
        _client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,  # every hop is re-checked against the allowlist
            headers={"User-Agent": settings.fetch_user_agent},
        )
    return _client


def _html_title(markup: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", markup, re.IGNORECASE | re.DOTALL)
    return html.unescape(_TAG.sub("", match.group(1))).strip()[:200] if match else ""


def _without_struck(markup: str) -> str:
    """The page as it reads today: every struck-through passage removed.

    A styled element can hold elements of its own name (a <span> inside a
    <span>), so its end is found by counting, not by the first closing tag. When
    no end is found, which only malformed markup produces, just the opening tag
    goes: losing a revoked sentence to bad markup is recoverable, losing the law
    in force after it is not.
    """
    markup = _STRUCK_TAG.sub(" ", markup)
    kept: list[str] = []
    position = 0
    while (opening := _STRUCK_STYLE.search(markup, position)) is not None:
        same_name = re.compile(rf"<(/?){re.escape(opening.group(1))}\b[^>]*>", re.IGNORECASE)
        depth, end = 1, opening.end()
        for tag in same_name.finditer(markup, opening.end()):
            depth += -1 if tag.group(1) else 1
            if depth == 0:
                end = tag.end()
                break
        kept.append(markup[position:opening.start()])
        kept.append(" ")
        position = end
    kept.append(markup[position:])
    return "".join(kept)


def html_to_text(markup: str) -> str:
    """Readable text from a court or government page, without a parser dependency."""
    without_code = _without_struck(_SCRIPT_OR_STYLE.sub(" ", markup))
    with_breaks = _BLOCK_END.sub("\n", without_code)
    text = html.unescape(_TAG.sub(" ", with_breaks))
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return _BLANK_LINES.sub("\n\n", "\n".join(line for line in lines if line))


def pdf_to_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = reader.pages[: settings.fetch_max_pdf_pages]
    return "\n".join((page.extract_text() or "") for page in pages)


def _extract(data: bytes, content_type: str) -> tuple[str, str]:
    """Return (title, text) for a downloaded body. Runs in a worker thread."""
    if "pdf" in content_type:
        return "", pdf_to_text(data)
    markup = data.decode("utf-8", errors="replace")
    return _html_title(markup), html_to_text(markup)


async def _get_within_allowlist(url: str) -> httpx.Response:
    """GET following redirects by hand, checking the allowlist at every hop."""
    current = url
    for _ in range(settings.fetch_max_redirects + 1):
        if not is_allowed_url(current):
            raise FetchError("the URL is outside the authorized official domains")
        response = await _http().get(current)
        if response.is_redirect:
            location = response.headers.get("location")
            if not location:
                raise FetchError("the page answered with a redirect without a destination")
            current = str(httpx.URL(current).join(location))
            continue
        return response
    raise FetchError("the page redirected too many times")


@traced(name="web.fetch", run_type="tool")
async def fetch_page(url: str) -> FetchedPage:
    """Download and read one official page. Raises FetchError, never silently."""
    started = time.perf_counter()
    try:
        response = await _get_within_allowlist(url)
    except httpx.HTTPError as exc:
        observe_external("web_fetch", "error", time.perf_counter() - started)
        raise FetchError("the official page did not respond") from exc
    except FetchError:
        observe_external("web_fetch", "error", time.perf_counter() - started)
        raise

    if response.status_code != 200:
        observe_external("web_fetch", "error", time.perf_counter() - started)
        raise FetchError(f"the official page returned HTTP {response.status_code}")

    content_type = (response.headers.get("content-type") or "").lower()
    if not any(kind in content_type for kind in ("html", "pdf", "text/plain", "xml")):
        observe_external("web_fetch", "error", time.perf_counter() - started)
        raise FetchError(f"unsupported content type: {content_type or 'unknown'}")

    body = response.content[: settings.fetch_max_bytes]
    title, text = await asyncio.to_thread(_extract, body, content_type)
    truncated = len(response.content) > len(body) or len(text) > settings.fetch_max_chars
    text = text[: settings.fetch_max_chars].strip()
    if not text:
        observe_external("web_fetch", "error", time.perf_counter() - started)
        raise FetchError("no readable text was extracted from the page")

    observe_external("web_fetch", "success", time.perf_counter() - started)
    return FetchedPage(
        url=str(response.url),
        title=title or url,
        text=text,
        content_type=content_type.split(";")[0],
        truncated=truncated,
    )


async def fetch_pages(urls: list[str], limit: int | None = None) -> list[FetchedPage]:
    """Read several official pages at once, skipping the ones that fail."""
    maximum = limit if limit is not None else settings.fetch_max_pages
    selected = list(dict.fromkeys(urls))[:maximum]
    if not selected:
        return []
    results = await asyncio.gather(
        *(fetch_page(url) for url in selected), return_exceptions=True
    )
    pages: list[FetchedPage] = []
    for url, result in zip(selected, results, strict=True):
        if isinstance(result, FetchedPage):
            pages.append(result)
        elif isinstance(result, BaseException) and not isinstance(result, Exception):
            raise result
        else:
            logger.info("Could not read the official page %s: %s", url, result)
    return pages


async def canary() -> None:
    """Read one fixed official page, never a URL originating from a client."""
    domains = allowed_domains()
    url = CANARY_URL if is_allowed_url(CANARY_URL, domains) else f"https://{domains[0]}/"
    await fetch_page(url)


async def fetch_raw(url: str) -> bytes:
    """The untouched bytes behind an official URL, same rules as `fetch_page`.

    Used when the lawyer wants the file itself — the PDF of the decision — and
    not the text extracted from it.
    """
    response = await _get_within_allowlist(url)
    if response.status_code != 200:
        raise FetchError(f"the official page returned HTTP {response.status_code}")
    return response.content[: settings.fetch_max_bytes]


def filename_for(url: str, content_type: str) -> str:
    """A name the extractors recognise, from what the server said it sent.

    The content type decides, not the URL: the Planalto serves `.htm`, and a
    court may serve a PDF from a path with no extension at all.
    """
    stem = PurePosixPath(urlsplit(url).path).stem or "documento"
    if "pdf" in content_type:
        return f"{stem}.pdf"
    if "html" in content_type:
        return f"{stem}.html"
    return f"{stem}.txt"


@traced(name="web.fetch_document", run_type="tool")
async def fetch_document(url: str) -> FetchedDocument:
    """A whole official document, or an error: never the first part of one.

    `fetch_page` cuts a page to what fits beside a question, and `fetch_raw`
    cuts the bytes, and both are right for what they serve. Neither is right
    for a text that replaces a law in the corpus: the first five megabytes of
    a longer page would be indexed as if they were all of it. This refuses
    instead, at the limit an upload has.
    """
    started = time.perf_counter()
    try:
        response = await _get_within_allowlist(url)
    except httpx.HTTPError as exc:
        observe_external("web_fetch", "error", time.perf_counter() - started)
        raise FetchError("the official page did not respond") from exc
    except FetchError:
        observe_external("web_fetch", "error", time.perf_counter() - started)
        raise

    if response.status_code != 200:
        observe_external("web_fetch", "error", time.perf_counter() - started)
        raise FetchError(f"the official page returned HTTP {response.status_code}")
    content_type = (response.headers.get("content-type") or "").lower()
    if not any(kind in content_type for kind in ("html", "pdf", "text/plain")):
        observe_external("web_fetch", "error", time.perf_counter() - started)
        raise FetchError(f"unsupported content type: {content_type or 'unknown'}")
    if len(response.content) > settings.max_doc_mb * 1024 * 1024:
        observe_external("web_fetch", "error", time.perf_counter() - started)
        raise FetchError(f"the document is larger than {settings.max_doc_mb} MB")

    observe_external("web_fetch", "success", time.perf_counter() - started)
    return FetchedDocument(url=str(response.url), content_type=content_type,
                           data=response.content)


async def close() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except (httpx.HTTPError, OSError):
            pass
        _client = None
