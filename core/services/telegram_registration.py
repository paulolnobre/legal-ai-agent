"""Tell Telegram this bot's webhook and its "/" menu.

Run at every production start, and by `clients/demo_firm/set_webhook.py` by
hand. Republishing the menu used to be only the manual step, run after a
deploy that added a command, and it was forgotten: a command shipped and
nobody saw it when typing "/". Starting the bot now does it, so the menu is
the one the running code answers.

Both calls are idempotent, and they are made together on purpose: a menu
naming a webhook's worth of commands this release does not have would be
worse than no menu at all.

The bot token is part of every Telegram API URL, and httpx puts the URL in
its exception messages. Nothing here raises or logs one of those: a failure
is reported by method name, status code and exception class only.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from core.config import LEGAL_TELEGRAM_URL, settings
from core.handlers.legal_message import command_menu

logger = logging.getLogger(__name__)

# Telegram only forwards the update kinds named here. Anything else it drops
# before the webhook ever sees it, silently, on Telegram's own servers. This
# list is the reason inline-keyboard buttons went live in the handler and did
# nothing when pressed: callback_query was missing here, so every press was
# accepted by Telegram's client and discarded before reaching this bot. If a
# new update kind is ever handled (a poll answer, a chosen inline result), it
# needs a line here before the handler code can matter at all.
ALLOWED_UPDATES = ["message", "edited_message", "callback_query"]


class RegistrationFailed(Exception):
    """Telegram refused, or could not be asked. The message never holds a URL.

    `transient` is a failure worth trying again (no answer, a 5xx, a 429);
    anything else (bad configuration, a 4xx, a webhook another instance owns)
    will not fix itself by waiting.
    """

    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


def webhook_payload(webhook_url: str, secret_token: str) -> dict:
    return {
        "url": webhook_url,
        "secret_token": secret_token,
        "allowed_updates": ALLOWED_UPDATES,
        "drop_pending_updates": False,
    }


def _api_base() -> str:
    return LEGAL_TELEGRAM_URL


def _webhook_url() -> str:
    # PUBLIC_BASE_URL alone: the pages' own host (WEB_BASE_URL) must never
    # move where Telegram delivers.
    if not settings.public_base_url:
        raise RegistrationFailed("PUBLIC_BASE_URL is not configured.")
    url = f"{settings.public_base_url.rstrip('/')}/telegram/webhook"
    if not url.startswith("https://"):
        raise RegistrationFailed("The production webhook must use HTTPS.")
    return url


async def _call(client: httpx.AsyncClient, method: str, payload: dict) -> dict:
    try:
        response = await client.post(f"{_api_base()}/{method}", json=payload)
    except httpx.HTTPError as exc:
        raise RegistrationFailed(f"{method}: {type(exc).__name__}", transient=True) from None
    try:
        body = response.json()
        accepted = response.status_code == 200 and body.get("ok") is True
    except ValueError:
        body, accepted = {}, False
    if not accepted:
        status = response.status_code
        raise RegistrationFailed(f"{method} refused with status {status}",
                                 transient=status >= 500 or status == 429)
    return body


async def register(client: httpx.AsyncClient, *, move: bool = False) -> int:
    """Register the webhook, then the menu. Returns how many commands.

    Without `move`, a webhook that already points at another address is left
    alone: that is another instance holding this bot's token (a copy of
    production's .env on a second machine, say), and starting it must not
    take the firm's messages away from the real one. set_webhook.py passes
    `move=True`, because running it by hand is the decision to move it.
    """
    if not settings.legal_telegram_bot_token:
        raise RegistrationFailed("LEGAL_TELEGRAM_BOT_TOKEN is not configured.")
    if not settings.telegram_webhook_secret:
        raise RegistrationFailed("TELEGRAM_WEBHOOK_SECRET is not configured.")
    url = _webhook_url()
    if not move:
        current = (await _call(client, "getWebhookInfo", {})).get("result", {}).get("url", "")
        if current and current != url:
            raise RegistrationFailed(
                "the webhook points at another address; not moved. Run "
                "clients/demo_firm/set_webhook.py to move it here."
            )
    menu = command_menu()
    await _call(client, "setWebhook", webhook_payload(url, settings.telegram_webhook_secret))
    # setMyCommands replaces the whole list, so a command dropped from the
    # help disappears from the menu too.
    await _call(client, "setMyCommands", {"commands": menu})
    return len(menu)


def should_register_at_startup() -> bool:
    """Production, fully configured. A development run must never repoint
    production's webhook at itself."""
    return (settings.app_env == "production"
            and bool(settings.legal_telegram_bot_token)
            and bool(settings.telegram_webhook_secret)
            and bool(settings.public_base_url))


# After a failure worth retrying: about eight minutes in all, then the menu
# waits for the next start or for set_webhook.py. Retrying matters most after
# TELEGRAM_WEBHOOK_SECRET changed: until Telegram has the new secret, every
# delivery carries the old one and is refused.
RETRY_DELAYS = (15, 30, 60, 120, 240)


async def register_at_startup(
    client_factory: Callable[[], httpx.AsyncClient] = lambda: httpx.AsyncClient(timeout=30),
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> bool:
    """`register`, for the lifespan: never raises, since the webhook already
    registered keeps working and a Telegram outage must not keep the bot
    down. A failure is a warning in the logs; a transient one is retried."""
    for delay in (*RETRY_DELAYS, None):
        try:
            async with client_factory() as client:
                count = await register(client)
        except RegistrationFailed as exc:
            if exc.transient and delay is not None:
                logger.warning("Telegram webhook and menu not registered yet: %s; "
                               "retrying in %ds", exc, delay)
                await sleep(delay)
                continue
            logger.warning("Telegram webhook and menu not registered: %s", exc)
            return False
        except Exception as exc:  # anything else, still without its message
            logger.warning("Telegram webhook and menu not registered: %s", type(exc).__name__)
            return False
        host = urlsplit(settings.public_base_url or "").hostname
        logger.info("Telegram webhook registered at %s, menu with %d commands", host, count)
        return True
    return False


async def register_if_production() -> bool:
    """What the lifespan starts: registration when this is the configured
    production bot, nothing otherwise."""
    if not should_register_at_startup():
        return False
    return await register_at_startup()


__all__ = [
    "ALLOWED_UPDATES", "RETRY_DELAYS", "RegistrationFailed", "register", "register_at_startup",
    "register_if_production", "should_register_at_startup", "webhook_payload",
]
