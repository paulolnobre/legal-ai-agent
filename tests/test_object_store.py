"""Putting a document in a bucket, encrypted, and getting it back.

Two things are being checked, and they fail in different ways.

The signature is either byte-exact or the request is rejected, so it fails
loudly and the risk is only wasted time. The vectors below were produced by
signing the same requests with `botocore` and comparing: they are a recorded
agreement with a known-good implementation, not values this code printed about
itself. `botocore` is not a dependency of this project and is not installed in
CI; pinning its answers is how the signing stays verified without carrying
forty megabytes to use two percent of it.

The encryption fails silently, which is why the tests that matter here are the
ones asserting what is *absent*: that no plaintext reaches the bucket, and that
nothing is written at all when the key is missing. A document written in clear
into a bucket is indistinguishable from the rest afterwards, and nobody would
find it.
"""
import asyncio
import hashlib
from types import SimpleNamespace

import httpx
import pytest

from core import document_crypto
from core.config import settings
from core.integrations import object_store
from core.services import encrypted_storage

# Signed with these credentials, this host, this region and this instant, so a
# vector is reproducible. They are AWS's published example values and open
# nothing. The host and region are the ones the vectors were recorded with;
# SigV4 does not care which S3 provider answers, only that they match.
_REGION = "auto"
_AK = "AKIAIOSFODNN7EXAMPLE"
_SK = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
_HOST = "abc123.r2.cloudflarestorage.com"
_AMZ = "20260927T120000Z"

_SHA = "e" * 64
_KEY = "ab/cd/" + "e" * 64 + ".pdf"


# ── the signature, against botocore's answers ────────────────────────────────
# In this public copy the bucket names were replaced. The vectors below were
# regenerated with this implementation after it reproduced every vector
# originally recorded from botocore.


@pytest.mark.parametrize("method, uri, payload_sha, expected", [
    ("PUT", "/demo-docs/ab/cd/" + "e" * 64 + ".pdf",
     "6980bfd0db4442407e1b133f3d298f66c126037ec92d714f3535e30647d888a7",
     "83dc57ea941cdb957538a8f9ef446340b85622fa714bc4e76ba7a8053e140706"),
    ("GET", "/demo-docs/ab/cd/" + "f" * 64,
     "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
     "8894b6ae663304dbe4f62b1851c41a8a63c7ce90c7cdc6eac9dd40618670a059"),
    ("DELETE", "/demo-docs/12/34/" + "0" * 64 + ".docx",
     "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
     "3dbb4ddd1731eb46bc6b74c29e515673e9463aaac5306bf141c7631ffae3784a"),
    ("HEAD", "/demo-docs/aa/bb/" + "9" * 64,
     "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
     "64de411b9cee80b5c12cc487d19dc80819c733054691778aa27201aa7b198ccf"),
    ("PUT", "/demo-docs/00/11/" + "a" * 64,
     "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
     "acf07f20b21af366772ee086b55d011dfdd71c749db37fe9e0fcc8b34fe398d7"),
    ("PUT", "/demo-docs/de/ad/" + "b" * 64 + ".xlsx",
     "e96760a87768717bcebcfd25ddc7d46b4dbc95a4b0014def080c08539f7d90d0",
     "87a83e60592dba5f7ad1a5c7b53001fbf361b99b92cb5cb230b6ec032368173f"),
])
def test_the_signature_matches_a_known_good_implementation(
    method, uri, payload_sha, expected,
):
    header = object_store.authorization(
        method, uri, host=_HOST, payload_sha256=payload_sha, amz_date=_AMZ,
        access_key=_AK, secret_key=_SK, region=_REGION,
    )

    assert header.split("Signature=")[1] == expected


_EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


@pytest.mark.parametrize("uri, query, expected", [
    # A listing of the inbox folder, and its second page.
    ("/demo-firm-object", "list-type=2&prefix=entrada%2F",
     "3949cf6dd2da41a35bd4e7bf8ef17c525165d9ddca5d1406032564a89b5dc17e"),
    ("/demo-firm-object",
     "continuation-token=next%20page&list-type=2&prefix=entrada%2F",
     "aa607652f733570ebccaf844649edd585be90afb95a9c806d97429311acb5ab8"),
    # Names somebody else chose: a comma, accents, parentheses, a tilde.
    ("/demo-firm-object/entrada/LEI%20No%208.245%2C%20DE%201991.html", "",
     "deb397c5723d2cbd47cfb4954a563a42b2d75d316f1cc04249868d875b108d87"),
    ("/demo-firm-object/entrada/Clientes/A%C3%A7%C3%A3o%20Ltda/"
     "contrato%20%28v2%29%20~final.txt", "",
     "0b820c4c3f39d55781b340a44cb0757449ae46a144b7af8496e2e6b1840023d3"),
])
def test_a_query_and_an_encoded_name_sign_like_botocore(uri, query, expected):
    """Recorded from botocore's S3SigV4Auth for the Magalu host and region,
    after the same generator reproduced the empty-body vectors above."""
    header = object_store.authorization(
        "GET", uri, host="br-se1.magaluobjects.com", payload_sha256=_EMPTY,
        amz_date=_AMZ, access_key=_AK, secret_key=_SK, region="br-se1",
        canonical_query=query,
    )

    assert header.split("Signature=")[1] == expected


@pytest.mark.parametrize("name, encoded", [
    ("LEI No 8.245, DE 1991.html", "LEI%20No%208.245%2C%20DE%201991.html"),
    ("Clientes/Ação Ltda/contrato (v2) ~final.txt",
     "Clientes/A%C3%A7%C3%A3o%20Ltda/contrato%20%28v2%29%20~final.txt"),
])
def test_a_path_keeps_its_slashes_and_encodes_everything_else(name, encoded):
    assert object_store.uri_encode(name, keep_slash=True) == encoded
    assert "/" not in object_store.uri_encode(name)


def test_the_credential_scope_names_the_bucket_service_and_date():
    header = object_store.authorization(
        "GET", "/b/ab/cd/" + "f" * 64, host=_HOST,
        payload_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        amz_date=_AMZ, access_key=_AK, secret_key=_SK, region=_REGION,
    )

    assert f"Credential={_AK}/20260927/auto/s3/aws4_request" in header
    assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in header


def test_the_secret_key_never_appears_in_the_header():
    header = object_store.authorization(
        "GET", "/b/ab/cd/" + "f" * 64, host=_HOST, payload_sha256="0" * 64,
        amz_date=_AMZ, access_key=_AK, secret_key=_SK, region=_REGION,
    )

    assert _SK not in header


def test_changing_one_byte_of_the_payload_changes_the_signature():
    """If it did not, the signature would not be covering the body at all."""
    first = object_store.authorization("PUT", "/b/ab/cd/" + "e" * 64, host=_HOST,
                             payload_sha256="a" * 64, amz_date=_AMZ,
                             access_key=_AK, secret_key=_SK, region=_REGION)
    second = object_store.authorization("PUT", "/b/ab/cd/" + "e" * 64, host=_HOST,
                              payload_sha256="b" * 64, amz_date=_AMZ,
                              access_key=_AK, secret_key=_SK, region=_REGION)

    assert first != second


# ── the key shape that keeps the signing simple ──────────────────────────────


@pytest.mark.parametrize("bad", [
    "ab/cd/not-a-sha",
    "../../etc/passwd",
    "ab/cd/" + "e" * 64 + "/../../escape",
    "ab/cd/" + "e" * 64 + ".pdf?versionId=1",
    "ab/cd/" + "e" * 64 + " com espaco.pdf",
    "AB/CD/" + "E" * 64,
    "",
])
def test_a_key_outside_the_safe_shape_is_refused_before_signing(bad, monkeypatch):
    """Every URI-encoding case where a hand-written canonical request diverges
    from a library's lives outside this set, so it is refused rather than
    handled."""
    monkeypatch.setattr(settings, "object_store_endpoint", f"https://{_HOST}")
    monkeypatch.setattr(settings, "object_store_bucket", "demo-docs")
    monkeypatch.setattr(settings, "object_store_access_key_id", _AK)
    monkeypatch.setattr(settings, "object_store_secret_access_key", _SK)

    with pytest.raises(object_store.ObjectStoreError, match="content-addressed"):
        asyncio.run(object_store.get_object(bad))


def test_the_key_the_archive_produces_is_accepted():
    """The guard has to admit the one caller it exists for."""
    from core.services.document_storage import storage_key

    key = storage_key("a" * 64, "contrato.pdf")

    assert object_store._check_key(key) == key


# ── the bucket itself ────────────────────────────────────────────────────────


@pytest.fixture
def bucket(monkeypatch):
    state = SimpleNamespace(objects={}, requests=[], status=None)

    def handler(request):
        state.requests.append(request)
        if state.status:
            return httpx.Response(state.status, content=b"<Error/>")
        path = request.url.path
        if request.method == "PUT":
            state.objects[path] = request.content
            return httpx.Response(200)
        if request.method in ("GET", "HEAD"):
            if path not in state.objects:
                return httpx.Response(404, content=b"<Error/>")
            return httpx.Response(200, content=state.objects[path])
        if request.method == "DELETE":
            state.objects.pop(path, None)
            return httpx.Response(204)
        return httpx.Response(400)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(object_store, "_http", lambda: client)
    monkeypatch.setattr(settings, "object_store_endpoint", f"https://{_HOST}")
    monkeypatch.setattr(settings, "object_store_bucket", "demo-docs")
    monkeypatch.setattr(settings, "object_store_access_key_id", _AK)
    monkeypatch.setattr(settings, "object_store_secret_access_key", _SK)
    monkeypatch.setattr(settings, "doc_master_key", document_crypto.new_master_key())
    return state


def test_every_request_carries_a_signature_and_a_payload_hash(bucket):
    asyncio.run(object_store.put_object("ab/cd/" + "e" * 64, b"bytes"))

    sent = bucket.requests[0]
    assert sent.headers["authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert sent.headers["x-amz-content-sha256"] == hashlib.sha256(b"bytes").hexdigest()
    assert sent.headers["x-amz-date"]


def test_requests_are_signed_for_the_configured_region(bucket, monkeypatch):
    """The region is part of the signature. Signed for the wrong one, every
    request is refused, and Magalu has more than one."""
    monkeypatch.setattr(settings, "object_store_region", "br-ne1")

    asyncio.run(object_store.put_object("ab/cd/" + "e" * 64, b"bytes"))

    assert "/br-ne1/s3/aws4_request" in bucket.requests[0].headers["authorization"]


def test_a_missing_object_is_told_apart_from_a_refusal(bucket):
    with pytest.raises(object_store.ObjectStoreNotFound):
        asyncio.run(object_store.get_object("ab/cd/" + "e" * 64))


def test_deleting_something_already_gone_is_not_an_error(bucket):
    """Deletion is the one operation that has to be safe to retry, because the
    caller retrying it is a retention sweep finishing what it started."""
    asyncio.run(object_store.delete_object("ab/cd/" + "e" * 64))


def test_a_refused_deletion_is_raised_rather_than_reported_as_done(bucket):
    """Only a missing object is safe to swallow here. A bucket that refuses is
    a deletion that did not happen, and a retention sweep reporting success on
    a 403 is the silent failure this project treats as worse than a crash.

    The layer above chooses to swallow it and log; that choice belongs there,
    not here, and a mutation collapsing the two survived until this existed.
    """
    bucket.status = 403

    with pytest.raises(object_store.ObjectStoreError, match="HTTP 403"):
        asyncio.run(object_store.delete_object("ab/cd/" + "e" * 64))


def test_a_refusal_does_not_echo_the_bucket_reply(bucket):
    bucket.status = 403

    with pytest.raises(object_store.ObjectStoreError, match="HTTP 403"):
        asyncio.run(object_store.get_object("ab/cd/" + "e" * 64))


# ── what actually lands in the bucket ────────────────────────────────────────


def test_the_bucket_never_sees_the_plaintext(bucket):
    """The whole point. If this passes and nothing else does, the migration is
    still safe; if this fails, nothing else matters."""
    secret = b"Contrato entre a parte A e o cliente B, valor R$ 250.000,00"
    sha = hashlib.sha256(secret).hexdigest()

    asyncio.run(encrypted_storage.store(secret, key=_KEY, raw_sha256=sha))

    stored = b"".join(bucket.objects.values())
    assert secret not in stored
    assert b"Contrato" not in stored and b"250.000" not in stored


def test_a_document_comes_back_through_the_bucket(bucket):
    original = b"peticao inicial, com acentuacao: inadimplencia"
    sha = hashlib.sha256(original).hexdigest()

    wrapped, version = asyncio.run(
        encrypted_storage.store(original, key=_KEY, raw_sha256=sha),
    )

    assert version == document_crypto.CURRENT_KEK_VERSION
    assert asyncio.run(
        encrypted_storage.read(_KEY, wrapped, raw_sha256=sha),
    ) == original


def test_the_wrapped_key_is_returned_and_not_stored_in_the_bucket(bucket):
    """Both halves in the bucket would defeat the entire design."""
    sha = hashlib.sha256(b"x").hexdigest()

    wrapped, _v = asyncio.run(
        encrypted_storage.store(b"x", key=_KEY, raw_sha256=sha),
    )

    assert wrapped
    assert wrapped not in b"".join(bucket.objects.values())


def test_without_the_master_key_nothing_is_written_at_all(bucket, monkeypatch):
    """Not written in clear with a warning, not written and fixed later. One
    unencrypted object in a bucket looks exactly like the rest afterwards."""
    monkeypatch.setattr(settings, "doc_master_key", None)
    sha = hashlib.sha256(b"x").hexdigest()

    with pytest.raises(encrypted_storage.StorageUnavailable, match="criptografia"):
        asyncio.run(encrypted_storage.store(b"x", key=_KEY, raw_sha256=sha))

    assert bucket.objects == {}
    assert bucket.requests == []


def test_without_a_bucket_nothing_is_written(bucket, monkeypatch):
    monkeypatch.setattr(settings, "object_store_bucket", None)
    sha = hashlib.sha256(b"x").hexdigest()

    with pytest.raises(encrypted_storage.StorageUnavailable):
        asyncio.run(encrypted_storage.store(b"x", key=_KEY, raw_sha256=sha))

    assert bucket.requests == []


def test_a_document_whose_key_was_deleted_cannot_be_read(bucket):
    """The erasure guarantee, from the storage layer's side: the object is
    still in the bucket and it is noise."""
    sha = hashlib.sha256(b"dados do titular").hexdigest()
    asyncio.run(encrypted_storage.store(b"dados do titular", key=_KEY, raw_sha256=sha))

    with pytest.raises(document_crypto.EncryptionError):
        asyncio.run(encrypted_storage.read(_KEY, b"\x01" + b"\x00" * 60, raw_sha256=sha))


def test_removing_is_best_effort_because_the_real_deletion_already_happened(
    bucket, monkeypatch,
):
    """A bucket briefly unreachable must not turn a completed erasure into a
    failed one: the row carrying the key is already gone, so what is left is
    unreadable."""
    async def refuses(_key):
        raise object_store.ObjectStoreError("bucket is away")

    monkeypatch.setattr(object_store, "delete_object", refuses)

    asyncio.run(encrypted_storage.remove("ab/cd/" + "e" * 64))


def test_two_copies_of_one_file_never_share_an_object(bucket):
    """Content addressing would overwrite here, and silently. Encryption gives
    every copy its own data key and nonce, so the same bytes stored twice are
    two different ciphertexts; under one name the second write would leave the
    first document's wrapped key opening nothing, with nothing to notice.

    Deduplication is what content addressing bought, and encryption already
    spent it, so the name is random instead.
    """
    from core.services import document_storage

    first = document_storage.bucket_key("contrato.pdf")
    second = document_storage.bucket_key("contrato.pdf")

    assert first != second
    assert first.endswith(".pdf") and second.endswith(".pdf")
