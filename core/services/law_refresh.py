"""Keep the laws in the corpus the laws in force.

A law ingested today is right today. The Planalto amends its consolidated pages
as laws change, and nothing brought those changes into the corpus: an article
revoked next year would go on being cited from the archive, with the firm's own
document as its source. #77 stopped revoked wording reaching the model from a
page; this stops the archive itself from going stale.

A document is linked to its official page with /atualizar. From then on it is
fetched again every `law_refresh_days`, and when the extracted text differs it
is re-indexed in place, on the same id, the way `reprocess_document` does it,
because the audit trail and every past answer point at that id.

Three rules keep it from doing harm.

**A large change is held, not applied.** A law that grows or shrinks by a fifth
between two checks is far more often a wrong URL, a maintenance page or a
truncated download than an amendment. It is reported, and a person decides with
`/atualizar <id> forçar`.

**Nothing changes on a failed read.** An unreachable page leaves the document as
it was and is tried again the next day. Only a page that has not been read for
twice the interval is reported, so an afternoon outage at the Planalto costs no
message and a page that moved is not forgotten.

**It speaks only when something happened**, like the bucket sweep, and only to
lawyers and above: an unchanged law is not news.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from core.config import settings
from core.integrations import telegram, web_fetch
from core.repositories.connection import get_pool
from core.repositories.documents import (
    get_official_source,
    list_due_for_refresh,
    mark_source_checked,
)
from core.repositories.staff import ROLE_ADMIN, ROLE_LAWYER, ROLE_OWNER, list_staff
from core.schedule import is_office_hours, local_now
from core.services.ingestion import Refreshed, refresh_from_original

logger = logging.getLogger(__name__)

# The people a law changing is news for: whoever answers questions with it.
_TOLD_ROLES = (ROLE_LAWYER, ROLE_ADMIN, ROLE_OWNER)

_CHECK_INTERVAL = 3600  # seconds between clock checks; the pass runs once a day

# One refresh at a time in this process: the scheduled pass and a lawyer's
# /atualizar on the same document would otherwise both embed it and both write.
_running = asyncio.Lock()


async def refresh_document(conn, document_id: int, *, url: str | None = None,
                           force: bool = False, staff_id=None) -> Refreshed:
    """Compare one document with its official page, and update it if it changed.

    With `url`, the document is linked to that page, but only once the page has
    been read and judged: a URL whose text is nothing like the document's is
    not kept, so a wrong link cannot go on being checked every week.

    Raises LookupError for a document that does not exist or has no page, and
    web_fetch.FetchError or ValueError when the page cannot be read or yields
    no usable text. In every one of those cases nothing was written.
    """
    async with _running:
        return await _refresh(conn, document_id, url=url, force=force, staff_id=staff_id)


async def _refresh(conn, document_id: int, *, url: str | None, force: bool,
                   staff_id) -> Refreshed:
    official = await get_official_source(conn, document_id)
    if official is None:
        raise LookupError(f"Document {document_id} does not exist.")
    source_url = url or official.source_url
    if not source_url:
        raise LookupError(f"Document {document_id} is not linked to an official page.")

    fetched = await web_fetch.fetch_document(source_url)
    result = await refresh_from_original(
        conn, document_id, fetched.data, web_fetch.filename_for(fetched.url, fetched.content_type),
        source_url=source_url, max_change=settings.law_refresh_max_change,
        force=force, staff_id=staff_id,
    )
    if result.outcome == "unchanged":
        # A new link is kept once the page proved to be this document's.
        await mark_source_checked(conn, document_id, source_url=url)
    elif result.outcome == "needs_review" and url is None:
        # A held link is checked, so the daily pass does not repeat the same
        # report every day; it says it again next week if nobody acts. A new
        # URL that was held is not kept at all: it was never shown to be right.
        await mark_source_checked(conn, document_id)
    return result


def _br_int(value: int) -> str:
    return f"{value:,}".replace(",", ".")


def _percent(change: float) -> str:
    direction = "maior" if change > 0 else "menor"
    return f"{abs(change) * 100:.0f}% {direction}"


@dataclass(frozen=True)
class PassReport:
    refreshed: list[Refreshed]
    unreadable: list[tuple[int, str, str]]  # id, title, reason


def summary(report: PassReport) -> str | None:
    """What to tell the firm, or None when nothing is worth saying."""
    updated = [r for r in report.refreshed if r.outcome == "updated"]
    held = [r for r in report.refreshed if r.outcome == "needs_review"]
    if not (updated or held or report.unreadable):
        return None

    lines: list[str] = []
    if updated:
        lines.append("📜 Legislação atualizada a partir da página oficial:")
        lines += [
            f"  • [{r.document_id}] {r.title}: {_br_int(r.chars_before)} → "
            f"{_br_int(r.chars_after)} caracteres, {r.chunks_after} trechos"
            for r in updated
        ]
        lines.append("Respostas anteriores podem citar a versão antiga.")
    if held:
        if lines:
            lines.append("")
        lines.append(
            "⚠️ A página oficial mudou demais para eu atualizar sozinho. "
            "Costuma ser página fora do ar ou URL errada, não mudança da lei:"
        )
        lines += [
            f"  • [{r.document_id}] {r.title}: texto {_percent(r.change)}"
            for r in held
        ]
        lines.append("Se a página estiver certa: /atualizar <id> forçar")
    if report.unreadable:
        if lines:
            lines.append("")
        lines.append(
            f"⚠️ Não consigo ler a página oficial há mais de "
            f"{settings.law_refresh_days * 2} dias:"
        )
        lines += [
            f"  • [{doc_id}] {title}: {reason}"
            for doc_id, title, reason in report.unreadable
        ]
        lines.append("Confira se a página mudou de endereço e use /atualizar <id> <url>.")
    return "\n".join(lines)


async def refresh_due_documents() -> PassReport:
    """One pass over every linked document whose check is due."""
    refreshed: list[Refreshed] = []
    unreadable: list[tuple[int, str, str]] = []
    # Reported only once a page has gone unread for two intervals. Everything
    # before that is an outage the next day's pass will most likely get past.
    overdue = timedelta(days=settings.law_refresh_days * 2)
    pool = await get_pool()
    async with pool.connection() as conn:
        due = await list_due_for_refresh(conn, older_than_days=settings.law_refresh_days)
        for source in due:
            try:
                refreshed.append(await refresh_document(conn, source.id))
            except asyncio.CancelledError:
                raise
            except (web_fetch.FetchError, ValueError) as exc:
                logger.warning("Could not refresh document %s: %s", source.id, exc)
                last = source.checked_at
                if last is None or datetime.now(timezone.utc) - last > overdue:
                    unreadable.append((source.id, source.title, str(exc)))
            except Exception:
                # One document failing must not stop the others being checked.
                logger.exception("Refreshing document %s failed", source.id)
    return PassReport(refreshed, unreadable)


async def _tell(message: str) -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        people = await list_staff(conn, roles=_TOLD_ROLES)
    for person in people:
        if not person.chat_id:
            continue
        try:
            await telegram.send_message(person.chat_id, message)
        except Exception:
            # One unreachable chat must not stop the others from being told,
            # and the corpus is already updated either way.
            logger.exception("Could not tell %s about the law refresh", person.id)


async def run_law_refresh_worker(stop_event: asyncio.Event) -> None:
    """Once a day, in office hours, compare the linked laws that are due."""
    if not settings.law_refresh_enabled:
        logger.info("Law refresh is off")
        return

    last_run_date = None
    while not stop_event.is_set():
        today = local_now().date()
        # Office hours, so that a change is announced to people who are there
        # to read it, and a Planalto outage at night never becomes a message.
        if is_office_hours() and today != last_run_date:
            last_run_date = today
            try:
                message = summary(await refresh_due_documents())
                if message:
                    await _tell(message)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed pass must not take the assistant down: tomorrow's
                # pass picks up whatever is still due.
                logger.exception("Law refresh pass failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_CHECK_INTERVAL)
        except TimeoutError:
            continue
        break


__all__ = [
    "PassReport",
    "Refreshed",
    "refresh_document",
    "refresh_due_documents",
    "run_law_refresh_worker",
    "summary",
]
