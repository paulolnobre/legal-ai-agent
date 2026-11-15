"""The daily housekeeping pass: retention, and sweeping abandoned steps.

One task, started with the application, that wakes once a day outside office
hours. It enforces the LGPD retention windows on the access log, and drops the
conversation steps nobody came back to answer. It is the administrative routine
the persistence layer's rule reserves for the operator — it is never reachable
from a chat command, and it never touches documents, chunks or stored
originals.

An expired step is already inert, because every read filters on the expiry, so
this sweep is about not keeping a row forever for someone who walked away
mid-question.

The panel chat's attachments are swept on every quarter-hour check, not once a
day: an attachment no message took lives only 15 minutes.
"""
import asyncio
import logging

from core.repositories.conversation import purge_older_than as purge_conversation
from core.repositories.panel import purge_expired_files, purge_old_requests
from core.repositories.panel_uploads import purge_uploads
from core.config import settings
from core.repositories.connection import get_pool
from core.repositories.callback_actions import purge_expired as purge_expired_callbacks
from core.repositories.documents import purge_expired_access_log
from core.repositories.budget_history import record_month
from core.repositories.pending_actions import purge_expired as purge_expired_steps
from core.repositories.pje_credentials import purge_expired_enrolments
from core.repositories.microsoft_accounts import purge_expired_sign_ins
from core.repositories.web_access import purge_expired_sessions
from core.schedule import local_now
from core.services import budget

logger = logging.getLogger(__name__)

_CHECK_INTERVAL = 900  # seconds between clock checks; the pass runs once a day


async def record_budget_history() -> None:
    """Write this month's spend where nothing expires it.

    The meter itself forgets: the Redis counters expire in 40 days and the
    Prometheus gauges reset on every deploy. Both answer "am I near the
    ceiling", neither answers "what did March cost". Written daily rather
    than on the first of the month, so a missed day or a restart costs
    nothing and the row is at most a day behind.
    """
    report = await budget.used()
    month = report.get("month")
    if not month:
        # An unreachable meter is not an outage worth an exception here: the
        # pass has other work, and tomorrow's run writes the same month.
        logger.info("Budget counters unavailable; month not recorded")
        return
    stamp = local_now().strftime("%Y-%m")
    pool = await get_pool()
    async with pool.connection() as conn:
        await record_month(
            conn, stamp,
            input_tokens=month["input"], output_tokens=month["output"],
            cost_ceiling_usd=budget.cost_ceiling(month["input"], month["output"]),
        )


async def run_retention_pass() -> int:
    """Apply the retention windows once. Returns the access-log entries deleted."""
    pool = await get_pool()
    async with pool.connection() as conn:
        deleted = await purge_expired_access_log(conn, settings.access_log_retention_days)
        swept = await purge_expired_steps(conn)
        # Used buttons are kept for a while: answering a repeated press from
        # the stored result only works while the row still exists.
        retired = await purge_expired_callbacks(conn)
        # Enrolment links are not kept at all once spent or expired: unlike a
        # button, there is no repeated press to answer from a stored result,
        # and the row's only remaining value would be to an attacker.
        links = await purge_expired_enrolments(conn)
        # A Microsoft code nobody finished: its device code is a credential.
        sign_ins = await purge_expired_sign_ins(conn)
        # The same holds for web sessions and login links. A dead session is
        # already refused, so this is tidiness and not the lock.
        sessions = await purge_expired_sessions(conn)
        # What people said to the bot, kept as long as their questions are.
        turns = await purge_conversation(conn, settings.query_retention_days)
        # The panel's files are gone at expiry already (the read checks it);
        # this removes the sealed bytes. Its requests go with the turns.
        files = await purge_expired_files(conn)
        await purge_old_requests(conn, settings.query_retention_days)
    if files:
        logger.info("Removed %s expired panel files", files)
    if turns:
        logger.info("Removed %s conversation turns past retention", turns)
    if sessions:
        logger.info("Removed %s expired web sessions", sessions)
    if links:
        logger.info("Removed %s spent or expired PJe enrolment links", links)
    if sign_ins:
        logger.info("Removed %s expired Microsoft sign-ins", sign_ins)
    if swept:
        logger.info("Swept %s abandoned conversation steps", swept)
    if retired:
        logger.info("Removed %s long-expired button actions", retired)
    return deleted


async def sweep_uploads() -> int:
    """Delete the chat's attachments no longer alive. Every clock check, not
    once a day: an unattached one lives only 15 minutes. A failure is logged
    and retried at the next check, a quarter of an hour away."""
    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            purged = await purge_uploads(conn)
    except Exception:
        logger.exception("Panel upload sweep failed")
        return 0
    if purged:
        logger.info("Removed %s panel uploads", purged)
    return purged


async def run_maintenance_worker(stop_event: asyncio.Event) -> None:
    """Run the retention pass once a day, at the configured local hour, and
    sweep the chat's attachments on every check."""
    last_run_date = None
    while not stop_event.is_set():
        await sweep_uploads()
        now = local_now()
        if now.hour == settings.retention_hour and now.date() != last_run_date:
            last_run_date = now.date()
            try:
                await run_retention_pass()
                await record_budget_history()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed pass must not take the assistant down: it is retried
                # tomorrow, and the log says it failed today.
                logger.exception("Retention pass failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_CHECK_INTERVAL)
        except TimeoutError:
            continue
        break
