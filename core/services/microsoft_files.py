"""Reading a lawyer's own OneDrive, on their behalf and nobody else's.

Every call here spends that person's stored refresh token and no one else's,
which is what makes this safe to offer to ten people whose Microsoft accounts
have nothing to do with each other: the isolation is Microsoft's, and this
code only has to avoid mixing up whose token it is holding.

Two things are easy to get wrong and are therefore done in one place.

**A refreshed token is stored back.** Microsoft may hand out a new refresh
token every time one is spent, and the old one then stops working. A caller
that ignores the new one keeps working for hours and then finds the
connection dead for no visible reason.

**A file from somebody's drive lands in their own folder.** It is their
material, from their account, and the archive's rule since the folders landed
is that what a person brings is theirs until they share it.
"""
from __future__ import annotations

import logging

from core.config import settings
from core.integrations import microsoft
from core.repositories.microsoft_accounts import (
    get_refresh_token,
    save_account,
    touch,
)
from core.services.ingestion import Ingested, ingest_document

logger = logging.getLogger(__name__)


class NotConnected(RuntimeError):
    """This person has not connected a Microsoft account, or it stopped working."""


async def _access_token(conn, staff_id: int, account_name: str = "") -> str:
    """Spend this person's refresh token, and store whatever comes back.

    Raises NotConnected when Microsoft refuses it: revoked, expired, or a
    password change. The row is left in place, because the person may simply
    reconnect, and deleting it here would lose the account name that tells
    them which account it was.
    """
    stored = await get_refresh_token(conn, staff_id)
    if not stored:
        raise NotConnected("no Microsoft account is connected")
    try:
        tokens = await microsoft.access_token_from(stored)
    except microsoft.SignInDeclined as declined:
        raise NotConnected(str(declined)) from declined
    if tokens.refresh_token != stored:
        await save_account(
            conn, staff_id,
            account_name=account_name or await microsoft.whoami(tokens.access_token),
            refresh_token=tokens.refresh_token,
        )
    await touch(conn, staff_id)
    return tokens.access_token


async def list_files(conn, staff_id: int, query: str = "",
                     limit: int = 10) -> list[microsoft.DriveItem]:
    """What this person has in their drive, searched or most recent."""
    token = await _access_token(conn, staff_id)
    return await microsoft.list_files(token, query=query, limit=limit)


async def import_file(conn, staff_id: int, item: microsoft.DriveItem) -> Ingested:
    """Bring one file into the archive, in this person's own folder.

    The size is checked twice, as everything downloaded here is: once against
    what the listing claimed, which is a claim, and once against the bytes
    that arrived, which is the fact.
    """
    ceiling = settings.max_doc_mb * 1024 * 1024
    if item.size > ceiling:
        raise ValueError(f"o arquivo tem {item.size // (1024 * 1024)} MB, "
                         f"acima do limite de {settings.max_doc_mb} MB")
    token = await _access_token(conn, staff_id)
    data = await microsoft.download_file(token, item.item_id, max_bytes=ceiling)
    logger.info("Importing %s bytes from OneDrive for staff %s", len(data), staff_id)
    return await ingest_document(
        conn, data,
        filename=item.name,
        uploaded_by=staff_id,
        # Their drive, their document. Sharing it is a separate, deliberate act.
        visibility="private",
    )


__all__ = ["NotConnected", "import_file", "list_files"]
