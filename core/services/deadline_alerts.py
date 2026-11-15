"""Tell a lawyer a deadline is near: 10 days before, 5 days before and on the day.

Every 15 minutes from 08:00 office time, every day (weekends too: a deadline
does not wait for Monday), each open deadline of an open case is checked:

- the threshold is the smallest of 10, 5, 0 at or above the days left, so a
  deadline added with 7 days left is told at once ("em 7 dias") and a missed
  higher threshold is never sent late;
- the people told are the creator of a private case, or the active members
  of a team's, read now: someone who left the team or was deactivated is not;
- `deadline_alerts` is the only memory. There is no "ran today" flag, so a
  deadline due today and added at 10:00 is told at the next wake, and a
  failed send is tried again at the next wake;
- a person's rows are written after their message went out (or, with no
  Telegram, after a device accepted the push). A crash in between tells them
  twice, which costs a glance; never telling them can cost a deadline.

The push says only "Prazo próximo": a lock screen is seen by others.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, time

from core.config import pages_base_url
from core.integrations import datajud, telegram
from core.repositories.connection import get_pool
from core.repositories.processes import (
    alert_candidates,
    purge_alerts,
    record_alerts,
)
from core.schedule import local_now
from core.services import push

logger = logging.getLogger(__name__)

THRESHOLDS = (0, 5, 10)
START = time(8, 0)
INTERVAL_SECONDS = 15 * 60
# Telegram refuses a message over 4096 characters.
_MESSAGE_LIMIT = 3800
# Longer than any deadline stays within the ten-day window.
_RETENTION_DAYS = 120

_running = asyncio.Lock()


@dataclass(frozen=True)
class Due:
    deadline_id: int
    staff_id: int
    chat_id: str | None
    title: str
    due_date: date
    process_number: str
    client: str | None
    threshold: int


def threshold(days_left: int) -> int | None:
    """The alert this many days left calls for, or None outside 0..10."""
    for value in THRESHOLDS:
        if days_left <= value:
            return value if days_left >= 0 else None
    return None


def _line(item: Due, today: date) -> str:
    days = (item.due_date - today).days
    when = f"{item.due_date:%d/%m/%Y}"
    if days == 0:
        head = f"🔴 Prazo vence HOJE ({when})"
    elif days == 1:
        head = f"⚠️ Prazo vence amanhã ({when})"
    else:
        head = f"⚠️ Prazo vence em {days} dias ({when})"
    parts = [f"{head}: {item.title}",
             f"proc. {datajud.format_process_number(item.process_number)}"]
    if item.client:
        parts.append(item.client)
    return " · ".join(parts)


def messages_for(items: list[Due], today: date) -> list[str]:
    """One person's alerts, split so no message passes Telegram's limit."""
    base = pages_base_url()
    footer = f"\n\nVeja em {base}/web/processos/" if base else "\n\nVeja em Processos, no app."
    messages: list[str] = []
    current = ""
    for item in items:
        line = _line(item, today)[: _MESSAGE_LIMIT - len(footer) - 2]
        if current and len(current) + len(line) + 2 + len(footer) > _MESSAGE_LIMIT:
            messages.append(current + footer)
            current = ""
        current = f"{current}\n\n{line}" if current else line
    if current:
        messages.append(current + footer)
    return messages


def _due(candidates, told, today: date) -> list[Due]:
    found = []
    for c in candidates:
        level = threshold((c.due_date - today).days)
        if level is None or (c.deadline_id, c.staff_id, level) in told:
            continue
        found.append(Due(c.deadline_id, c.staff_id, c.chat_id, c.title, c.due_date,
                         c.process_number, c.client, level))
    return found


async def _tell(staff_id: int, chat_id: str | None, items: list[Due], today: date) -> bool:
    """Told means every Telegram message went out; with no Telegram, that a
    device accepted the push. A push never stands in for a failed message,
    which is tried again at the next wake."""
    told = True
    if chat_id:
        for message in messages_for(items, today):
            if not await telegram.send_message(chat_id, message, parse_mode=None):
                told = False
                break
    pushed = await push.notify(staff_id, "deadline")
    return told if chat_id else pushed


async def check_once(today: date | None = None) -> int:
    """One pass. Returns how many alerts were recorded as told."""
    if _running.locked():
        return 0
    today = today or local_now().date()
    recorded = 0
    async with _running:
        pool = await get_pool()
        async with pool.connection() as conn:
            candidates, told = await alert_candidates(conn, today)
            people = list(dict.fromkeys(i.staff_id for i in _due(candidates, told, today)))
            for staff_id in people:
                # Read again now: whoever was deactivated, left the team or
                # lost the case while the others were told is not told.
                mine, told = await alert_candidates(conn, today, staff_id=staff_id)
                items = _due(mine, told, today)
                if not items:
                    continue
                if await _tell(staff_id, items[0].chat_id, items, today):
                    await record_alerts(
                        conn, [(i.deadline_id, staff_id, i.threshold) for i in items])
                    recorded += len(items)
                else:
                    # Not recorded, so the next wake tries again.
                    logger.warning("Could not deliver deadline alerts to %s", staff_id)
            await purge_alerts(conn, keep_days=_RETENTION_DAYS)
    if recorded:
        logger.info("Delivered %d deadline alert(s)", recorded)
    return recorded


async def run_deadline_alerts_worker(stop_event: asyncio.Event, *,
                                     interval: float = INTERVAL_SECONDS) -> None:
    """Wake every `interval`; from 08:00 office time, run a pass."""
    while not stop_event.is_set():
        now = local_now()
        if now.time() >= START:
            try:
                await check_once(now.date())
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed pass must not take the assistant down with it.
                logger.exception("Deadline alerts pass failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            continue
        break


__all__ = ["Due", "check_once", "messages_for", "run_deadline_alerts_worker", "threshold"]
