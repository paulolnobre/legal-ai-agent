"""Sending a notice to the installed app, against local push services.

Each push service here is a real HTTPS server on 127.0.0.1 with a certificate
made for the test, trusted only by these tests. The subscriptions are real
rows (inserted directly: the allowlist is checked when a device subscribes,
and 127.0.0.1 is rightly not on it). What a stub receives is decrypted with
the device's own keys and its VAPID token is verified with the server's
public key, so a payload or a header that is wrong cannot pass by looking
plausible.

Skipped without TEST_DATABASE_URL, except where nothing is read.
"""
import asyncio
import base64
import contextlib
import datetime
import ipaddress
import json
import logging
import re
import secrets
import socket
import ssl
import threading
import time
from types import SimpleNamespace

import http_ece
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.x509.oid import NameOID
from pydantic import SecretStr

from core.config import settings
from core.services import push

PAULO, RUI, ANA = 1, 4, 5
SUBJECT = "mailto:contato@example.com"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ── a push service on this machine ──────────────────────────────────────────


def _certificate(directory) -> tuple[str, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = directory / "stub.crt", directory / "stub.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return str(cert_path), str(key_path)


class Stub:
    """One push service. `mode` is a status code, ("redirect", url),
    "silent" (reads the request, never answers) or "trickle" (answers one
    byte a second, never finishing)."""

    def __init__(self, tls: ssl.SSLContext, mode):
        self.mode = mode
        self.requests: list[dict] = []
        self._tls = tls
        self._stop = threading.Event()
        self._sock = socket.create_server(("127.0.0.1", 0))
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self.origin = f"https://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def endpoint(self, token: str | None = None) -> str:
        return f"{self.origin}/push/{token or secrets.token_urlsafe(24)}"

    def close(self):
        self._stop.set()
        self._thread.join(timeout=5)
        self._sock.close()

    def _serve(self):
        while not self._stop.is_set():
            try:
                raw, _ = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self._answer, args=(raw,), daemon=True).start()

    def _answer(self, raw):
        try:
            raw.settimeout(10)
            conn = self._tls.wrap_socket(raw, server_side=True)
        except (OSError, ssl.SSLError):
            raw.close()
            return
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
            head, _, body = data.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            headers = {k.strip().lower(): v.strip()
                       for k, _, v in (line.partition(":") for line in lines[1:])}
            length = int(headers.get("content-length", "0"))
            while len(body) < length:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                body += chunk
            method, path, _ = lines[0].split(" ", 2)
            self.requests.append({"method": method, "path": path, "headers": headers,
                                  "body": body})
            if self.mode == "silent":
                self._stop.wait(30)
                return
            if self.mode == "trickle":
                for byte in b"HTTP/1.1 201 Created\r\nX-Slow: " + b"a" * 40:
                    if self._stop.wait(1):
                        return
                    conn.sendall(bytes([byte]))
                return
            if isinstance(self.mode, tuple):
                status, extra = 307, f"Location: {self.mode[1]}\r\n"
            else:
                status, extra = int(self.mode), ""
            conn.sendall(f"HTTP/1.1 {status} X\r\n{extra}Content-Length: 0\r\n"
                         "Connection: close\r\n\r\n".encode())
        except (OSError, ssl.SSLError):
            pass
        finally:
            with contextlib.suppress(OSError):
                conn.close()


@pytest.fixture
def services(tmp_path, monkeypatch):
    """Make push services on demand, trusted by the sender for this test only."""
    cert, key = _certificate(tmp_path)
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(cert, key)
    monkeypatch.setattr(push, "_VERIFY", ssl.create_default_context(cafile=cert))
    made: list[Stub] = []

    def make(mode=201) -> Stub:
        stub = Stub(server_tls, mode)
        made.append(stub)
        return stub

    yield make
    for stub in made:
        stub.close()


# ── the server's keys and switch ────────────────────────────────────────────


@pytest.fixture
def vapid(monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    private = _b64(key.private_numbers().private_value.to_bytes(32, "big"))
    public = key.public_key().public_bytes(serialization.Encoding.X962,
                                           serialization.PublicFormat.UncompressedPoint)
    monkeypatch.setattr(settings, "web_enabled", True)
    monkeypatch.setattr(settings, "web_app", "pilot")
    monkeypatch.setattr(settings, "web_app_staff", f"{PAULO},{RUI}")
    monkeypatch.setattr(settings, "web_push_vapid_public_key", _b64(public))
    monkeypatch.setattr(settings, "web_push_vapid_private_key", SecretStr(private))
    monkeypatch.setattr(settings, "web_push_subject", SUBJECT)
    return SimpleNamespace(public=public)


# ── the database ────────────────────────────────────────────────────────────


class Device(SimpleNamespace):
    key: ec.EllipticCurvePrivateKey
    auth: bytes
    endpoint: str


@pytest.fixture
def store(db, monkeypatch):
    """This run's schema, the pool `notify` takes its connection from, and a
    way to give a person a device subscribed at a given endpoint."""
    asyncio.run(_build(db))

    class _Pool:
        def connection(self, timeout=None):
            @contextlib.asynccontextmanager
            async def opened():
                conn = await db.connect()
                try:
                    yield conn
                    await conn.commit()
                finally:
                    await conn.close()
            return opened()

    async def get_pool():
        return _Pool()

    monkeypatch.setattr(push, "get_pool", get_pool)

    async def device(staff_id: int, endpoint: str) -> Device:
        key = ec.generate_private_key(ec.SECP256R1())
        auth = secrets.token_bytes(16)
        point = key.public_key().public_bytes(serialization.Encoding.X962,
                                              serialization.PublicFormat.UncompressedPoint)
        conn = await db.connect()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO staff_passkeys (staff_id, credential_id, public_key, "
                    "user_handle) VALUES (%s, %s, 'k', 'h') RETURNING id",
                    (staff_id, secrets.token_bytes(16)))
                row = await cur.fetchone()
                assert row is not None
                await cur.execute(
                    "INSERT INTO push_subscriptions (staff_id, passkey_id, endpoint, p256dh, "
                    "auth) VALUES (%s, %s, %s, %s, %s)",
                    (staff_id, row[0], endpoint, point, auth))
            await conn.commit()
        finally:
            await conn.close()
        return Device(key=key, auth=auth, endpoint=endpoint)

    async def rows():
        conn = await db.connect()
        try:
            async with conn.cursor() as cur:
                await cur.execute("SELECT endpoint, last_ok_at IS NOT NULL "
                                  "FROM push_subscriptions ORDER BY id")
                return await cur.fetchall()
        finally:
            await conn.close()

    yield SimpleNamespace(device=lambda *a: asyncio.run(device(*a)),
                          rows=lambda: asyncio.run(rows()))
    asyncio.run(db.drop())


async def _build(db):
    conn = await db.connect()
    try:
        await db.build(conn)
    finally:
        await conn.close()


def _notify(staff_id: int, kind) -> None:
    asyncio.run(push.notify(staff_id, kind))


def _verify_jwt(token: str, public: bytes) -> dict:
    header, claims, signature = token.split(".")
    raw = _unb64(signature)
    assert len(raw) == 64, "ES256 signs as r || s"
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public)
    key.verify(der, f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    assert json.loads(_unb64(header)) == {"typ": "JWT", "alg": "ES256"}
    return json.loads(_unb64(claims))


# ── what is sent ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("kind", "urgency"), [("answer", "high"), ("djen", "normal"),
                                                ("access", "normal")])
def test_payload_is_exactly_kind_and_url(store, services, vapid, kind, urgency):
    stub = services(201)
    device = store.device(RUI, stub.endpoint())
    before = int(time.time())

    _notify(RUI, kind)

    [request] = stub.requests
    assert request["method"] == "POST"
    assert stub.origin + request["path"] == device.endpoint
    headers = request["headers"]
    plain = http_ece.decrypt(request["body"], private_key=device.key,
                             auth_secret=device.auth, version="aes128gcm")
    assert json.loads(plain) == {"kind": kind, "url": "/web/?app=1"}
    assert plain == json.dumps({"kind": kind, "url": "/web/?app=1"}).encode()
    assert headers["content-encoding"] == "aes128gcm"
    assert headers["content-type"] == "application/octet-stream"
    assert headers["ttl"] == "86400"
    assert headers["urgency"] == urgency

    match = re.fullmatch(r"vapid t=([A-Za-z0-9_.-]+),\s*k=([A-Za-z0-9_-]+)",
                         headers["authorization"])
    assert match, headers["authorization"]
    assert _unb64(match.group(2)) == vapid.public
    claims = _verify_jwt(match.group(1), vapid.public)
    assert claims["aud"] == stub.origin
    assert claims["sub"] == SUBJECT
    assert before + 12 * 3600 - 5 <= claims["exp"] <= int(time.time()) + 12 * 3600 + 5
    assert store.rows() == [(device.endpoint, True)], "a delivered notice marks it ok"


def test_the_urls_are_fixed_and_land_on_the_app():
    assert push.URLS == {"answer": "/web/?app=1", "djen": "/web/?app=1",
                         "access": "/web/?app=1", "deadline": "/web/processos/?app=1"}


def test_notify_returns_true_only_on_delivery(store, services, vapid):
    """The deadline alerts record a push-only person only when it arrived."""
    fine, broken = services(201), services(500)
    store.device(RUI, fine.endpoint())
    store.device(PAULO, broken.endpoint())

    assert asyncio.run(push.notify(RUI, "deadline")) is True
    assert asyncio.run(push.notify(PAULO, "deadline")) is False
    assert asyncio.run(push.notify(ANA, "deadline")) is False


def test_every_device_of_the_person_and_nobody_else_is_told(store, services, vapid):
    stub = services(201)
    first = store.device(RUI, stub.endpoint())
    second = store.device(RUI, stub.endpoint())
    store.device(PAULO, stub.endpoint())

    _notify(RUI, "djen")

    assert sorted(stub.origin + r["path"] for r in stub.requests) == sorted(
        [first.endpoint, second.endpoint])


# ── what the push service answers ───────────────────────────────────────────


@pytest.mark.parametrize("status", [404, 410])
def test_404_and_410_drop_the_subscription(store, services, vapid, status):
    gone, fine = services(status), services(201)
    store.device(RUI, gone.endpoint())
    kept = store.device(RUI, fine.endpoint())

    _notify(RUI, "answer")

    assert len(gone.requests) == 1
    assert store.rows() == [(kept.endpoint, True)]


@pytest.mark.parametrize("mode", [500, "silent"])
def test_500_and_timeout_keep_it_and_log_the_host_only(store, services, vapid, caplog, mode):
    stub = services(mode)
    secret = "capability-" + secrets.token_urlsafe(16)
    device = store.device(RUI, stub.endpoint(secret))

    with caplog.at_level(logging.INFO, logger="core.services.push"):
        _notify(RUI, "answer")

    assert len(stub.requests) == 1
    assert store.rows() == [(device.endpoint, False)], "kept, and not marked ok"
    assert "127.0.0.1" in caplog.text
    if mode == 500:
        assert "500" in caplog.text
    assert secret not in caplog.text and "/push/" not in caplog.text, "the endpoint is a capability"


def test_a_redirect_is_not_followed(store, services, vapid, caplog):
    elsewhere = services(201)
    redirecting = services(("redirect", elsewhere.endpoint()))
    device = store.device(RUI, redirecting.endpoint())

    with caplog.at_level(logging.INFO, logger="core.services.push"):
        _notify(RUI, "answer")

    assert len(redirecting.requests) == 1
    assert elsewhere.requests == [], "the push service cannot point the server anywhere"
    assert store.rows() == [(device.endpoint, False)], "a 3xx is a failure, not a success"
    assert "307" in caplog.text


def test_a_hanging_push_service_costs_at_most_its_timeout(store, services, vapid):
    """Review Focus 5: a server that never answers and one that trickles a
    byte a second (which a per-read timeout never cuts) are each cut at the
    deadline, and the person's other device still gets the notice."""
    silent, trickle, fine = services("silent"), services("trickle"), services(201)
    store.device(RUI, silent.endpoint())
    store.device(RUI, trickle.endpoint())
    last = store.device(RUI, fine.endpoint())

    started = time.monotonic()
    _notify(RUI, "answer")
    elapsed = time.monotonic() - started

    assert len(silent.requests) == 1 and len(trickle.requests) == 1
    assert len(fine.requests) == 1, "the third subscription is still sent"
    assert push.DEADLINE == 5
    assert elapsed < push.DEADLINE + 2.5, "the devices are told at once, not one after another"
    assert (last.endpoint, True) in store.rows()


def test_each_send_is_cut_at_the_deadline(monkeypatch):
    """The deadline is on the whole exchange, not on a socket read."""
    monkeypatch.setattr(push, "DEADLINE", 0.2)

    async def forever(*_args):
        await asyncio.sleep(30)
        return 201

    monkeypatch.setattr(push, "_send_one", forever)
    subscription = push.Subscription(1, "https://web.push.apple.com/x", b"\x04" * 65, b"a" * 16)

    started = time.monotonic()
    status = asyncio.run(push._attempt(subscription, b"{}", "high"))

    assert status is None
    assert time.monotonic() - started < 2


# ── never in the way ────────────────────────────────────────────────────────


def test_notify_never_raises(monkeypatch, vapid, caplog):
    async def broken_pool():
        raise RuntimeError("the database went away")

    monkeypatch.setattr(push, "get_pool", broken_pool)
    with caplog.at_level(logging.WARNING, logger="core.services.push"):
        asyncio.run(push.notify(RUI, "answer"))

    async def broken_send(*_args):
        raise ValueError("the push service sent nonsense")

    async def subscriptions(_conn, _staff_id):
        return [push.Subscription(1, "https://web.push.apple.com/secret-endpoint",
                                  b"\x04" * 65, b"a" * 16)]

    class _Pool:
        @contextlib.asynccontextmanager
        async def connection(self, timeout=None):
            yield "conn"

    async def pool():
        return _Pool()

    monkeypatch.setattr(push, "get_pool", pool)
    monkeypatch.setattr(push.push_subscriptions, "of", subscriptions)
    monkeypatch.setattr(push, "_send_one", broken_send)
    with caplog.at_level(logging.WARNING, logger="core.services.push"):
        asyncio.run(push.notify(RUI, "answer"))

    assert "web.push.apple.com" in caplog.text
    assert "secret-endpoint" not in caplog.text


def test_a_cancelled_notice_is_not_swallowed(monkeypatch, vapid):
    """Never raising is about failures: the worker's own deadline still
    stops it."""
    async def subscriptions(_conn, _staff_id):
        await asyncio.sleep(30)

    class _Pool:
        @contextlib.asynccontextmanager
        async def connection(self, timeout=None):
            yield "conn"

    async def pool():
        return _Pool()

    monkeypatch.setattr(push, "get_pool", pool)
    monkeypatch.setattr(push.push_subscriptions, "of", subscriptions)

    with pytest.raises(TimeoutError):
        asyncio.run(asyncio.wait_for(push.notify(RUI, "answer"), 0.2))


@pytest.mark.parametrize("change", [
    {"web_app": "off"},
    {"web_enabled": False},
    {"web_app_staff": "1, 14"},
    {"web_push_vapid_private_key": None},
    {"web_push_vapid_public_key": ""},
    {"web_push_subject": "https://example.com"},
])
def test_nothing_is_sent_outside_the_switch_or_without_keys(monkeypatch, vapid, change):
    """Not even a connection is taken: the switch and the keys are read first."""
    taken: list[str] = []

    async def pool():
        taken.append("pool")
        raise AssertionError("no connection outside the switch")

    monkeypatch.setattr(push, "get_pool", pool)
    for name, value in change.items():
        monkeypatch.setattr(settings, name, value)

    asyncio.run(push.notify(RUI, "answer"))

    assert taken == []


def test_the_switch_reaches_a_listed_person(monkeypatch, vapid):
    """The other side of the test above: the same person, switched on, is looked up."""
    taken: list[int] = []

    async def subscriptions(_conn, staff_id):
        taken.append(staff_id)
        return []

    class _Pool:
        @contextlib.asynccontextmanager
        async def connection(self, timeout=None):
            yield "conn"

    async def pool():
        return _Pool()

    monkeypatch.setattr(push, "get_pool", pool)
    monkeypatch.setattr(push.push_subscriptions, "of", subscriptions)

    asyncio.run(push.notify(RUI, "answer"))
    asyncio.run(push.notify(ANA, "answer"))

    assert taken == [RUI], "Ana is not in the pilot"
