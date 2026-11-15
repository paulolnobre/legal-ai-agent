"""Whether work that arrives now is done now or waits for the morning.

One rule for both ways in: the Telegram webhook and the panel. Out of hours,
work waits, except for two things: an owner keeping the system alive at
night, and answering a button the bot already put on someone's screen.
"""
from __future__ import annotations

import asyncio
import logging

from core.repositories.connection import get_pool
from core.repositories.staff import has_unrestricted_access
from core.schedule import is_office_hours

logger = logging.getLogger(__name__)

# The webhook must answer Telegram quickly, and this lookup is the only thing
# that could stall it: past this, deferring until the morning is the right
# answer anyway, and the update is already safe in the queue.
_ACCESS_CHECK_TIMEOUT = 2.0


async def _may_work_out_of_hours(chat_id) -> bool:
    """Whether this chat is served at any hour. False on any doubt."""
    try:
        async def _lookup() -> bool:
            pool = await get_pool()
            async with pool.connection() as conn:
                return await has_unrestricted_access(conn, chat_id)

        return await asyncio.wait_for(_lookup(), timeout=_ACCESS_CHECK_TIMEOUT)
    except TimeoutError:
        logger.warning("Unrestricted-access check timed out; deferring the update")
    except Exception:
        # A database hiccup must not drop the update: deferring is safe, because
        # the queue holds it until the morning either way.
        logger.exception("Could not check unrestricted access; deferring")
    return False


async def may_work_now(chat_id, *, button_press: bool) -> bool:
    """True when this work runs now, False when it waits for the morning.

    A button press is the second half of an exchange the bot itself started:
    the person is looking at the screen now, and a token deferred until the
    morning would have expired by then.
    """
    if button_press or is_office_hours():
        return True
    if chat_id is None:
        return False
    return await _may_work_out_of_hours(chat_id)


__all__ = ["may_work_now"]
