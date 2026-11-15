"""Encrypting a document before it leaves this host for a bucket.

The point is narrow and worth stating exactly, because client-side encryption
is routinely sold as more than it is: this makes the storage and backup
providers unable to read the firm's documents. It does **not** protect against
somebody who owns this server, because the key that opens the documents has to
be readable by the process that serves them.

That narrow thing is the one LGPD cares about here. The documents leave this
host, to a processor the firm does not control, and this is what makes that
transfer carry ciphertext instead of client files.

**Envelope, and why it earns its complexity.** Each document gets its own data
key, used once. That key is wrapped with the master key and stored in the
document's row, never beside the ciphertext. Two things follow, and both are
the reason for the design rather than decoration.

Rotating the master key re-wraps keys in the database, a few thousand small
rows, instead of re-encrypting every object in the bucket, which is the whole
archive pulled down and pushed back.

Deleting the row destroys the only copy of that document's data key, so the
object in the bucket becomes unrecoverable noise even if it survives: an old
version, a bucket nobody swept, a backup that is immutable on purpose. Erasure
under LGPD art. 18 stops being "reach every copy, prove you reached them all"
and becomes "delete one row", which is a thing that can actually be shown.

**Nonce discipline.** A repeated nonce under one key does not weaken GCM, it
destroys it: the keystream repeats and the authentication key itself becomes
recoverable. So every nonce here is 12 random bytes from the OS, and no key is
ever used for a second message. A data key encrypts one document; a wrap
encrypts one data key. Nothing in this module reuses a key across calls, which
is what makes random nonces safe at this size.

**The ciphertext is bound to the document it belongs to.** The document's
sha256 is passed as additional authenticated data, so somebody holding the
bucket cannot move object A's bytes over object B and have them decrypt into
the wrong case file. Without that, every object is interchangeable with every
other, and the encryption is silent about which document it is protecting.

A note on what is deliberately absent: Python cannot reliably wipe a key from
memory, and a `del` here would be theatre that reads as a guarantee. The data
key lives as long as the request does.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32      # AES-256
NONCE_BYTES = 12    # what GCM is specified for; longer is not stronger here

# Stored as the first byte of every wrapped key, so a rotation can tell which
# master key a row was wrapped with and re-wrap only what is stale. Without it
# a rotation is an all-or-nothing migration with no way to resume after a
# failure halfway through.
CURRENT_KEK_VERSION = 1


class EncryptionError(RuntimeError):
    """A failure worth showing somebody. Never carries key material."""


@dataclass(frozen=True)
class Sealed:
    """One encrypted document, ready to be split between bucket and database.

    The split is the whole design: `blob` goes to the object store and may be
    copied, versioned and backed up freely, because without `wrapped_key` it
    is noise. `wrapped_key` goes in the document's row, where deleting it is a
    real deletion.
    """

    blob: bytes          # nonce + ciphertext + tag. To the bucket, and to backups.
    wrapped_key: bytes   # version + nonce + wrapped data key. To Postgres.
    kek_version: int


def new_master_key() -> str:
    """A fresh master key, for the operator to place in the environment.

    Printed once and escrowed off this host, because losing it makes every
    stored document permanently unreadable. That risk is larger than the one
    the encryption removes, so the escrow is not optional and a key nobody has
    verified restoring from is not escrowed.
    """
    return os.urandom(KEY_BYTES).hex()


def _master(raw: str | None) -> bytes:
    """The master key from configuration, or an error saying what to do.

    The three failures are told apart because their fixes are different: not
    set at all, not hex, and the right hex of the wrong length. A single
    "invalid key" would send somebody looking for a corrupted value when the
    variable is simply missing.
    """
    text = (raw or "").strip()
    if not text:
        raise EncryptionError(
            "DOC_MASTER_KEY is not set, so no document can be stored. "
            'Generate one with: python -c "from core.document_crypto import '
            'new_master_key; print(new_master_key())" and keep a copy outside '
            "this host: without it every stored document is unreadable."
        )
    try:
        key = bytes.fromhex(text)
    except ValueError as exc:
        raise EncryptionError(
            "DOC_MASTER_KEY is not valid hexadecimal."
        ) from exc
    if len(key) != KEY_BYTES:
        raise EncryptionError(
            f"DOC_MASTER_KEY must be {KEY_BYTES} bytes, which is "
            f"{KEY_BYTES * 2} hexadecimal characters; this one decodes to "
            f"{len(key)} bytes."
        )
    return key


def seal(plaintext: bytes, *, master_key: str | None, document_sha: str) -> Sealed:
    """Encrypt one document. Its data key exists only for this call."""
    if not document_sha.strip():
        # The binding is not optional: without it any object decrypts as any
        # other, which is the failure this is here to prevent.
        raise EncryptionError("A document cannot be sealed without its sha256.")
    kek = _master(master_key)
    aad = document_sha.strip().encode()

    data_key = os.urandom(KEY_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    blob = nonce + AESGCM(data_key).encrypt(nonce, plaintext, aad)

    wrap_nonce = os.urandom(NONCE_BYTES)
    wrapped = (
        bytes([CURRENT_KEK_VERSION])
        + wrap_nonce
        + AESGCM(kek).encrypt(wrap_nonce, data_key, aad)
    )
    return Sealed(blob=blob, wrapped_key=wrapped, kek_version=CURRENT_KEK_VERSION)


def open_sealed(blob: bytes, wrapped_key: bytes, *, master_key: str | None,
                document_sha: str) -> bytes:
    """Decrypt one document, or fail closed.

    A wrong key, a tampered ciphertext and bytes belonging to another document
    all read the same to the caller, because telling them apart tells whoever
    is guessing which guess was closer.
    """
    kek = _master(master_key)
    aad = (document_sha or "").strip().encode()
    if len(wrapped_key) <= 1 + NONCE_BYTES or len(blob) <= NONCE_BYTES:
        raise EncryptionError("O documento guardado está incompleto.")

    version = wrapped_key[0]
    if version != CURRENT_KEK_VERSION:
        # Not fatal in itself: a rotation in progress leaves both versions
        # around, and the caller is the one that knows whether it still holds
        # the older master key.
        raise EncryptionError(
            f"Este documento foi selado com a chave mestra versão {version}, "
            f"e a chave em uso é a versão {CURRENT_KEK_VERSION}."
        )
    wrap_nonce = wrapped_key[1:1 + NONCE_BYTES]
    wrapped = wrapped_key[1 + NONCE_BYTES:]
    try:
        data_key = AESGCM(kek).decrypt(wrap_nonce, wrapped, aad)
        return AESGCM(data_key).decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], aad)
    except InvalidTag as exc:
        raise EncryptionError(
            "Não consegui abrir este documento. A chave não confere, ou o "
            "conteúdo foi alterado depois de gravado."
        ) from exc


def rewrap(wrapped_key: bytes, *, old_master_key: str | None,
           new_master_key: str | None, document_sha: str) -> bytes:
    """Move one document's data key to a new master key.

    This is what a master-key rotation costs: one small row per document,
    rather than downloading, decrypting, re-encrypting and re-uploading the
    archive. The document's own ciphertext is never touched.
    """
    kek_old = _master(old_master_key)
    kek_new = _master(new_master_key)
    aad = (document_sha or "").strip().encode()
    if len(wrapped_key) <= 1 + NONCE_BYTES:
        raise EncryptionError("A chave guardada deste documento está incompleta.")
    try:
        data_key = AESGCM(kek_old).decrypt(
            wrapped_key[1:1 + NONCE_BYTES], wrapped_key[1 + NONCE_BYTES:], aad,
        )
    except InvalidTag as exc:
        raise EncryptionError(
            "A chave mestra antiga não abre este documento."
        ) from exc
    nonce = os.urandom(NONCE_BYTES)
    return (
        bytes([CURRENT_KEK_VERSION])
        + nonce
        + AESGCM(kek_new).encrypt(nonce, data_key, aad)
    )


__all__ = [
    "CURRENT_KEK_VERSION", "EncryptionError", "KEY_BYTES", "NONCE_BYTES",
    "Sealed", "new_master_key", "open_sealed", "rewrap", "seal",
]
