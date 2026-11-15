"""Turning provider content blocks into the text the lawyer reads.

Every answer in this system is traceable: the model emits native citations and
we render them as `[n]` markers plus a sources section. Two citation shapes
can arrive here — passages from the firm's own documents, and web results from
a search tool — and both collapse into one numbered list so a mixed answer
reads as a single argument with a single bibliography.
"""
import logging

logger = logging.getLogger(__name__)

_SNIPPET_MAX = 200


def document_block(title, content, doc_type=None) -> dict:
    """A `document` content block with citations enabled.

    Document text is passed as a data block and never concatenated into the
    system prompt — that separation is what keeps a malicious document from
    issuing instructions to the model.
    """
    label = f"{title}" + (f" ({doc_type})" if doc_type else "")
    return {
        "type": "document",
        "source": {"type": "content", "content": [{"type": "text", "text": content}]},
        "title": label,
        "citations": {"enabled": True},
    }


def _footnote(citation) -> tuple[tuple, str] | None:
    """Build a dedup key and the rendered footnote for one citation."""
    cited = (getattr(citation, "cited_text", "") or "").strip().replace("\n", " ")
    url = getattr(citation, "url", None)

    if url:
        # Web result: the URL is the identity — the lawyer needs to open it.
        title = (getattr(citation, "title", None) or url).strip()
        return ("web", url), f"{title}: {url}"

    title = getattr(citation, "document_title", None) or "documento"
    if not cited:
        return None
    snippet = f'"{cited[:_SNIPPET_MAX]}…"' if len(cited) > _SNIPPET_MAX else f'"{cited}"'
    return ("doc", title, cited[:120]), f"{title}: {snippet}"


def render(content_blocks) -> str:
    """Assemble the answer with `[n]` markers and a numbered sources section."""
    parts: list[str] = []
    footnotes: list[str] = []
    numbers: dict[tuple, int] = {}

    for block in content_blocks:
        if getattr(block, "type", None) != "text":
            continue  # thinking, server_tool_use and tool results are not shown
        parts.append(block.text)
        for citation in getattr(block, "citations", None) or []:
            entry = _footnote(citation)
            if entry is None:
                continue
            key, rendered = entry
            if key not in numbers:
                numbers[key] = len(footnotes) + 1
                footnotes.append(f"[{numbers[key]}] {rendered}")
            parts.append(f" [{numbers[key]}]")

    answer = "".join(parts).strip()
    if footnotes:
        answer += "\n\nFontes:\n" + "\n".join(footnotes)
    return answer


def searched_queries(content_blocks) -> list[str]:
    """The queries the model actually ran, for the audit log."""
    queries = []
    for block in content_blocks:
        if getattr(block, "type", None) == "server_tool_use":
            query = (getattr(block, "input", None) or {}).get("query")
            if query:
                queries.append(str(query))
    return queries


def cited_urls(content_blocks) -> set[str]:
    """URLs the model cited inline, so they aren't listed twice."""
    urls = set()
    for block in content_blocks:
        if getattr(block, "type", None) != "text":
            continue
        for citation in getattr(block, "citations", None) or []:
            url = getattr(citation, "url", None)
            if url:
                urls.add(url)
    return urls


def web_sources(content_blocks, limit: int = 6) -> list[str]:
    """Pages the web-search tool returned, as `Title — URL` lines.

    The dynamic-filtering search tool digests results in a sandbox and answers
    in prose, without emitting per-claim citation objects the way documents and
    `web_fetch` do. Harvesting the result blocks is therefore the only way to
    show a lawyer where the answer came from — and it is the honest label:
    these are pages consulted, not passages cited.
    """
    already_cited = cited_urls(content_blocks)
    seen: set[str] = set()
    sources: list[str] = []

    for block in content_blocks:
        if getattr(block, "type", None) != "web_search_tool_result":
            continue
        inner = getattr(block, "content", None)
        if not isinstance(inner, list):
            continue  # an error object rather than results
        for result in inner:
            url = getattr(result, "url", None)
            if not url or url in seen or url in already_cited:
                continue
            seen.add(url)
            title = (getattr(result, "title", None) or url).strip()
            sources.append(f"{title}: {url}")
            if len(sources) >= limit:
                return sources
    return sources
