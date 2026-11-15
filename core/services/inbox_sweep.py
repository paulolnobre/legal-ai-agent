"""Read the bucket on a schedule, so nobody has to remember to.

`/importar` was enough while somebody uploaded a file and immediately asked
about it. It stopped being enough when Drive started synchronising into the
bucket: documents now arrive continuously, from people who are not in the chat
and have no reason to know a command exists. A document the firm believes it
delivered, sitting unread because nobody typed anything, is the failure this
removes.

Three decisions shape it, and each one is about not being a nuisance.

**It sweeps only during office hours.** Nothing in a law firm needs a contract
indexed at three in the morning, and a sweep at that hour spends money on
embeddings to produce a notification nobody wants. Files that
arrive overnight are read at the first sweep of the morning, and anybody working
late still has `/importar`, which is unchanged and immediate.

**It reports only when something happened.** A message every half hour saying
"nothing new" trains people to ignore the channel, and the one run that mattered
arrives into that habit.

**It never notifies about a skip.** A skip is decided from the listing, costs
nothing, and repeats every single sweep for as long as the file sits in the
bucket: a holiday photo somebody dropped in would announce itself forever. Those
stay in `/importar`, where a person asked and is present to act on the answer.
"""
import asyncio
import logging

from core.config import settings
from core.integrations import inbox_bucket, telegram
from core.repositories.connection import get_pool
from core.repositories.staff import MANAGEMENT_ROLES, list_staff
from core.schedule import is_office_hours
from core.services.inbox_import import import_new_documents

logger = logging.getLogger(__name__)

# Only one sweep at a time, and never one alongside a manual /importar in the
# same process. Both would list the same files and download them twice before
# either wrote to the ledger; ingestion would deduplicate the second copy, so
# the cost is bandwidth and embeddings rather than a wrong archive, which is
# still a cost worth not paying.
_running = asyncio.Lock()


def _summary(report) -> str | None:
    """What to tell the firm, or None when there is nothing worth saying.

    Skips are deliberately absent. See the module docstring: they repeat every
    sweep and would turn the notification into noise nobody reads.
    """
    if not report.ingested and not report.failed:
        return None

    lines = ["📥 Novos documentos no acervo, vindos do balde:"]
    for name, matter, doc_type in report.ingested:
        parts = [part for part in (doc_type, matter) if part]
        suffix = f" ({', '.join(parts)})" if parts else " (sem classificação)"
        lines.append(f"  • {name}{suffix}")
    if report.failed:
        lines.append(f"\n⚠️ {len(report.failed)} não pôde(puderam) ser lido(s):")
        lines += [f"  • {name}: {reason}" for name, reason in report.failed]
    lines.append("\nUse /docs para ver o acervo, ou /editar para corrigir.")
    return "\n".join(lines)


async def sweep_once() -> int:
    """One pass over the bucket. Returns how many documents were ingested.

    The scheduled run has no uploader, and `uploaded_by` is left empty rather
    than filled with the owner's id. The owner did not upload it, and a record
    saying they did is a small lie in an audit trail that exists to be believed.
    """
    if _running.locked():
        logger.info("Bucket sweep already running; skipping this tick")
        return 0

    async with _running:
        pool = await get_pool()
        async with pool.connection() as conn:
            report = await import_new_documents(conn, staff_id=None)
            message = _summary(report)
            managers = (
                await list_staff(conn, roles=MANAGEMENT_ROLES) if message else []
            )

    if not message:
        return len(report.ingested)

    for manager in managers:
        if not manager.chat_id:
            continue
        try:
            await telegram.send_message(manager.chat_id, message)
        except Exception:
            # One unreachable chat must not stop the others from being told,
            # and the documents are already in the archive either way.
            logger.exception("Could not tell %s about the import", manager.id)
    return len(report.ingested)


async def run_inbox_sweep_worker(stop_event: asyncio.Event) -> None:
    """Sweep the bucket every configured interval, during office hours only."""
    if not (settings.inbox_enabled and settings.inbox_sweep_enabled):
        logger.info("Scheduled bucket sweep is off")
        return

    interval = max(5, settings.inbox_sweep_minutes) * 60
    while not stop_event.is_set():
        if is_office_hours():
            try:
                ingested = await sweep_once()
                if ingested:
                    logger.info("Bucket sweep ingested %d document(s)", ingested)
            except asyncio.CancelledError:
                raise
            except inbox_bucket.InboxError as exc:
                # The bucket being unreachable is a condition, not a bug: it is
                # logged at warning and retried on the next tick rather than
                # filling the log with a stack trace every half hour.
                logger.warning("Bucket sweep could not read the bucket: %s", exc)
            except Exception:
                # A failed sweep must not take the assistant down with it.
                logger.exception("Bucket sweep failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            continue
        break


__all__ = ["run_inbox_sweep_worker", "sweep_once"]
