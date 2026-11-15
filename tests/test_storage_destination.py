"""Where an original goes, and the one place that decides.

Turning the bucket on is the riskiest kind of change: it is invisible from the
outside. A document ingested after the switch looks exactly like one ingested
before, right up to the day somebody asks for the file back. So these tests
are about the decisions that would fail silently.

Old documents keep being readable. New ones go to the bucket. Nothing
half-configured writes anywhere. And two copies of one file never land on one
object, which is the defect content addressing would have introduced here the
moment encryption made every copy's ciphertext different.
"""
import asyncio
from types import SimpleNamespace

import pytest

from core.config import settings
from core.services import document_storage, encrypted_storage


@pytest.fixture
def volume(tmp_path, monkeypatch):
    """No bucket, no key: this deployment keeps originals on local disk."""
    monkeypatch.setattr(settings, "document_storage_path", str(tmp_path))
    monkeypatch.setattr(settings, "object_store_endpoint", None)
    monkeypatch.setattr(settings, "object_store_bucket", None)
    monkeypatch.setattr(settings, "object_store_access_key_id", None)
    monkeypatch.setattr(settings, "object_store_secret_access_key", None)
    monkeypatch.setattr(settings, "doc_master_key", None)


@pytest.fixture
def bucket(monkeypatch):
    """A configured bucket and key: originals are encrypted and go out."""
    state = SimpleNamespace(put={}, removed=[])

    async def store(data, *, key, raw_sha256):
        state.put[key] = (data, raw_sha256)
        return b"\x01wrapped-key-bytes", 1

    async def read(key, wrapped_key, *, raw_sha256):
        return state.put[key][0]

    async def remove(key):
        state.removed.append(key)

    monkeypatch.setattr(encrypted_storage, "configured", lambda: True)
    monkeypatch.setattr(document_storage.encrypted_storage, "store", store)
    monkeypatch.setattr(document_storage.encrypted_storage, "read", read)
    monkeypatch.setattr(document_storage.encrypted_storage, "remove", remove)
    return state


# ── which store is used ──────────────────────────────────────────────────────


def test_without_a_bucket_an_original_stays_on_this_host(volume):
    stored = asyncio.run(
        document_storage.store(b"conteudo", raw_sha256="a" * 64, filename="c.pdf"),
    )

    assert not stored.in_bucket
    assert stored.wrapped_key == b""


def test_with_a_bucket_an_original_leaves_encrypted(volume, bucket):
    stored = asyncio.run(
        document_storage.store(b"conteudo", raw_sha256="a" * 64, filename="c.pdf"),
    )

    assert stored.in_bucket
    assert stored.wrapped_key and stored.kek_version == 1
    assert bucket.put[stored.key][0] == b"conteudo"


def test_a_document_says_where_it_lives_rather_than_a_setting_saying_it(
    volume, bucket,
):
    """Turning the bucket on must not strand what is already on disk. An old
    document has no wrapped key, so it is read from the volume even now, and
    no migration has to run before the feature works."""
    # Written before the bucket existed: a key, and no wrapped key.
    key, _created = document_storage.store_original(
        b"antigo", raw_sha256="b" * 64, filename="v.pdf",
    )

    # The bucket is configured now, and this still comes off the disk.
    assert asyncio.run(document_storage.read(key)) == b"antigo"


def test_a_new_document_is_read_back_from_the_bucket(volume, bucket):
    stored = asyncio.run(
        document_storage.store(b"novo", raw_sha256="c" * 64, filename="n.pdf"),
    )

    assert asyncio.run(
        document_storage.read(stored.key, stored.wrapped_key, raw_sha256="c" * 64),
    ) == b"novo"


def test_removal_follows_the_document_to_wherever_it_is(volume, bucket):
    stored = asyncio.run(
        document_storage.store(b"x", raw_sha256="d" * 64, filename="x.pdf"),
    )

    asyncio.run(document_storage.remove(stored.key, stored.wrapped_key))

    assert bucket.removed == [stored.key]


# ── the half-configured case, which is the dangerous one ─────────────────────


def test_a_bucket_without_a_key_refuses_rather_than_writing_to_disk(
    volume, monkeypatch,
):
    """The silent failure this prevents: an operator sets up a bucket, forgets
    the key, and documents keep being accepted onto local disk in clear while
    the firm believes they are encrypted and off the host."""
    monkeypatch.setattr(settings, "object_store_endpoint", "https://br-se1.magaluobjects.com")
    monkeypatch.setattr(settings, "object_store_bucket", "docs")
    monkeypatch.setattr(settings, "object_store_access_key_id", "k")
    monkeypatch.setattr(settings, "object_store_secret_access_key", "s")

    with pytest.raises(encrypted_storage.StorageUnavailable, match="chave"):
        asyncio.run(
            document_storage.store(b"x", raw_sha256="e" * 64, filename="x.pdf"),
        )


def test_a_key_without_a_bucket_also_refuses(volume, monkeypatch):
    """The mirror image, and just as wrong: half a configuration is not a
    deployment, it is a mistake in progress."""
    monkeypatch.setattr(settings, "doc_master_key", "ab" * 32)

    with pytest.raises(encrypted_storage.StorageUnavailable):
        asyncio.run(
            document_storage.store(b"x", raw_sha256="f" * 64, filename="x.pdf"),
        )


# ── the name in the bucket ───────────────────────────────────────────────────


def test_two_copies_of_one_file_never_land_on_one_object(volume, bucket):
    """Encryption gives every copy its own data key and nonce, so the same
    bytes stored twice are two different ciphertexts. Under a content-addressed
    name the second write would overwrite the first, and the first document's
    wrapped key would open nothing from that moment, with nothing to notice.
    """
    first = asyncio.run(
        document_storage.store(b"mesmo", raw_sha256="a" * 64, filename="c.pdf"))
    second = asyncio.run(
        document_storage.store(b"mesmo", raw_sha256="a" * 64, filename="c.pdf"))

    assert first.key != second.key
    assert len(bucket.put) == 2, "neither write destroyed the other"


@pytest.mark.parametrize("filename, kept", [
    ("contrato.pdf", ".pdf"),
    ("planilha.XLSX", ".xlsx"),
    ("sem-extensao", ""),
    # Below here the suffix is non-empty and not a safe extension. These are
    # the cases the guard exists for, and a first version of this test used
    # only names with no suffix at all, so removing the guard changed nothing
    # and the mutation survived.
    ("arquivo.a-b", ""),
    ("arquivo.verylongextension", ""),
    ("arquivo.pdf exe", ""),
    ("arquivo.pdf/../../etc", ""),
])
def test_the_bucket_name_keeps_a_safe_extension_and_drops_anything_else(
    filename, kept,
):
    """An unsafe extension left on would produce a name the object storage client refuses
    to sign, so the document would simply never be stored."""
    key = document_storage.bucket_key(filename)

    assert key.endswith(kept)
    if not kept:
        assert "." not in key


def test_the_bucket_name_is_a_shape_the_client_will_sign():
    """The object storage client refuses anything outside the content-addressed shape, so
    a name this builder produces has to be inside it or nothing is ever
    stored."""
    from core.integrations import object_store

    key = document_storage.bucket_key("peticao.pdf")

    assert object_store._check_key(key) == key


def test_the_local_key_is_still_content_addressed(volume):
    """Deduplication still pays on disk, where the same bytes really are one
    file. It is only in the bucket that encryption spent it."""
    first, _ = document_storage.store_original(
        b"mesmo", raw_sha256="a" * 64, filename="c.pdf")
    second, created = document_storage.store_original(
        b"mesmo", raw_sha256="a" * 64, filename="c.pdf")

    assert first == second and created is False


def test_an_exclusive_original_has_a_file_of_its_own_on_the_volume(volume, tmp_path):
    """An original stored before the database decides whether a document will
    hold it gets a fresh name, so taking it back never removes a file that
    another document, stored with the same bytes, holds."""
    first = asyncio.run(document_storage.store(
        b"mesmo", raw_sha256="a" * 64, filename="c.pdf", exclusive=True))
    second = asyncio.run(document_storage.store(
        b"mesmo", raw_sha256="a" * 64, filename="c.pdf", exclusive=True))

    assert first.key != second.key and first.newly_created and second.newly_created
    assert not first.in_bucket and first.key.endswith(".pdf")
    asyncio.run(document_storage.remove(first.key))
    assert document_storage.read_original(second.key) == b"mesmo"
    assert not (tmp_path / first.key).exists()
