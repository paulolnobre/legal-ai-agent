"""Tell a lawyer about a new publication without being asked.

`/processo` answers when somebody remembers to ask. An intimation is the one
thing a law firm cannot afford to learn about late, because a deadline runs from
it whether or not anybody looked. So this asks the DJEN on the firm's behalf,
every `publication_watch_minutes` during office hours, for each OAB and each
process somebody follows, and sends each person what is new for them.

How "new" is decided, since it is the whole job:

- a publication is new for a person until they have been told about it, kept
  per person in `publication_notices`, so the same intimation reaching two
  people by two routes reaches each of them once;
- nothing made available before the person started following is sent, so
  following an OAB does not unload last week's intimations into the chat;
- the notice is recorded only after the message went out. A crash in between
  sends it twice, which is the right failure: a duplicate costs a glance, a
  missed intimation can cost a deadline.

The DJEN refusing this server (it refuses non-Brazilian addresses) ends the
pass for everyone: the next query would be refused for the same reason.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from core.config import settings
from core.integrations import djen, telegram
from core.services import push
from core.repositories.connection import get_pool
from core.repositories.publications import (
    Watch,
    list_active_watches,
    noticed,
    purge_old_notices,
    record_notices,
)
from core.schedule import is_office_hours, local_now

logger = logging.getLogger(__name__)

# Telegram refuses a message over 4096 characters. Each notice is cut to fit,
# and a person with many new publications gets several messages.
_MESSAGE_LIMIT = 3800

# Longer than any window a query can return, so a publication is never
# forgotten while it could still be fetched again and look new.
_NOTICE_RETENTION_DAYS = 120

_running = asyncio.Lock()


@dataclass
class _Found:
    communication: djen.Communication
    # Why each person gets it: "OAB 12345/PA" or "processo acompanhado".
    reasons: dict[int, str] = field(default_factory=dict)
    chats: dict[int, str] = field(default_factory=dict)


def _since(watch: Watch) -> date:
    return local_now(watch.since).date()


def _entry(c: djen.Communication, reason: str) -> str:
    when = f"{c.made_available:%d/%m/%Y}" if c.made_available else "data não informada"
    what = ", ".join(part for part in (c.kind, c.document_type) if part) or "Publicação"
    lines = [f"• {when}, {c.court or 'tribunal não informado'}: {what}",
             f"  Processo {c.process_number}"]
    if c.body:
        lines.append(f"  {c.body}")
    text = djen.excerpt(c)
    if text:
        lines.append(f"  {text}")
    if c.link:
        lines.append(f"  {c.link}")
    lines.append(f"  (por: {reason})")
    return "\n".join(lines)


def messages_for(entries: list[str]) -> list[str]:
    """The notices for one person, split so no message passes Telegram's limit."""
    header = "📰 Publicação nova no DJEN:" if len(entries) == 1 else (
        f"📰 {len(entries)} publicações novas no DJEN:"
    )
    footer = f"\n\n{djen.DATE_RULE}"
    messages: list[str] = []
    current = header
    for entry in entries:
        entry = entry[: _MESSAGE_LIMIT - len(header) - len(footer) - 4]
        if len(current) + len(entry) + 2 + len(footer) > _MESSAGE_LIMIT:
            messages.append(current + footer)
            current = header + " (continuação)"
        current += "\n\n" + entry
    messages.append(current + footer)
    return messages


async def _collect(watches: list[Watch], today: date) -> dict[int, _Found]:
    """Every new-enough publication, with who gets it and why."""
    window_start = today - timedelta(days=settings.publication_watch_lookback_days)
    by_key: dict[tuple[str, str], list[Watch]] = {}
    for watch in watches:
        by_key.setdefault((watch.kind, watch.key), []).append(watch)

    found: dict[int, _Found] = {}
    for (kind, key), followers in by_key.items():
        try:
            if kind == "oab":
                number, uf = key.split("/")
                comms = await djen.communications_for_lawyer(
                    number, uf, since=window_start, until=today,
                )
                reason = f"OAB {key}"
            else:
                comms = await djen.communications(key, limit=settings.djen_fetch_items)
                reason = "processo acompanhado"
        except djen.DjenRefused:
            raise
        except djen.DjenError as exc:
            # One query failing leaves the others to be read, and this one is
            # asked again next pass: nothing was recorded for it.
            logger.warning("DJEN watch query failed: %s", exc)
            continue

        for c in comms:
            if c.publication_id is None or c.made_available is None:
                continue
            if c.made_available < window_start:
                continue
            for watch in followers:
                if c.made_available < _since(watch):
                    continue
                entry = found.setdefault(c.publication_id, _Found(c))
                entry.reasons.setdefault(watch.staff_id, reason)
                entry.chats[watch.staff_id] = watch.chat_id
    return found


async def check_once() -> int:
    """One pass. Returns how many notices were delivered."""
    if _running.locked():
        return 0
    async with _running:
        pool = await get_pool()
        async with pool.connection() as conn:
            watches = await list_active_watches(conn)
            if not watches:
                return 0
            found = await _collect(watches, local_now().date())
            told = await noticed(conn, list(found))

            per_person: dict[int, list[tuple[int, str]]] = {}
            chats: dict[int, str] = {}
            ordered = sorted(
                found.items(),
                key=lambda item: item[1].communication.made_available or date.min,
                reverse=True,
            )
            for publication_id, entry in ordered:
                for staff_id, reason in entry.reasons.items():
                    if (publication_id, staff_id) in told:
                        continue
                    per_person.setdefault(staff_id, []).append(
                        (publication_id, _entry(entry.communication, reason))
                    )
                    chats[staff_id] = entry.chats[staff_id]

            delivered = 0
            for staff_id, items in per_person.items():
                sent_all = True
                for message in messages_for([text for _, text in items]):
                    if not await telegram.send_message(chats[staff_id], message, parse_mode=None):
                        sent_all = False
                        break
                if sent_all:
                    await record_notices(conn, staff_id, [pid for pid, _ in items])
                    delivered += len(items)
                    # After the messages and their record: a notice that
                    # fails costs nothing here.
                    await push.notify(staff_id, "djen")
                else:
                    # Not recorded, so the next pass tries again.
                    logger.warning("Could not deliver publication notices to %s", staff_id)
            await purge_old_notices(conn, keep_days=_NOTICE_RETENTION_DAYS)
    if delivered:
        logger.info("Delivered %d publication notice(s)", delivered)
    return delivered


async def run_publication_watch_worker(stop_event: asyncio.Event) -> None:
    """Check the DJEN every configured interval, during office hours only."""
    if not (settings.publication_watch_enabled and settings.djen_enabled):
        logger.info("Publication watch is off")
        return

    interval = max(15, settings.publication_watch_minutes) * 60
    while not stop_event.is_set():
        if is_office_hours():
            try:
                await check_once()
            except asyncio.CancelledError:
                raise
            except djen.DjenRefused as exc:
                # A condition of where this server is, not a bug: logged at
                # warning, not as a stack trace every two hours.
                logger.warning("Publication watch refused by the DJEN: %s", exc)
            except Exception:
                # A failed pass must not take the assistant down with it.
                logger.exception("Publication watch pass failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            continue
        break


__all__ = ["check_once", "messages_for", "run_publication_watch_worker"]
