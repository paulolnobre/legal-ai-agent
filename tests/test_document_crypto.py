"""Encrypting a document so the bucket provider cannot read it.

Cryptography fails silently: a broken implementation still returns bytes, and
those bytes still decrypt on the happy path. So none of the tests below are
satisfied by a round trip working. They are about the four ways this kind of
module is usually wrong.

A nonce is reused, and GCM stops being encryption at all.
A ciphertext is not bound to its document, so objects are interchangeable.
A tampered ciphertext decrypts into something instead of failing.
A missing key is treated as an empty key, and the archive is written in clear.
"""
import pytest

from core import document_crypto as crypto

SHA = "9f2c" * 16


@pytest.fixture
def key():
    return crypto.new_master_key()


# ── the key itself ───────────────────────────────────────────────────────────


def test_a_generated_key_is_256_bits_of_hex():
    assert len(bytes.fromhex(crypto.new_master_key())) == crypto.KEY_BYTES


def test_two_generated_keys_differ():
    """A key generator that repeats itself is the worst defect here, and the
    cheapest to check."""
    assert len({crypto.new_master_key() for _ in range(64)}) == 64


def test_without_a_key_nothing_is_stored_in_clear(key):
    with pytest.raises(crypto.EncryptionError, match="DOC_MASTER_KEY is not set"):
        crypto.seal(b"peticao", master_key="", document_sha=SHA)


def test_the_missing_key_error_says_how_to_make_one_and_warns_about_losing_it():
    """A missing key and a malformed one fail the same way, so the message is
    what separates them. It has to carry the fix, and the warning, because
    generating a key without escrowing it is the mistake that costs the
    archive."""
    with pytest.raises(crypto.EncryptionError) as raised:
        crypto.seal(b"x", master_key=None, document_sha=SHA)

    assert "new_master_key" in str(raised.value)
    assert "outside this host" in str(raised.value)


def test_a_key_of_the_wrong_length_is_refused_rather_than_padded(key):
    short = "ab" * 16  # 16 bytes, not 32

    with pytest.raises(crypto.EncryptionError, match="32 bytes"):
        crypto.seal(b"x", master_key=short, document_sha=SHA)


def test_a_key_that_is_not_hex_is_told_apart_from_one_of_the_wrong_length():
    """Different mistakes, different fixes."""
    with pytest.raises(crypto.EncryptionError, match="hexadecimal"):
        crypto.seal(b"x", master_key="not a key at all", document_sha=SHA)


# ── the round trip, and the things it does not prove ─────────────────────────


def test_a_document_comes_back_exactly(key):
    original = b"Peticao inicial\n\x00\xff binary too"

    sealed = crypto.seal(original, master_key=key, document_sha=SHA)

    assert crypto.open_sealed(
        sealed.blob, sealed.wrapped_key, master_key=key, document_sha=SHA,
    ) == original


def test_the_plaintext_is_not_in_the_blob(key):
    sealed = crypto.seal(b"nome do cliente", master_key=key, document_sha=SHA)

    assert b"nome do cliente" not in sealed.blob


def test_the_data_key_is_not_in_the_blob_or_recoverable_from_it(key):
    """The wrapped key travels in the database, never beside the ciphertext.
    If it were in the blob, the bucket would hold both halves."""
    sealed = crypto.seal(b"x", master_key=key, document_sha=SHA)

    assert sealed.wrapped_key not in sealed.blob
    assert bytes.fromhex(key) not in sealed.blob


def test_an_empty_document_is_still_sealed(key):
    """A zero-byte file is a real thing to receive, and must not crash the
    ingestion path."""
    sealed = crypto.seal(b"", master_key=key, document_sha=SHA)

    assert crypto.open_sealed(
        sealed.blob, sealed.wrapped_key, master_key=key, document_sha=SHA,
    ) == b""


# ── nonce discipline ─────────────────────────────────────────────────────────


def test_sealing_the_same_bytes_twice_produces_different_ciphertext(key):
    """A repeated nonce under one key does not weaken GCM, it destroys it. The
    check that catches that early is that nothing is ever deterministic."""
    first = crypto.seal(b"identical", master_key=key, document_sha=SHA)
    second = crypto.seal(b"identical", master_key=key, document_sha=SHA)

    assert first.blob != second.blob
    assert first.wrapped_key != second.wrapped_key


def test_nonces_do_not_repeat_across_many_seals(key):
    nonces = {
        crypto.seal(b"x", master_key=key, document_sha=SHA).blob[:crypto.NONCE_BYTES]
        for _ in range(200)
    }

    assert len(nonces) == 200


def _data_key_inside(sealed, master_key, sha=SHA):
    """The data key the seal actually used.

    The tests below reach inside the envelope on purpose. Everything visible
    from outside, the round trip and the blob differing between seals, stays
    green when the data key is the master key itself or when the wrap nonce
    never changes. Both of those are catastrophic, and both survived a first
    pass of tests that only looked at the outside.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    kek = bytes.fromhex(master_key)
    head = 1 + crypto.NONCE_BYTES
    return AESGCM(kek).decrypt(
        sealed.wrapped_key[1:head], sealed.wrapped_key[head:], sha.encode(),
    )


def test_the_data_key_is_not_the_master_key(key):
    """If it were, every document in the archive would share one key, and that
    key would be the one in the environment. The envelope would be decoration
    and the round trip would still pass."""
    sealed = crypto.seal(b"x", master_key=key, document_sha=SHA)

    assert _data_key_inside(sealed, key) != bytes.fromhex(key)


def test_every_document_gets_its_own_data_key(key):
    first = crypto.seal(b"x", master_key=key, document_sha=SHA)
    second = crypto.seal(b"x", master_key=key, document_sha=SHA)

    assert _data_key_inside(first, key) != _data_key_inside(second, key)


def test_the_wrap_nonce_does_not_repeat_either(key):
    """The blob's nonce is not the only one. A fixed nonce here repeats under
    the master key, which is the one key used for every document, so it is the
    worse of the two places to get this wrong."""
    head = 1 + crypto.NONCE_BYTES
    nonces = {
        crypto.seal(b"x", master_key=key, document_sha=SHA).wrapped_key[1:head]
        for _ in range(200)
    }

    assert len(nonces) == 200


# ── the binding that makes objects not interchangeable ───────────────────────


def test_a_document_does_not_open_under_another_document_identity(key):
    """The attack this exists to stop: somebody holding the bucket moves one
    case file's bytes over another's. Without the binding, it decrypts and the
    lawyer reads the wrong client's document with no sign anything happened."""
    sealed = crypto.seal(b"contrato do cliente A", master_key=key, document_sha=SHA)

    with pytest.raises(crypto.EncryptionError):
        crypto.open_sealed(
            sealed.blob, sealed.wrapped_key, master_key=key, document_sha="0" * 64,
        )


def test_swapping_two_sealed_documents_is_caught(key):
    other_sha = "1234" * 16
    a = crypto.seal(b"documento A", master_key=key, document_sha=SHA)
    b = crypto.seal(b"documento B", master_key=key, document_sha=other_sha)

    with pytest.raises(crypto.EncryptionError):
        crypto.open_sealed(
            b.blob, a.wrapped_key, master_key=key, document_sha=SHA,
        )


def test_sealing_without_a_document_identity_is_refused(key):
    """An empty sha would bind every object to the same nothing, which is the
    same as not binding at all."""
    with pytest.raises(crypto.EncryptionError, match="sha256"):
        crypto.seal(b"x", master_key=key, document_sha="   ")


# ── tampering ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("position", [0, 5, 12, 20, -1])
def test_a_single_altered_byte_fails_instead_of_returning_rubbish(key, position):
    """Authenticated encryption's whole point. A stream cipher without the tag
    would hand back corrupted bytes and let them into the corpus."""
    sealed = crypto.seal(b"conteudo de um contrato longo", master_key=key,
                         document_sha=SHA)
    broken = bytearray(sealed.blob)
    broken[position] ^= 0x01

    with pytest.raises(crypto.EncryptionError, match="alterado"):
        crypto.open_sealed(bytes(broken), sealed.wrapped_key,
                           master_key=key, document_sha=SHA)


def test_an_altered_wrapped_key_fails(key):
    sealed = crypto.seal(b"x", master_key=key, document_sha=SHA)
    broken = bytearray(sealed.wrapped_key)
    broken[-1] ^= 0x01

    with pytest.raises(crypto.EncryptionError):
        crypto.open_sealed(sealed.blob, bytes(broken),
                           master_key=key, document_sha=SHA)


def test_another_master_key_does_not_open_it(key):
    sealed = crypto.seal(b"x", master_key=key, document_sha=SHA)

    with pytest.raises(crypto.EncryptionError):
        crypto.open_sealed(sealed.blob, sealed.wrapped_key,
                           master_key=crypto.new_master_key(), document_sha=SHA)


def test_a_truncated_blob_says_so_rather_than_raising_an_index_error(key):
    sealed = crypto.seal(b"x", master_key=key, document_sha=SHA)

    with pytest.raises(crypto.EncryptionError, match="incompleto"):
        crypto.open_sealed(b"short", sealed.wrapped_key,
                           master_key=key, document_sha=SHA)


def test_a_truncated_wrapped_key_says_so(key):
    sealed = crypto.seal(b"x", master_key=key, document_sha=SHA)

    with pytest.raises(crypto.EncryptionError, match="incompleto"):
        crypto.open_sealed(sealed.blob, b"\x01\x02",
                           master_key=key, document_sha=SHA)


# ── rotation, which is the reason for the envelope ───────────────────────────


def test_rotating_the_master_key_leaves_the_ciphertext_untouched(key):
    """The payoff: a rotation rewrites small rows in the database, not the
    archive in the bucket."""
    sealed = crypto.seal(b"contrato", master_key=key, document_sha=SHA)
    fresh = crypto.new_master_key()

    rewrapped = crypto.rewrap(sealed.wrapped_key, old_master_key=key,
                              new_master_key=fresh, document_sha=SHA)

    assert crypto.open_sealed(sealed.blob, rewrapped,
                              master_key=fresh, document_sha=SHA) == b"contrato"


def test_after_rotation_the_old_key_no_longer_opens_the_document(key):
    sealed = crypto.seal(b"contrato", master_key=key, document_sha=SHA)
    fresh = crypto.new_master_key()
    rewrapped = crypto.rewrap(sealed.wrapped_key, old_master_key=key,
                              new_master_key=fresh, document_sha=SHA)

    with pytest.raises(crypto.EncryptionError):
        crypto.open_sealed(sealed.blob, rewrapped, master_key=key, document_sha=SHA)


def test_rewrapping_with_the_wrong_old_key_is_refused(key):
    sealed = crypto.seal(b"x", master_key=key, document_sha=SHA)

    with pytest.raises(crypto.EncryptionError, match="antiga"):
        crypto.rewrap(sealed.wrapped_key, old_master_key=crypto.new_master_key(),
                      new_master_key=crypto.new_master_key(), document_sha=SHA)


def test_a_wrapped_key_carries_the_version_that_made_it(key):
    """Without it a rotation is all-or-nothing, with no way to resume after
    failing halfway through."""
    sealed = crypto.seal(b"x", master_key=key, document_sha=SHA)

    assert sealed.wrapped_key[0] == crypto.CURRENT_KEK_VERSION
    assert sealed.kek_version == crypto.CURRENT_KEK_VERSION


def test_a_document_sealed_by_a_future_version_is_named_not_guessed(key):
    """The message tells the operator which key they need, because "could not
    decrypt" during a half-finished rotation sends somebody hunting a bug that
    is not there."""
    sealed = crypto.seal(b"x", master_key=key, document_sha=SHA)
    from_future = bytes([crypto.CURRENT_KEK_VERSION + 1]) + sealed.wrapped_key[1:]

    with pytest.raises(crypto.EncryptionError, match="versão"):
        crypto.open_sealed(sealed.blob, from_future, master_key=key, document_sha=SHA)


# ── crypto-shredding, which is what erasure rests on ─────────────────────────


def test_losing_the_wrapped_key_makes_the_object_unrecoverable(key):
    """This is the LGPD argument in one test. Deleting the document's row
    destroys the only copy of its data key, so the object left in the bucket,
    in an old version, or in an immutable backup is noise. Erasure stops being
    "reach every copy" and becomes "delete one row"."""
    sealed = crypto.seal(b"dados do titular", master_key=key, document_sha=SHA)

    # The row is gone; the blob survives in the bucket. Even the master key,
    # which we still hold, does not open it.
    with pytest.raises(crypto.EncryptionError):
        crypto.open_sealed(sealed.blob, b"\x01" + b"\x00" * 60,
                           master_key=key, document_sha=SHA)
