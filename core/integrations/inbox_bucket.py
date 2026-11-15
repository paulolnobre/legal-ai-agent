"""Read the folder the firm drops documents into, in Magalu Cloud Object Storage.

It is a prefix (`entrada/` by default) of a bucket, the documents bucket unless
another one is named, read with the same key and the same signing as the
encrypted originals. One key per client company is the rule, so the inbox does
not get a second one.

**The prefix is what keeps the importer away from the originals.** Those live
at the root of the documents bucket under hexadecimal names with a real
extension, so an importer pointed at the whole bucket would find `.pdf` files,
download ciphertext and try to read it as a document. An empty prefix on the
documents bucket is therefore refused rather than configured by accident.

Read-only by construction: nothing here writes or deletes, so the firm's copy
stays the firm's copy and what has been read is remembered in the database.

The bucket is an input, so everything from it is untrusted. Names are never
used to build a path, sizes are checked before a download and again after, and
the content goes through the same ingestion pipeline a Telegram attachment
does, with the same extension allowlist and the same extraction.
"""
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime

from core.config import settings
from core.integrations import object_store

logger = logging.getLogger(__name__)


class InboxError(RuntimeError):
    """A safe failure reading the inbox. Never carries a credential."""


@dataclass(frozen=True)
class BucketFile:
    """One file in the inbox, as the listing describes it."""

    # What the ledger remembers it by: the name and the content's ETag
    # together, so a corrected file uploaded over an old name is a new file.
    file_id: str
    # The object's full key, which is what a download asks for.
    key: str
    # The key below the inbox prefix: what the firm sees in its own folder
    # tree, and what the classification reads.
    name: str
    size: int
    uploaded_at: datetime | None


def _location() -> tuple[str, str]:
    """The bucket and prefix to read, refusing the one configuration that
    would read the encrypted originals as if they were new documents."""
    bucket = (settings.inbox_bucket or settings.object_store_bucket or "").strip()
    prefix = (settings.inbox_prefix or "").strip().lstrip("/")
    if not (bucket and object_store.credentials_configured()):
        raise InboxError("o balde de documentos não está configurado")
    if not prefix and bucket == (settings.object_store_bucket or "").strip():
        raise InboxError(
            "a entrada precisa de uma pasta própria (INBOX_PREFIX) quando fica no "
            "mesmo balde dos documentos"
        )
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return bucket, prefix


def _file_id(key: str, etag: str) -> str:
    return "s3:" + hashlib.sha256(f"{key}\0{etag}".encode()).hexdigest()


async def list_files() -> list[BucketFile]:
    """Everything currently in the inbox folder."""
    bucket, prefix = _location()
    try:
        objects = await object_store.list_objects(bucket, prefix)
    except object_store.ObjectStoreError as exc:
        raise InboxError(str(exc).rstrip(".")) from exc

    files = [
        BucketFile(
            file_id=_file_id(obj.key, obj.etag),
            key=obj.key,
            name=obj.key[len(prefix):],
            size=obj.size,
            uploaded_at=obj.last_modified,
        )
        for obj in objects
        if obj.key[len(prefix):]
    ]
    logger.info("Inbox listing returned %d file(s)", len(files))
    return files


async def download(key: str, *, max_bytes: int) -> bytes:
    """One file's content, refused rather than truncated when it is too large.

    The size is checked twice, because the listing is a claim and the response
    is the fact: a file can grow between the two, and a truncated legal document
    ingested as if whole is worse than one that failed to arrive.
    """
    bucket, _prefix = _location()
    try:
        return await object_store.get_named(bucket, key, max_bytes=max_bytes)
    except object_store.ObjectStoreNotFound as exc:
        raise InboxError("o arquivo não está mais no balde") from exc
    except object_store.ObjectStoreError as exc:
        raise InboxError(str(exc).rstrip(".")) from exc


__all__ = ["BucketFile", "InboxError", "download", "list_files"]
