"""Durable Telegram update worker and FastAPI server entry point."""
import asyncio
import logging
import time

from core.config import settings
from core.handlers import legal_message
from core.handlers.legal_message import process_legal_message
from core.integrations import update_queue
from core.metrics import UPDATE_SECONDS
from core.observability import bind_context, new_correlation_id
from core.repositories import panel
from core.repositories.connection import get_pool
from core.schedule import is_office_hours
from core.services import media

logger = logging.getLogger(__name__)

_UPDATE_TIMEOUT = 300


async def _handle_update(update: dict) -> None:
    """Hand one update to the handler, whatever shape it arrived in.

    Nothing is decided here about what an update means. This function used to
    look for a chat at the top level and return when it found none, which
    silently discarded every button press: a press carries its chat one level
    deeper, inside the message the button sits on. The worker then acknowledged
    the payload as done, so a press left no queue entry, no log line and no
    error, and the button simply stopped spinning.

    A second reading of an update's shape is a copy of a decision made
    elsewhere, and a copy drifts. The handler is the one place that reads it.

    The limit is one deadline the handler may move, and only a panel
    request reading an attachment does: its media stage gets its own time,
    then the answer gets these 300 s from where the stage ended.
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        async with asyncio.timeout(_UPDATE_TIMEOUT) as limit:
            token = legal_message.update_limit.set(
                legal_message.UpdateLimit(limit, _UPDATE_TIMEOUT))
            try:
                await process_legal_message(conn, update)
            finally:
                legal_message.update_limit.reset(token)


async def _fail_abandoned_panel_requests() -> None:
    """A panel request still marked running when a worker starts was left by
    one that died. It is never replayed: recover_processing puts its payload
    back, and the worker then finds it no longer startable."""
    pool = await get_pool()
    async with pool.connection() as conn:
        failed = await panel.fail_running_at_startup(conn)
    if failed:
        logger.warning("Marked %s abandoned panel requests as failed", failed)


async def _run_worker_session(stop_event: asyncio.Event) -> None:
    # Decrypted audio a killed worker left behind goes before any request is
    # taken (it cannot be anyone's in flight: this process serves one at a time).
    await asyncio.to_thread(media.sweep_media_dir)
    await update_queue.recover_processing()
    await _fail_abandoned_panel_requests()
    logger.info("Telegram queue worker ready")

    while not stop_event.is_set():
        if is_office_hours():
            # Deferred messages are drained before messages that arrived today.
            payload = await update_queue.reserve_update(deferred=True)
            if payload is None:
                payload = await update_queue.reserve_update(deferred=False)
        else:
            # Outside office hours the deferred queue stays untouched until
            # morning, but the active queue keeps being drained: the webhook
            # only puts a message there at this hour when its sender holds
            # unrestricted access, so the owner can work on the system at night.
            payload = await update_queue.reserve_update(deferred=False)

        if payload is None:
            continue

        await _process_reserved_payload(payload)


async def _process_reserved_payload(payload: str) -> None:
    """Process one reserved queue item with a stable correlation context."""
    started = time.perf_counter()
    result = "failed"
    update: dict | None = None
    update_id: int | str | None = None
    correlation = new_correlation_id("queue")
    try:
        update = update_queue.decode_update(payload)
        update_id = update.get("update_id")
        if update_id is not None:
            correlation = f"telegram-{update_id}"
    except Exception:
        with bind_context(correlation=correlation):
            retrying = await update_queue.retry_update(payload, None)
            result = "retry" if retrying else "dead_letter"
            logger.exception(
                "Telegram update could not be decoded (retrying=%s)",
                retrying,
            )
            await asyncio.sleep(2)
    else:
        with bind_context(correlation=correlation, update=update_id):
            try:
                await _handle_update(update)
            except asyncio.CancelledError:
                result = "cancelled"
                raise
            except Exception:
                retrying = await update_queue.retry_update(payload, update_id)
                result = "retry" if retrying else "dead_letter"
                logger.exception(
                    "Telegram update failed (id=%s, retrying=%s)",
                    update_id,
                    retrying,
                )
                await asyncio.sleep(2)
            else:
                await update_queue.acknowledge_update(payload)
                result = "success"
    finally:
        UPDATE_SECONDS.labels(result).observe(time.perf_counter() - started)


async def run_update_worker(stop_event: asyncio.Event) -> None:
    """Consume updates and reconnect if Redis or PostgreSQL becomes unavailable."""
    while not stop_event.is_set():
        try:
            await _run_worker_session(stop_event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Queue worker session failed; reconnecting")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=10)
            except TimeoutError:
                continue
            break


def run() -> None:
    """Development-compatible entry point used by run_legal.py."""
    import uvicorn

    uvicorn.run(
        "core.web.app:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.app_env == "development",
    )
