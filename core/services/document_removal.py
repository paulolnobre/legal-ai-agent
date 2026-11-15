"""Removing a document from the archive, in the order that fails safely.

Deletion touches three things that cannot be committed together: a database
row, a file on disk, and a cache of answers built from both. Nothing here can
make them atomic, so the order is chosen by what each failure leaves behind.

    1. The row and the audit entry go in one transaction. Either the archive
       forgot the document and recorded who made it forget, or neither
       happened. There is no state where a document vanishes unattributed.

    2. The file is unlinked after that commit. A file removed first, before a
       failed commit, would leave a live document whose original is silently
       gone, and the firm would find out the day a lawyer asked for it. A row
       removed first, before a failed unlink, leaves an orphan file that this
       function reports by name, so it can be dealt with.

    3. The answer cache is dropped last, and its failure is not fatal. A stale
       answer is wrong for at most its TTL; a deletion refused because Redis
       was unreachable would be wrong until someone tried again.

The audit entry outlives the document on purpose. `document_access_log` holds
integer ids rather than a foreign key, so the record of who read a document,
and of who deleted it, survives the document itself. That is the point of an
audit log: it answers questions about things that are no longer there.
"""
import logging
from dataclasses import dataclass

from core.integrations import cache
from core.repositories.documents import delete_document, log_access
from core.services import document_storage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RemovalOutcome:
    """What happened, in enough detail for the reply to be honest about it."""

    status: str  # "removed" | "not_found"
    title: str = ""
    original_removed: bool = False
    # The original stayed because another document is stored under the same
    # file. Its bytes are identical, so this is not a leftover.
    original_kept_for_another_document: bool = False
    # The row is gone and the file is not. The key is carried out so the reply
    # can name it, because a client's document still on disk is exactly what a
    # deletion was asked to prevent.
    orphaned_storage_key: str | None = None


async def remove_document(conn, document_id: int, *, staff_id, chat_id) -> RemovalOutcome:
    """Delete one document: its row, its chunks, its original and its answers."""
    deletion = await delete_document(conn, document_id, commit=False)
    if deletion.status == "not_found":
        await conn.rollback()
        return RemovalOutcome(status="not_found")

    await log_access(
        conn,
        action="delete_document",
        staff_id=staff_id,
        chat_id=chat_id,
        document_ids=[document_id],
        commit=False,
    )
    await conn.commit()

    original_removed = False
    orphaned: str | None = None
    if deletion.storage_key:
        try:
            await document_storage.remove(deletion.storage_key, deletion.wrapped_key)
            original_removed = True
        except OSError:
            orphaned = deletion.storage_key
            logger.exception(
                "Deleted document %s but could not remove its original", document_id,
            )

    # The corpus just shrank, so every cached answer was true for an archive
    # that no longer exists. Ingestion drops them for the same reason.
    try:
        await cache.invalidate_answers()
    except Exception:  # pragma: no cover - cache failures must not undo a deletion
        logger.exception("Deleted document %s but could not clear the answer cache",
                         document_id)

    logger.info(
        "Document deleted: id=%s original_removed=%s shared=%s",
        document_id,
        original_removed,
        deletion.shared_original,
    )
    return RemovalOutcome(
        status="removed",
        title=deletion.title,
        original_removed=original_removed,
        original_kept_for_another_document=deletion.shared_original,
        orphaned_storage_key=orphaned,
    )


__all__ = ["RemovalOutcome", "remove_document"]
