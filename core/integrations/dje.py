"""The publications of an identified process, in its court's Diário da Justiça.

DataJud says that a process exists and what moved in it. It does not carry what
was published: intimations, the operative part of decisions, the dates a
deadline starts from. Those are in the court's Diário da Justiça Eletrônico. For
the TJPA, its signed editions are indexed by Google and by Exa, measured on
2026-09-11 with queries that carried no process number.

This is the one place a process number is sent to a search engine, and it was
asked for. Everywhere else the number goes only to DataJud, because it
identifies a client's case and telling a search provider about it tells them
what the firm is working on. The exception is kept as narrow as the request
allows:

- only a number DataJud has just confirmed, never one read from the question
  and not found, so a mistyped number is not sent anywhere;
- only the number itself, in quotes, with no other word of the question;
- only inside the diary of the court DataJud resolved it to;
- only while DJE_SEARCH_ENABLED is on.

Serper is asked first, and Exa only when Serper fails or finds nothing. An
edition of the diary runs to hundreds of pages, past what the page reader opens,
so what comes back is the search engine's excerpt, and `mentions` says whether
that excerpt actually shows the number.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core.config import settings
from core.integrations import datajud, exa, serper
from core.integrations.serper import SearchResult

logger = logging.getLogger(__name__)

# The court DataJud resolves a CNJ number to, and that court's diary. Only a
# diary measured to be indexed belongs here: an entry for one that is not would
# turn every lookup into a search that can only come back empty.
DJE_SITES = {"tjpa": "dje.tjpa.jus.br"}


@dataclass(frozen=True)
class DiarySearch:
    """What the diary search found for one process, and which provider found it."""

    number: str
    site: str
    results: list[SearchResult] = field(default_factory=list)
    provider: str | None = None
    problems: list[str] = field(default_factory=list)


def site_for(process_number: str) -> str | None:
    """The diary to search for this process, or None when there is none here."""
    try:
        return DJE_SITES.get(datajud.court_alias(process_number))
    except ValueError:
        return None


def mentions(process_number: str, text: str) -> bool:
    """Whether an excerpt shows this process number, punctuated or not.

    A search engine can return an edition for a quoted number and still show an
    excerpt from somewhere else in it. Only an excerpt that shows the number is
    evidence that the edition is about this process.
    """
    wanted = datajud.normalize_process_number(process_number)
    return wanted in datajud.extract_process_numbers(text or "", limit=50)


async def search_publications(process_number: str) -> DiarySearch | None:
    """Look for this process in its court's diary: Serper first, then Exa.

    Returns None when the court has no diary configured, so the caller can tell
    "not searched" from "searched and found nothing".
    """
    site = site_for(process_number)
    if site is None:
        return None
    number = datajud.format_process_number(process_number)
    query = f'"{number}"'
    problems: list[str] = []

    try:
        found = await serper.search_site(query, site)
    except serper.SerperError as exc:
        problems.append(f"Serper falhou ({exc})")
    else:
        if found:
            return DiarySearch(number, site, found, "serper")
        problems.append("Serper não encontrou publicações")

    if not (settings.exa_api_key or "").strip():
        problems.append("o Exa não está configurado para tentar em seguida")
        return DiarySearch(number, site, [], None, problems)

    try:
        found = await exa.search_site(query, site)
    except exa.ExaError as exc:
        problems.append(f"Exa falhou ({exc})")
    else:
        if found:
            return DiarySearch(number, site, found, "exa", problems)
        problems.append("Exa também não encontrou publicações")
    return DiarySearch(number, site, [], None, problems)


__all__ = ["DJE_SITES", "DiarySearch", "mentions", "search_publications", "site_for"]
