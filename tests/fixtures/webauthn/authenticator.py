"""A software passkey, for tests only.

It answers the options the server hands out the way a platform authenticator
does: an ES256 key, a "none" attestation in CBOR, a client data JSON naming
the origin, and a signature over the authenticator data and that JSON's hash.
Every answer then goes through the real py_webauthn verification on the
server, so a test can produce exactly the response it needs to be refused:
another origin, another RP ID, the user-verification flag unset, a sign count
that did not move.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

_UP = 0x01
_UV = 0x04
_AT = 0x40


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Authenticator:
    """One passkey on one device. `sign_count` is what it reports next;
    0 is what an iCloud passkey always reports."""

    def __init__(self, origin: str, *, sign_count: int = 0) -> None:
        self.origin = origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.user_handle: bytes | None = None
        self.sign_count = sign_count

    def _cose_key(self) -> bytes:
        numbers = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1,
                            -2: numbers.x.to_bytes(32, "big"),
                            -3: numbers.y.to_bytes(32, "big")})

    def _authenticator_data(self, rp_id: str, *, uv: bool, attested: bool) -> bytes:
        flags = _UP | (_UV if uv else 0) | (_AT if attested else 0)
        data = (hashlib.sha256(rp_id.encode()).digest() + bytes([flags])
                + self.sign_count.to_bytes(4, "big"))
        if attested:
            data += (bytes(16) + len(self.credential_id).to_bytes(2, "big")
                     + self.credential_id + self._cose_key())
        return data

    def _client_data(self, kind: str, challenge: str, origin: str | None) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge,
                           "origin": origin or self.origin,
                           "crossOrigin": False}).encode()

    def create(self, options: dict, *, origin: str | None = None,
               rp_id: str | None = None, uv: bool = True) -> dict:
        """What navigator.credentials.create() returns, as JSON."""
        self.user_handle = unb64(options["user"]["id"])
        client_data = self._client_data("webauthn.create", options["challenge"], origin)
        auth_data = self._authenticator_data(rp_id or options["rp"]["id"], uv=uv,
                                             attested=True)
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": b64(self.credential_id), "rawId": b64(self.credential_id),
            "type": "public-key", "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
            "response": {"clientDataJSON": b64(client_data),
                         "attestationObject": b64(attestation),
                         "transports": ["internal"]},
        }

    def get(self, options: dict, *, origin: str | None = None,
            rp_id: str | None = None, uv: bool = True) -> dict:
        """What navigator.credentials.get() returns, as JSON, signed."""
        client_data = self._client_data("webauthn.get", options["challenge"], origin)
        auth_data = self._authenticator_data(rp_id or options["rpId"], uv=uv,
                                             attested=False)
        signature = self.key.sign(auth_data + hashlib.sha256(client_data).digest(),
                                  ec.ECDSA(hashes.SHA256()))
        return {
            "id": b64(self.credential_id), "rawId": b64(self.credential_id),
            "type": "public-key", "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
            "response": {"clientDataJSON": b64(client_data),
                         "authenticatorData": b64(auth_data),
                         "signature": b64(signature),
                         "userHandle": b64(self.user_handle) if self.user_handle else None},
        }
