"""Documents in a bucket, encrypted here before they leave.

This is the join between two modules that each do one thing:
`core.document_crypto` turns bytes into ciphertext plus a key to keep, and
`core.integrations.object_store` moves bytes. Neither knows about the other, so the
question "is anything written unencrypted?" has exactly one place to be
answered, which is this file.

**The two halves go to different places, and that is the design.** The
ciphertext goes to the bucket, where it may be copied, versioned and backed up
freely because without its key it is noise. The wrapped key goes back to the
caller to be stored in the document's row, where deleting it is a real
deletion. That is what turns LGPD erasure from "reach every copy in every
bucket and every backup, and prove it" into "delete one row".

**Nothing is written if the master key is missing.** Not "written in clear
with a warning", not "written and fixed later": the write fails. A single
unencrypted object in a bucket is indistinguishable from all the others
afterwards, and nobody would find it.
"""
from __future__ import annotations

import logging

from core import document_crypto
from core.config import settings
from core.integrations import object_store

logger = logging.getLogger(__name__)


class StorageUnavailable(RuntimeError):
    """The bucket or the key is not configured. Never carries a credential."""


def configured() -> bool:
    """Whether a document can be stored in the bucket at all."""
    return object_store.configured() and bool((settings.doc_master_key or "").strip())


def _ready() -> None:
    if not object_store.configured():
        raise StorageUnavailable(
            "O armazenamento de documentos não está configurado neste servidor."
        )
    if not (settings.doc_master_key or "").strip():
        # Deliberately not a fallback to writing in clear: see the module note.
        raise StorageUnavailable(
            "A chave de criptografia de documentos não está configurada, "
            "então nada pode ser gravado no armazenamento."
        )


async def store(data: bytes, *, key: str, raw_sha256: str) -> tuple[bytes, int]:
    """Encrypt one document and put it in the bucket under `key`.

    The object key is passed in rather than built here. How originals are
    named is the archive's decision and it already has one; a second opinion
    in this module would be a second place for that format to change.

    Returns the wrapped data key and the master-key version. The caller stores
    both in the document's row; losing them is what makes the object
    unrecoverable, which is the point.
    """
    _ready()
    sealed = document_crypto.seal(
        data, master_key=settings.doc_master_key, document_sha=raw_sha256,
    )
    await object_store.put_object(key, sealed.blob)
    logger.info(
        "Stored an encrypted document: %s plaintext bytes, %s stored",
        len(data), len(sealed.blob),
    )
    return sealed.wrapped_key, sealed.kek_version


async def read(key: str, wrapped_key: bytes, *, raw_sha256: str) -> bytes:
    """Fetch one document and open it.

    A document whose row was deleted cannot be read even if its object is
    still in the bucket, because the wrapped key went with the row. That is
    the erasure guarantee working, not a fault.
    """
    _ready()
    blob = await object_store.get_object(key)
    return document_crypto.open_sealed(
        blob, wrapped_key, master_key=settings.doc_master_key, document_sha=raw_sha256,
    )


async def remove(key: str) -> None:
    """Delete the object.

    Best effort on purpose. The deletion that matters already happened when
    the row carrying the wrapped key went, and an object left behind here is
    unreadable noise rather than a leak. So a bucket that is briefly
    unreachable must not turn a completed erasure into a failed one.
    """
    if not object_store.configured():
        return
    try:
        await object_store.delete_object(key)
    except object_store.ObjectStoreError:
        logger.exception(
            "Could not delete %s from the bucket. The object is unreadable "
            "without its key, which the database no longer holds.", key,
        )


__all__ = ["StorageUnavailable", "configured", "read", "remove", "store"]
