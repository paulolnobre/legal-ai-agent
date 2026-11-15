"""An attachment sent in the panel's chat, sealed until the worker reads it.

A photo, a voice note, an audio file or a document to archive arrives in its
own request, before the message that goes with it. It is sealed with the documents' key (the same
envelope as `panel_files`), so no plaintext copy sits in the database or its
backups, and it is never served back by any route.

Lifetime, in the queries themselves: an upload no message took expires after
15 minutes; once a message takes it, it lives until that request ends (done,
failed, withdrawn) and at most 24 hours from its arrival, so a message
deferred to the morning keeps its attachment. The maintenance worker purges
the rest on every 15-minute check.

Storing locks the person's staff row `FOR SHARE` and checks they are still
active, the row `erase_person` locks `FOR UPDATE`: an upload finishing during
an erasure either lands before it (and the erasure deletes it) or is refused.
The table is created by `panel.ensure_panel_schema`, after the requests it
references.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from core import document_crypto
from core.config import settings

logger = logging.getLogger(__name__)

UploadKind = Literal["photo", "voice", "audio", "document"]
KINDS: tuple[UploadKind, ...] = ("photo", "voice", "audio", "document")

# Serialises one person's stores, so two at once cannot both fit under the
# live cap that only one of them fits under.
_UPLOADS_LOCK = 0x75706C64  # "upld"

# Whether an upload is still alive, as every query here reads it: not past
# its own expiry, not older than a day, and either waiting for its message or
# taken by a request that has not ended.
_LIVE = """
    expires_at > NOW()
    AND created_at > NOW() - make_interval(hours => %s)
    AND (request_id IS NULL OR EXISTS (
        SELECT 1 FROM panel_requests r WHERE r.id = panel_uploads.request_id
          AND r.status IN ('sending', 'queued', 'deferred', 'running')))
"""


class UploadUnavailable(Exception):
    """The upload a message names is not this person's, already taken, or
    expired: the message is not written."""


@dataclass(frozen=True)
class UploadInfo:
    """An upload as its row describes it, its seal unopened: enough to
    refuse a request (its limit, its rank) before anything is decrypted."""
    id: UUID
    staff_id: int
    kind: UploadKind
    media_type: str
    archive: bool
    # A document's name as its sender gave it, checked when it arrived;
    # shown and used as its title, never as a path. None for media.
    filename: str | None = None
    size_bytes: int = 0


@dataclass(frozen=True, kw_only=True)
class Upload(UploadInfo):
    """An upload unsealed (`take_upload`)."""
    data: bytes = field(repr=False)


def max_bytes(kind: UploadKind) -> int:
    return {
        "photo": settings.panel_photo_max_bytes,
        "voice": settings.panel_voice_max_bytes,
        "audio": settings.panel_audio_max_bytes,
        "document": settings.max_doc_mb * 1024 * 1024,
    }[kind]


def _seal(data: bytes) -> tuple[str, document_crypto.Sealed]:
    content_sha = hashlib.sha256(data).hexdigest()
    return content_sha, document_crypto.seal(
        data, master_key=settings.doc_master_key, document_sha=content_sha)


async def store_upload(conn, staff_id: int, *, kind: UploadKind, media_type: str,
                       data: bytes, archive: bool,
                       filename: str | None = None) -> UUID | None:
    """Seal one attachment for this person and return its id, or None when
    they are no longer active, it is over its kind's size, or it would take
    them over their live total. A document carries its name; media never
    does."""
    if kind not in KINDS:
        raise ValueError(f"Unknown upload kind: {kind!r}")
    if (kind == "document") != (filename is not None):
        raise ValueError("A document carries its name, and only a document does.")
    if not data or len(data) > max_bytes(kind):
        return None
    # Up to 100 MB: hashed and sealed off the event loop.
    content_sha, sealed = await asyncio.to_thread(_seal, data)
    upload_id: UUID | None = uuid.uuid4()
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute("SELECT active FROM staff WHERE id = %s FOR SHARE", (staff_id,))
            row = await cur.fetchone()
            if not row or not row[0]:
                upload_id = None
            else:
                await cur.execute("SELECT pg_advisory_xact_lock(%s, %s)",
                                  (_UPLOADS_LOCK, staff_id))
                await cur.execute(
                    "SELECT COALESCE(SUM(size_bytes), 0) FROM panel_uploads "
                    "WHERE staff_id = %s AND" + _LIVE,
                    (staff_id, int(settings.panel_upload_max_hours)),
                )
                live_row = await cur.fetchone()
                live = int(live_row[0]) if live_row else 0
                if live + len(data) > settings.panel_uploads_max_live_bytes:
                    upload_id = None
                else:
                    await cur.execute(
                        """
                        INSERT INTO panel_uploads (id, staff_id, kind, media_type, size_bytes,
                                                   sealed, wrapped_key, content_sha, archive,
                                                   filename, expires_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                NOW() + make_interval(mins => %s))
                        """,
                        (upload_id, staff_id, kind, media_type, len(data), sealed.blob,
                         sealed.wrapped_key, content_sha, bool(archive), filename,
                         int(settings.panel_upload_unattached_minutes)),
                    )
    await conn.commit()
    return upload_id


async def attach_upload(conn, upload_id: UUID, *, staff_id: int, request_id: UUID,
                        commit: bool = True) -> bool:
    """Give this person's unattached, unexpired upload to their request. Its
    life is then the request's, and at most a day from its arrival.

    `commit=False` inside the transaction that writes the request, so the
    two land together or not at all."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE panel_uploads
               SET request_id = %s,
                   expires_at = created_at + make_interval(hours => %s)
             WHERE id = %s AND staff_id = %s AND request_id IS NULL
               AND expires_at > NOW()
               AND EXISTS (SELECT 1 FROM panel_requests
                           WHERE id = %s AND staff_id = %s)
            """,
            (request_id, int(settings.panel_upload_max_hours), upload_id, staff_id,
             request_id, staff_id),
        )
        attached = (cur.rowcount or 0) == 1
    if commit:
        await conn.commit()
    return attached


async def peek_upload(conn, request_id: UUID) -> UploadInfo | None:
    """What this request's attachment is, without opening its seal, or None:
    none attached, or past its day. Read only."""
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, staff_id, kind, media_type, archive, filename, size_bytes
                FROM panel_uploads
                WHERE request_id = %s AND created_at > NOW() - make_interval(hours => %s)
                """,
                (request_id, int(settings.panel_upload_max_hours)),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    upload_id, staff_id, kind, media_type, archive, filename, size_bytes = row
    return UploadInfo(id=upload_id, staff_id=int(staff_id), kind=kind, media_type=media_type,
                      archive=bool(archive), filename=filename, size_bytes=int(size_bytes))


async def take_upload(conn, request_id: UUID) -> Upload | None:
    """The attachment of this request, unsealed, or None: none attached, past
    its day, or a seal that does not open. Read only; the caller deletes it
    once its reading is kept. Whoever may refuse the request decides first,
    on `peek_upload`: nothing is decrypted for a refused caller."""
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, staff_id, kind, media_type, archive, sealed, wrapped_key,
                       content_sha, filename, size_bytes
                FROM panel_uploads
                WHERE request_id = %s AND created_at > NOW() - make_interval(hours => %s)
                """,
                (request_id, int(settings.panel_upload_max_hours)),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    (upload_id, staff_id, kind, media_type, archive, sealed, wrapped_key, content_sha,
     filename, size_bytes) = row
    try:
        data = await asyncio.to_thread(
            document_crypto.open_sealed, bytes(sealed), bytes(wrapped_key),
            master_key=settings.doc_master_key, document_sha=content_sha,
        )
    except document_crypto.EncryptionError:
        logger.error("A panel upload did not open: its seal or its key does not match")
        return None
    return Upload(id=upload_id, staff_id=int(staff_id), kind=kind, media_type=media_type,
                  archive=bool(archive), data=data, filename=filename,
                  size_bytes=int(size_bytes))


async def delete_upload(conn, upload_id: UUID, commit: bool = True) -> None:
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM panel_uploads WHERE id = %s", (upload_id,))
    if commit:
        await conn.commit()


async def delete_request_upload(conn, request_id: UUID, commit: bool = True) -> None:
    """Delete whatever upload this request holds, alive or not: a request
    refused before its attachment was read leaves none behind."""
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM panel_uploads WHERE request_id = %s", (request_id,))
    if commit:
        await conn.commit()


async def purge_uploads(conn) -> int:
    """Delete every upload no longer alive: unattached past its 15 minutes,
    attached to a request that ended, or older than a day."""
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM panel_uploads WHERE NOT (" + _LIVE + ")",
            (int(settings.panel_upload_max_hours),),
        )
        deleted = cur.rowcount or 0
    await conn.commit()
    return deleted


__all__ = ["KINDS", "Upload", "UploadInfo", "UploadKind", "UploadUnavailable",
           "attach_upload", "delete_request_upload", "delete_upload", "max_bytes",
           "peek_upload", "purge_uploads", "store_upload", "take_upload"]
