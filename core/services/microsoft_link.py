"""Connecting a Microsoft account from the panel's integrations page.

Telegram waits for the sign-in in a background task. The page asks instead:
each ask polls Microsoft at most once, and never before the interval
Microsoft set, so nothing runs between asks and a restart only costs a new
code. The device code stays on the server, sealed.
"""
from __future__ import annotations

import logging
from typing import Literal

from core.config import settings
from core.integrations import microsoft
from core.models import StaffMember
from core.repositories import microsoft_accounts as accounts

logger = logging.getLogger(__name__)

Outcome = Literal["aguardando", "conectada", "recusada", "erro", "sem_codigo"]
Refusal = Literal["ja_conectada", "indisponivel", "erro"]

# What Microsoft asks for on `slow_down`, as `microsoft.wait_for_sign_in` does.
_SLOW_DOWN_SECONDS = 5


def available() -> bool:
    return settings.ms_enabled and microsoft.configured()


async def begin(conn, staff: StaffMember) -> accounts.SignIn | Refusal:
    """A new code for this person, replacing any earlier one."""
    if not available():
        return "indisponivel"
    if await accounts.get_account(conn, staff.id) is not None:
        return "ja_conectada"
    try:
        flow = await microsoft.start_device_flow()
    except microsoft.MicrosoftError:
        logger.warning("Could not start a Microsoft sign-in from the panel")
        return "erro"
    await accounts.start_sign_in(conn, staff.id, device_code=flow.device_code,
                                 user_code=flow.user_code,
                                 verification_uri=flow.verification_uri,
                                 interval_seconds=flow.interval, expires_in=flow.expires_in)
    sign_in = await accounts.live_sign_in(conn, staff.id)
    return sign_in if sign_in is not None else "erro"


async def check(conn, staff: StaffMember) -> Outcome:
    """Ask Microsoft once, if a poll is due, whether the person signed in."""
    claimed = await accounts.claim_poll(conn, staff.id)
    if claimed is None:
        live = await accounts.live_sign_in(conn, staff.id)
        return "aguardando" if live is not None else "sem_codigo"
    try:
        tokens = await microsoft.poll_once(claimed.device_code)
        account = await microsoft.whoami(tokens.access_token)
    except microsoft.SignInPending as pending:
        more = _SLOW_DOWN_SECONDS if str(pending) == "slow_down" else 0
        await accounts.delay_poll(conn, staff.id, claimed.flow_id,
                                  claimed.interval_seconds + more)
        return "aguardando"
    except microsoft.SignInDeclined:
        await accounts.end_sign_in(conn, staff.id, claimed.flow_id)
        return "recusada"
    except microsoft.MicrosoftError:
        await accounts.delay_poll(conn, staff.id, claimed.flow_id, claimed.interval_seconds)
        # Microsoft's own words are not logged: they may repeat the device
        # code, which is a credential until it expires.
        logger.warning("A Microsoft sign-in poll failed")
        return "erro"
    connected = await accounts.finish_sign_in(conn, staff.id, claimed.flow_id,
                                              account_name=account,
                                              refresh_token=tokens.refresh_token)
    return "conectada" if connected else "recusada"


__all__ = ["Outcome", "Refusal", "available", "begin", "check"]
