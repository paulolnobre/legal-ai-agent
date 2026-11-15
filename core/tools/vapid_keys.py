"""Draw the VAPID key pair Web Push signs with, as two `.env` lines.

Run on the VPS, appended straight to the file the app reads:

    python -m core.tools.vapid_keys >> .env

It prints those two lines and nothing else, so the values go from here to
`.env` without passing through a terminal, a session or a commit. The public
key is the uncompressed P-256 point a browser's `PushManager.subscribe`
takes; the private key is the raw 32-byte scalar, which pywebpush (py_vapid)
reads as it is. Both unpadded base64url.

It reads no setting and imports nothing of the app, so it runs before the
keys exist.
"""
from __future__ import annotations

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def draw() -> tuple[str, str]:
    """A fresh pair: (public, private)."""
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, "big")
    public = key.public_key().public_bytes(serialization.Encoding.X962,
                                           serialization.PublicFormat.UncompressedPoint)
    return _b64url(public), _b64url(private)


def main() -> None:
    public, private = draw()
    print(f"WEB_PUSH_VAPID_PUBLIC_KEY={public}")
    print(f"WEB_PUSH_VAPID_PRIVATE_KEY={private}")


if __name__ == "__main__":
    main()
