"""Tell Telegram about this bot: where to deliver, and what it can be asked.

The bot already does this itself at every production start
(`core/services/telegram_registration.py`). This script is the same call by
hand, for when the bot cannot be restarted, and unlike the start it stops
with an error when Telegram refuses. It is also the only way to move the
webhook from another address: the start leaves one it does not own alone.
"""
import asyncio
import os
import sys

import httpx

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

from core.config import settings  # noqa: E402
from core.services.telegram_registration import register  # noqa: E402


async def _main() -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        count = await register(client, move=True)
    print(f"Webhook configured at {(settings.public_base_url or '').rstrip('/')}/telegram/webhook")
    print(f"Command menu registered with {count} commands")


if __name__ == "__main__":
    asyncio.run(_main())
