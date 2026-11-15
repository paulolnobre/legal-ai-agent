"""Where a notice for the installed app goes: one row per subscribed device.

A subscription is the push service's address for one device (`endpoint`)
and the two keys the payload is encrypted to (`p256dh`, `auth`). It is made
only from an unlocked app session and takes that session's passkey, so
removing the passkey (or `/web encerrar`) stops the device's notices through
the foreign key.

**The endpoint is a place the server will POST to.** The browser chooses it,
so without a list it is a request the server makes wherever a page says.
`allowed_endpoint` keeps it to the known push services, matched on the parsed
host, never on the text: `push.apple.com.example` and `xpush.apple.com` both
contain the right letters.

Stored in the clear: sending to a subscription also needs the VAPID private
key, which lives in `.env`, not in the database.
"""
from __future__ import annotations

import re
from typing import NamedTuple
from urllib.parse import urlsplit

# A person rarely has more than a phone and a tablet. When an app is removed
# and installed again its old subscription stays until a send meets a 410;
# past this many the oldest goes, so the list a notice is sent to stays short.
MAX_PER_PERSON = 10
MAX_ENDPOINT = 1024

# Every Apple push host (WebKit asks servers to accept any subdomain), and the
# two other browsers' services, for checks outside Safari. Exact names, and
# one suffix that starts with its dot.
_HOSTS = frozenset({"fcm.googleapis.com", "updates.push.services.mozilla.com"})
_APPLE_SUFFIX = ".push.apple.com"
# What a host name may hold once parsed. Anything else (a backslash, a percent
# sign, a space) is a URL that another parser may read differently.
_HOST_CHARACTERS = re.compile(r"[a-z0-9.-]+")


class Subscription(NamedTuple):
    id: int
    endpoint: str
    p256dh: bytes
    auth: bytes


def allowed_endpoint(url: str) -> bool:
    """Whether `url` is a push service address the server may POST to: https,
    the default port, no user information, a known host, at most 1024
    characters."""
    if not isinstance(url, str) or not url or len(url) > MAX_ENDPOINT:
        return False
    # Printable ASCII only, no space and no backslash: the sending library
    # parses the URL again, and the two must not disagree on the host.
    if not (url.isascii() and url.isprintable()) or " " in url or "\\" in url:
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host = parts.hostname or ""
    if parts.scheme != "https" or port is not None:
        return False
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return False
    if not _HOST_CHARACTERS.fullmatch(host):
        return False
    if host in _HOSTS:
        return True
    return host.endswith(_APPLE_SUFFIX) and len(host) > len(_APPLE_SUFFIX) \
        and not host.startswith(".")


async def subscribe(conn, staff_id: int, passkey_id: int, *, endpoint: str,
                    p256dh: bytes, auth: bytes, commit: bool = True) -> None:
    """Store this device's subscription for this person and passkey.

    An endpoint already stored is re-bound, not duplicated: a device that
    subscribes again, or is now signed in as somebody else, gets its notices
    for whoever subscribed last. Handed again unchanged, it is left alone. The person keeps their newest
    `MAX_PER_PERSON`.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO push_subscriptions (staff_id, passkey_id, endpoint, p256dh, auth) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (endpoint) DO UPDATE SET staff_id = EXCLUDED.staff_id, "
            "passkey_id = EXCLUDED.passkey_id, p256dh = EXCLUDED.p256dh, "
            "auth = EXCLUDED.auth, created_at = NOW(), last_ok_at = NULL "
            # The app hands its subscription again on every open: unchanged,
            # it is left as it is, its dates included.
            "WHERE (push_subscriptions.staff_id, push_subscriptions.passkey_id, "
            "push_subscriptions.p256dh, push_subscriptions.auth) IS DISTINCT FROM "
            "(EXCLUDED.staff_id, EXCLUDED.passkey_id, EXCLUDED.p256dh, EXCLUDED.auth)",
            (staff_id, passkey_id, endpoint, p256dh, auth),
        )
        await cur.execute(
            "DELETE FROM push_subscriptions WHERE staff_id = %s AND id NOT IN ("
            "SELECT id FROM push_subscriptions WHERE staff_id = %s "
            "ORDER BY created_at DESC, id DESC LIMIT %s)",
            (staff_id, staff_id, MAX_PER_PERSON),
        )
    if commit:
        await conn.commit()


async def unsubscribe(conn, staff_id: int, endpoint: str, commit: bool = True) -> bool:
    """Remove this person's subscription at `endpoint`; another person's
    stays. Says whether one was removed."""
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM push_subscriptions WHERE staff_id = %s AND endpoint = %s",
            (staff_id, endpoint),
        )
        removed = bool(cur.rowcount)
    if commit:
        await conn.commit()
    return removed


async def of(conn, staff_id: int) -> list[Subscription]:
    """Where this person's notices go. One indexed query."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, endpoint, p256dh, auth FROM push_subscriptions "
            "WHERE staff_id = %s ORDER BY id",
            (staff_id,),
        )
        rows = await cur.fetchall()
    return [Subscription(row[0], row[1], bytes(row[2]), bytes(row[3])) for row in rows]


async def drop(conn, subscription_id: int, commit: bool = True) -> None:
    """A subscription the push service says is gone (404, 410)."""
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM push_subscriptions WHERE id = %s", (subscription_id,))
    if commit:
        await conn.commit()


async def mark_ok(conn, subscription_id: int, commit: bool = True) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE push_subscriptions SET last_ok_at = NOW() WHERE id = %s",
            (subscription_id,),
        )
    if commit:
        await conn.commit()


__all__ = ["MAX_ENDPOINT", "MAX_PER_PERSON", "Subscription", "allowed_endpoint", "drop",
           "mark_ok", "of", "subscribe", "unsubscribe"]
