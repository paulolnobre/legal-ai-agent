"""A notice on the locked phone: something is waiting in the app.

One entry point, `notify(staff_id, kind)`, called after a delivery that has
already happened (the panel reply committed, the Telegram message sent). It
says only which kind of thing is waiting; the phone shows a fixed title for
the kind and opens a fixed address. Nothing about the case, the client or
the person travels in it.

What it guards against:

- **Getting in the way.** It never raises. A push service that is down,
  slow or rude costs a log line, never a reply or a Telegram notice. Each
  push service gets one deadline on the whole exchange: a per-read timeout
  alone lets a server that sends a byte a second hold the sender forever.
  The person's devices are told at once, so the person's notice costs at
  most one deadline, however many devices they have.
- **Being pointed elsewhere.** The endpoint is checked against the known
  push services when a device subscribes. A redirect is never followed, so
  a push service, or anything answering as one, cannot send the server's
  next request somewhere else. A 3xx is a failure.
- **Leaking the endpoint.** It is a capability: whoever holds it can push to
  that phone. Logs name only its host.

404 and 410 mean the device is gone and the subscription is deleted. Any
other failure keeps it for the next notice.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import ssl
import time
from typing import Literal
from urllib.parse import urlsplit

import httpx
from py_vapid import Vapid
from pywebpush import WebPusher

from core.config import push_ready, settings, web_app_on_for
from core.repositories import push_subscriptions
from core.repositories.connection import get_pool
from core.repositories.push_subscriptions import Subscription

logger = logging.getLogger(__name__)

Kind = Literal["answer", "djen", "access", "deadline"]

# Where a tap lands, per kind. All on the assistant for now: the panel has no
# page yet for publications or access requests, whose details stay in
# Telegram. `app=1` marks a way into the installed app (the app entry rule).
URLS: dict[Kind, str] = {
    "answer": "/web/?app=1",
    "djen": "/web/?app=1",
    "access": "/web/?app=1",
    # The deadline alerts land on the page that lists them.
    "deadline": "/web/processos/?app=1",
}

# Seconds for the whole exchange with one push service, connection included.
DEADLINE = 5
# A notice older than a day is not worth showing.
TTL = 86400
# How long the push service may accept this server's signature.
_TOKEN_LIFETIME = 12 * 3600

# The push services' certificates are checked as any other. Tests point this
# at their own local certificate.
_VERIFY: ssl.SSLContext | bool = True


async def notify(staff_id: int, kind: Kind) -> bool:
    """Tell this person's subscribed devices that a `kind` is waiting.

    Returns at once, without a connection, unless push is set up and the
    person is in the app's switch. Never raises: the caller's delivery has
    already happened and a notice must not undo it. Cancellation still
    passes through, so the worker's own deadline still stops it.

    True when at least one device's push service accepted it: the deadline
    alerts record a person with no Telegram only when it arrived.
    """
    try:
        if not push_ready() or not web_app_on_for(staff_id):
            return False
        return await _notify(staff_id, kind)
    except Exception as exc:
        # Nothing here carries an endpoint: those failures are logged per
        # device below, by host.
        logger.warning("Could not send the %s notice: %s", kind, type(exc).__name__)
        return False


async def _notify(staff_id: int, kind: Kind) -> bool:
    pool = await get_pool()
    # A connection of its own, never the caller's transaction, and given back
    # before any push service is asked: a slow one must not hold it.
    async with pool.connection(timeout=DEADLINE) as conn:
        subscriptions = await push_subscriptions.of(conn, staff_id)
    if not subscriptions:
        return False
    payload = json.dumps({"kind": kind, "url": URLS[kind]}).encode("utf-8")
    urgency = "high" if kind == "answer" else "normal"
    statuses = await asyncio.gather(
        *(_attempt(subscription, payload, urgency) for subscription in subscriptions))
    gone = [s.id for s, status in zip(subscriptions, statuses) if status in (404, 410)]
    ok = [s.id for s, status in zip(subscriptions, statuses)
          if status is not None and 200 <= status < 300]
    if not gone and not ok:
        return False
    async with pool.connection(timeout=DEADLINE) as conn:
        for subscription_id in gone:
            await push_subscriptions.drop(conn, subscription_id)
        for subscription_id in ok:
            await push_subscriptions.mark_ok(conn, subscription_id)
    return bool(ok)


async def _attempt(subscription: Subscription, payload: bytes, urgency: str) -> int | None:
    """One device, within the deadline. The status, or None when there was
    no answer. Logs by host only."""
    host = urlsplit(subscription.endpoint).hostname
    try:
        status = await asyncio.wait_for(_send_one(subscription, payload, urgency), DEADLINE)
    except TimeoutError:
        logger.warning("Push service %s did not answer within %ss", host, DEADLINE)
        return None
    except Exception as exc:
        logger.warning("Push to %s failed: %s", host, type(exc).__name__)
        return None
    if status in (404, 410):
        logger.info("Push service %s says a device is gone (%s); dropped", host, status)
    elif not 200 <= status < 300:
        logger.warning("Push service %s answered %s", host, status)
    return status


async def _send_one(subscription: Subscription, payload: bytes, urgency: str) -> int:
    """Encrypt `payload` to the device, sign for its push service, POST it.
    pywebpush only builds the message; the request is ours, so it can be cut
    by the caller's deadline and never follows a redirect."""
    encoded = WebPusher({
        "endpoint": subscription.endpoint,
        "keys": {"p256dh": _b64(subscription.p256dh), "auth": _b64(subscription.auth)},
    }).encode(payload, "aes128gcm")
    parts = urlsplit(subscription.endpoint)
    # The token is for this push service only: its own scheme and host.
    audience = f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}"
    private = settings.web_push_vapid_private_key
    if private is None:
        raise RuntimeError("no VAPID private key")
    signed = Vapid.from_string(private.get_secret_value()).sign({
        "aud": audience,
        "sub": settings.web_push_subject,
        "exp": int(time.time()) + _TOKEN_LIFETIME,
    })
    headers = {
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(TTL),
        "Urgency": urgency,
        **signed,
    }
    async with httpx.AsyncClient(timeout=DEADLINE, follow_redirects=False,
                                 verify=_VERIFY) as client:
        response = await client.post(subscription.endpoint, content=encoded["body"],
                                     headers=headers)
    return response.status_code


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


__all__ = ["DEADLINE", "Kind", "Subscription", "TTL", "URLS", "notify"]
