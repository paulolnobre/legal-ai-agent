"""One place that decides which service finds candidate pages.

Both providers answer the same question and return the same shape, so this is
a choice of supplier, not of behaviour. Everything downstream (the allowlist,
the URL check, reading the page to confirm it, DataJud as the authority for a
process number) is identical either way. What differs is the evidence: Serper
returns a search-index snippet and Exa an excerpt taken from the page. So a
caller that tells the reader which provider found a page resolves the provider
once and passes it to both the search and the formatting, and the name it shows
cannot belong to a supplier that did not run.

The switch is configuration rather than architecture, so the two can be
compared on the same question. Swapping on an impression is exactly what the
roadmap's section 10 forbids: a change here has to be justified by legal
correctness, currency, authority, citation fidelity, unsupported-claim rate,
latency and cost, measured.
"""
from core.config import settings
from core.integrations import exa, serper
from core.integrations.serper import SearchResult


def active_provider() -> str:
    """The provider a search would use right now, "serper" or "exa".

    The one place the setting is read, so every caller agrees on the fallback.
    """
    return "exa" if settings.web_search_provider == "exa" else "serper"


async def search(query: str, provider: str | None = None) -> list[SearchResult]:
    """Search with `provider`, or with the configured one when none is given."""
    if (provider or active_provider()) == "exa":
        return await exa.search(query)
    return await serper.search(query)


async def canary(provider: str | None = None) -> None:
    """Probe only the selected provider with its fixed, non-client query."""
    if (provider or active_provider()) == "exa":
        await exa.canary()
        return
    await serper.canary()


def format_result(result: SearchResult, provider: str | None = None) -> str:
    """Describe a result the way its provider's evidence deserves.

    Serper returns an index snippet and Exa returns text taken from the page.
    Both are less than a full reading, but they are not the same thing, and the
    model is told which it is looking at rather than being left to assume.
    """
    if (provider or active_provider()) == "exa":
        return exa.format_result(result)
    return serper.format_result(result)


async def close() -> None:
    await serper.close()
    await exa.close()


__all__ = [
    "SearchResult",
    "active_provider",
    "canary",
    "close",
    "format_result",
    "search",
]
