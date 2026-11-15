"""Where an original document lives, and the one place that decides.

The agent searches extracted text and vectors in PostgreSQL. Originals live
either in a private Docker volume on this host or, once a bucket and a master
key are configured, encrypted in Magalu Cloud Object Storage. Callers ask this module and do
not choose.

**A document says where it lives, rather than a setting saying it.** One
stored in the bucket has a wrapped key; one in the volume does not, because
bucket objects are always encrypted and volume files never are. So the
presence of that key is the marker, and there is no second column to drift out
of agreement with reality. It also means turning the bucket on does not strand
anything: old documents keep being read from the volume, new ones go to the
bucket, and no migration has to run before the feature works.

**Nothing silently changes destination.** With a bucket configured but no master key,
a write fails rather than falling back to the volume: a document the firm
believes is encrypted, sitting in clear on a disk, is worse than a write that
refused.
"""
from __future__ import annotations

import logging
import os
import secrets
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from core.config import settings
from core.services import encrypted_storage

logger = logging.getLogger(__name__)

_SAFE_EXTENSION = re.compile(r"^\.[a-z0-9]{1,10}$")


def _root() -> Path:
    root = Path(settings.document_storage_path).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def _path_for(key: str) -> Path:
    root = _root()
    path = (root / key).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("Invalid document storage key.") from exc
    return path


def storage_key(raw_sha256: str, filename: str) -> str:
    """Return a stable key without retaining user-controlled path components."""
    extension = Path(filename).suffix.lower()
    if not _SAFE_EXTENSION.fullmatch(extension):
        extension = ""
    return f"{raw_sha256[:2]}/{raw_sha256[2:4]}/{raw_sha256}{extension}"


def store_original(data: bytes, *, raw_sha256: str, filename: str) -> tuple[str, bool]:
    """Atomically store an original and return ``(key, newly_created)``."""
    key = storage_key(raw_sha256, filename)
    destination = _path_for(key)
    if destination.is_file():
        return key, False
    return key, _write_new(data, destination)


def store_exclusive_original(data: bytes, *, filename: str) -> str:
    """Store an original under a fresh name of its own and return its key.

    For a write that may be taken back before any document refers to it:
    under a content-addressed name, a second preparation of the same bytes
    would find this file, a document would come to refer to it, and taking
    this one back would leave that document without its original. A file of
    its own is only ever this preparation's to remove. The name has the
    shape of the content-addressed one, so the restore drill reads it alike.
    """
    key = bucket_key(filename)
    if not _write_new(data, _path_for(key)):
        raise FileExistsError("A fresh storage key was already taken.")
    return key


def _write_new(data: bytes, destination: Path) -> bool:
    """Write `data` at `destination` unless a file is already there: True
    when this call created it."""
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".upload-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o600)
        try:
            os.link(temporary_name, destination)
        except FileExistsError:
            return False
        return True
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def read_original(key: str) -> bytes | None:
    """Read a previously stored original; missing files are not exceptional."""
    path = _path_for(key)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def remove_original(key: str) -> None:
    """Unlink one stored original.

    Two callers, both after the database has decided: ingestion, discarding a
    file it just wrote when the transaction that would have referenced it
    failed; and deletion, removing the original of a document the owner
    deleted, once the row is gone and no other document shares the file.

    Missing is success. Nothing here decides whether unlinking is safe.
    """
    try:
        _path_for(key).unlink()
    except FileNotFoundError:
        pass


# ── The facade every caller uses ─────────────────────────────────────────────


@dataclass(frozen=True)
class StoredOriginal:
    """Where a document went, and what has to be kept to read it back.

    `wrapped_key` is empty for a document in the local volume and set for one
    in the bucket. That single field is how every later read knows where to
    look, so it travels into the document's row and back out again.
    """

    key: str
    wrapped_key: bytes = b""
    kek_version: int = 0
    newly_created: bool = True

    @property
    def in_bucket(self) -> bool:
        return bool(self.wrapped_key)


def bucket_key(filename: str) -> str:
    """A fresh object name, unique to this stored copy.

    Deliberately not content-addressed, unlike the volume. Encryption gives
    every copy its own data key and nonce, so the same bytes stored twice are
    two different ciphertexts. Under a content-addressed name the second write
    would overwrite the first, and the first document's wrapped key would then
    open nothing: it would be silently unreadable from that moment, with
    nothing to notice.

    Deduplication is what content addressing bought, and encryption already
    spent it: two copies of one file were never going to share an object here.
    So the name is random, collisions do not happen, and no write ever
    destroys another document's original.
    """
    extension = Path(filename).suffix.lower()
    if not _SAFE_EXTENSION.fullmatch(extension):
        extension = ""
    name = secrets.token_hex(32)
    return f"{name[:2]}/{name[2:4]}/{name}{extension}"


async def store(data: bytes, *, raw_sha256: str, filename: str,
                exclusive: bool = False) -> StoredOriginal:
    """Put one original wherever this deployment keeps them.

    `exclusive` gives it a file of its own on the volume too (the bucket
    always does): for an original stored before the database decides whether
    a document will hold it, so that taking it back can never remove a file
    another document holds."""
    if encrypted_storage.configured():
        key = bucket_key(filename)
        wrapped, version = await encrypted_storage.store(
            data, key=key, raw_sha256=raw_sha256,
        )
        return StoredOriginal(key=key, wrapped_key=wrapped, kek_version=version)
    if _bucket_half_configured():
        # Refusing rather than quietly writing to the volume: see the module
        # note. An operator who set up a bucket and forgot the key must find
        # out now, not when somebody asks where a document is.
        raise encrypted_storage.StorageUnavailable(
            "O bucket está configurado mas a chave de criptografia não, então "
            "não vou gravar este documento em lugar nenhum."
        )
    if exclusive:
        return StoredOriginal(key=store_exclusive_original(data, filename=filename))
    key, created = store_original(data, raw_sha256=raw_sha256, filename=filename)
    return StoredOriginal(key=key, newly_created=created)


async def read(key: str, wrapped_key: bytes | None = None, *,
               raw_sha256: str = "") -> bytes | None:
    """Fetch one original from wherever it was put."""
    if wrapped_key:
        return await encrypted_storage.read(key, wrapped_key, raw_sha256=raw_sha256)
    return read_original(key)


async def remove(key: str, wrapped_key: bytes | None = None) -> None:
    """Delete one original from wherever it is."""
    if wrapped_key:
        await encrypted_storage.remove(key)
        return
    remove_original(key)


def _bucket_half_configured() -> bool:
    """A bucket set up without a key, or a key without a bucket."""
    from core.integrations import object_store
    has_key = bool((settings.doc_master_key or "").strip())
    return object_store.configured() != has_key and (object_store.configured() or has_key)


__all__ = [
    "StoredOriginal", "bucket_key", "read", "read_original", "remove",
    "remove_original", "storage_key", "store", "store_exclusive_original",
    "store_original",
]
