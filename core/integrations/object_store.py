"""Magalu Cloud Object Storage, over the S3 API, for the documents the firm keeps.

It speaks S3, which means every request carries an AWS SigV4 signature. That
signature is implemented here rather than pulled in with `boto3`, and the
reasoning is worth stating because "just use the SDK" is usually right.

`botocore` is around forty megabytes in a production image to use perhaps two
percent of it, and it is synchronous, so every call would have to be pushed
onto a thread from a codebase that is async all the way down. Against that,
SigV4 is a fully specified algorithm whose fiddly parts are URI encoding and
header canonicalisation.

**Those fiddly parts are removed rather than handled.** Object keys here are
constrained to hexadecimal, slashes and a lowercase extension, which is what
`document_storage.storage_key` already produces. Anything else is refused
before it is signed. A key with a space, an accent or a query character is the
case where a hand-written canonical URI diverges from a library's, and it
cannot occur because it is rejected at the door.

The signing was checked against `botocore` rather than assumed, with the
vectors kept in the tests.

Nothing here knows about encryption. This module moves bytes; whether those
bytes are ciphertext is decided by the service above it, and the separation is
deliberate so that "did it encrypt?" is answerable in one place.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from core.config import settings
from core.metrics import observe_external
from core.observability import traced

logger = logging.getLogger(__name__)

SERVICE = "s3"
ALGORITHM = "AWS4-HMAC-SHA256"

# The only shape of key this module will sign. Hexadecimal, slashes, and a
# short lowercase extension: exactly what the content-addressed key builder
# produces. Every URI-encoding edge case lives outside this set.
_SAFE_KEY = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}(\.[a-z0-9]{1,10})?$")

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

_client: httpx.AsyncClient | None = None


class ObjectStoreError(RuntimeError):
    """A failure worth showing somebody. Never carries a credential."""


class ObjectStoreNotFound(ObjectStoreError):
    """The object is not there. Distinct because deleting a gone object is fine."""


def credentials_configured() -> bool:
    """Whether there is an endpoint and a key to sign with, whatever the bucket."""
    return all((
        (settings.object_store_endpoint or "").strip(),
        (settings.object_store_access_key_id or "").strip(),
        (settings.object_store_secret_access_key or "").strip(),
    ))


def configured() -> bool:
    """Whether there is a bucket for the documents to talk to."""
    return credentials_configured() and bool((settings.object_store_bucket or "").strip())


def uri_encode(value: str, *, keep_slash: bool = False) -> str:
    """SigV4's encoding: every byte but A-Z a-z 0-9 - _ . ~ as %XX, in UTF-8.

    `quote` already treats exactly those as safe; the slash is the only choice
    left, kept in an object path and encoded everywhere else.
    """
    return quote(value, safe="/" if keep_slash else "")


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0))
    return _client


def _check_key(key: str) -> str:
    """Refuse anything the canonical URI here is not written to handle."""
    if not _SAFE_KEY.fullmatch(key or ""):
        raise ObjectStoreError(
            "This object key is not one this client will sign. Keys must be "
            "the content-addressed form 'ab/cd/<sha256>[.ext]'."
        )
    return key


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def _signing_key(secret: str, datestamp: str, region: str) -> bytes:
    """The SigV4 key chain: date, region, service, then the terminator."""
    key = _hmac(f"AWS4{secret}".encode(), datestamp)
    key = _hmac(key, region)
    key = _hmac(key, SERVICE)
    return _hmac(key, "aws4_request")


def authorization(method: str, canonical_uri: str, *, host: str,
                  payload_sha256: str, amz_date: str,
                  access_key: str, secret_key: str, region: str,
                  canonical_query: str = "") -> str:
    """The Authorization header for one request.

    Split out from the request itself so the signing can be tested against a
    known-good implementation without a network call, which is the only way to
    know an algorithm like this is right rather than merely plausible.
    """
    datestamp = amz_date[:8]
    scope = f"{datestamp}/{region}/{SERVICE}/aws4_request"
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_sha256}\n"
        f"x-amz-date:{amz_date}\n"
    )
    canonical_request = "\n".join([
        method, canonical_uri, canonical_query, canonical_headers, signed_headers,
        payload_sha256,
    ])
    string_to_sign = "\n".join([
        ALGORITHM, amz_date, scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])
    signature = hmac.new(
        _signing_key(secret_key, datestamp, region), string_to_sign.encode(), hashlib.sha256,
    ).hexdigest()
    return (
        f"{ALGORITHM} Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


async def _send(method: str, canonical_uri: str, *, query: dict[str, str] | None = None,
                body: bytes | None = None):
    """Sign and send one request. `canonical_uri` is already URI-encoded.

    The same string is signed and sent, so the two cannot disagree about how a
    name was encoded, which is the one way a hand-written SigV4 goes wrong.
    """
    if not credentials_configured():
        raise ObjectStoreError("Object storage is not configured on this server.")
    endpoint = (settings.object_store_endpoint or "").strip().rstrip("/")
    host = endpoint.split("://", 1)[-1]
    canonical_query = "&".join(
        f"{uri_encode(k)}={uri_encode(v)}" for k, v in sorted((query or {}).items())
    )
    payload = body if body is not None else b""
    payload_sha256 = hashlib.sha256(payload).hexdigest() if body is not None else _EMPTY_SHA256
    amz_date = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")

    headers = {
        "Host": host,
        "x-amz-content-sha256": payload_sha256,
        "x-amz-date": amz_date,
        "Authorization": authorization(
            method, canonical_uri, host=host, payload_sha256=payload_sha256,
            amz_date=amz_date,
            access_key=(settings.object_store_access_key_id or "").strip(),
            secret_key=(settings.object_store_secret_access_key or "").strip(),
            region=(settings.object_store_region or "").strip(),
            canonical_query=canonical_query,
        ),
    }
    url = f"{endpoint}{canonical_uri}" + (f"?{canonical_query}" if canonical_query else "")
    started = time.perf_counter()
    try:
        response = await _http().request(method, url, headers=headers, content=body)
    except httpx.HTTPError as exc:
        observe_external("object_store", "error", time.perf_counter() - started)
        raise ObjectStoreError("O armazenamento de documentos não respondeu.") from exc

    if response.status_code == 404:
        observe_external("object_store", "not_found", time.perf_counter() - started)
        raise ObjectStoreNotFound("Esse documento não está no armazenamento.")
    if response.status_code >= 400:
        observe_external("object_store", "error", time.perf_counter() - started)
        # The body is the bucket's XML error. It never contains our credential,
        # but it is bounded anyway rather than passed through whole.
        logger.error("Object storage refused a %s: HTTP %s", method, response.status_code)
        raise ObjectStoreError(
            f"O armazenamento recusou a operação (HTTP {response.status_code})."
        )
    observe_external("object_store", "success", time.perf_counter() - started)
    return response


async def _request(method: str, key: str, *, body: bytes | None = None):
    """A request for one of the firm's encrypted originals, by its own key."""
    if not configured():
        raise ObjectStoreError("Object storage is not configured on this server.")
    safe_key = _check_key(key)
    return await _send(method, f"/{settings.object_store_bucket}/{safe_key}", body=body)


@traced(name="object_store.put", run_type="tool")
async def put_object(key: str, data: bytes) -> None:
    """Write one object. Overwrites, because the key is the content's hash."""
    await _request("PUT", key, body=data)
    logger.info("Stored %s bytes in object storage", len(data))


@traced(name="object_store.get", run_type="tool")
async def get_object(key: str) -> bytes:
    """Read one object, or raise ObjectStoreNotFound."""
    response = await _request("GET", key)
    return response.content


async def delete_object(key: str) -> None:
    """Remove one object.

    An object that is already gone is not an error: deletion is the one
    operation that has to be safe to retry, because the caller retrying it is
    usually a retention sweep finishing a job it started.
    """
    try:
        await _request("DELETE", key)
    except ObjectStoreNotFound:
        return


async def exists(key: str) -> bool:
    try:
        await _request("HEAD", key)
    except ObjectStoreNotFound:
        return False
    return True


# ── Reading a bucket other people write to ───────────────────────────────────
# The firm's inbox lives in the same kind of bucket, but its names are chosen
# by people: spaces, accents, commas. Those are signed with the full SigV4
# encoding instead of being refused, and the key-shape guard above stays on the
# documents, whose names this code chooses.

_BUCKET_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
# A synchronised folder can be large; this bounds one listing, not the import,
# which stops far earlier at its own per-run limit.
_MAX_LIST_PAGES = 10


@dataclass(frozen=True)
class StoredObject:
    """One object as a listing describes it."""

    key: str
    size: int
    etag: str
    last_modified: dt.datetime | None


def _check_bucket(bucket: str) -> str:
    if not _BUCKET_NAME.fullmatch(bucket or ""):
        raise ObjectStoreError("Nome de balde inválido.")
    return bucket


def _check_name(key: str) -> str:
    """A name somebody else chose: anything printable, bounded like S3 bounds it."""
    if not key or len(key.encode()) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in key):
        raise ObjectStoreError("Nome de arquivo inválido no balde.")
    return key


def _parse_time(value: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


async def list_objects(bucket: str, prefix: str = "") -> list[StoredObject]:
    """Every object under `prefix`, following continuation up to a bound.

    Folder placeholders (a key ending in "/") are left out: they have no
    content and nothing to read.
    """
    _check_bucket(bucket)
    objects: list[StoredObject] = []
    token = None
    for _page in range(_MAX_LIST_PAGES):
        query = {"list-type": "2", "prefix": prefix}
        if token:
            query["continuation-token"] = token
        response = await _send("GET", f"/{bucket}", query=query)
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise ObjectStoreError("O armazenamento devolveu uma listagem inválida.") from exc
        for item in root.iter(f"{_S3_NS}Contents"):
            key = item.findtext(f"{_S3_NS}Key") or ""
            if not key or key.endswith("/"):
                continue
            objects.append(StoredObject(
                key=key,
                size=int(item.findtext(f"{_S3_NS}Size") or 0),
                etag=(item.findtext(f"{_S3_NS}ETag") or "").strip('"'),
                last_modified=_parse_time(item.findtext(f"{_S3_NS}LastModified")),
            ))
        if (root.findtext(f"{_S3_NS}IsTruncated") or "").lower() != "true":
            break
        token = root.findtext(f"{_S3_NS}NextContinuationToken")
        if not token:
            break
    return objects


async def get_named(bucket: str, key: str, *, max_bytes: int) -> bytes:
    """One object by a name somebody else chose, refused rather than truncated
    when it is larger than `max_bytes`."""
    _check_bucket(bucket)
    uri = f"/{bucket}/{uri_encode(_check_name(key), keep_slash=True)}"
    response = await _send("GET", uri)
    if len(response.content) > max_bytes:
        raise ObjectStoreError(f"O arquivo passa do limite de {max_bytes} bytes.")
    return response.content


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


__all__ = [
    "ALGORITHM", "ObjectStoreError", "ObjectStoreNotFound", "SERVICE", "StoredObject",
    "authorization", "close", "configured", "credentials_configured", "delete_object",
    "exists", "get_named", "get_object", "list_objects", "put_object", "uri_encode",
]
