"""One-time codes from an authenticator app, per RFC 6238.

Written here rather than pulled in, for the same reason the S3 signature was:
the algorithm is short, fully specified, and **the RFC publishes test vectors**,
so this can be checked against authoritative values instead of trusted. Those
vectors are pinned in the tests. A dependency whose correctness you cannot
verify is worth more than one whose correctness you can.

What this module is not: it holds no state and knows nothing about people. A
code that verifies here has only proven that whoever sent it holds the secret
at this moment. That a code is not replayed a second time is a property of the
records, not of the arithmetic, and it lives with them.

SHA-1 is not a mistake here. TOTP's security does not rest on collision
resistance, it rests on HMAC with a secret, and every authenticator app the
firm will actually use speaks SHA-1 with six digits. Choosing SHA-256 would
buy nothing and lock out the apps.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import quote

DIGITS = 6
STEP_SECONDS = 30
# One step either side. A phone whose clock is half a minute out is common and
# is not an attack; a wider window is a longer replay opportunity for a code
# somebody read over a shoulder.
DRIFT_STEPS = 1

_SECRET_BYTES = 20  # what RFC 4226 recommends, and what the apps expect


class TOTPError(ValueError):
    """A malformed secret or code. Never carries either."""


def new_secret() -> str:
    """A fresh base32 secret, in the form authenticator apps accept."""
    return base64.b32encode(secrets.token_bytes(_SECRET_BYTES)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    text = (secret or "").strip().replace(" ", "").upper()
    if not text:
        raise TOTPError("empty TOTP secret")
    # Base32 wants the padding back before it will decode.
    padded = text + "=" * (-len(text) % 8)
    try:
        return base64.b32decode(padded, casefold=True)
    except Exception as exc:
        raise TOTPError("the TOTP secret is not valid base32") from exc


def counter_at(when: float | None = None) -> int:
    """Which 30-second step a moment falls in."""
    return int((time.time() if when is None else when) // STEP_SECONDS)


def code_for_counter(secret: str, counter: int, digits: int = DIGITS) -> str:
    """The code for one step, by the book.

    RFC 4226 dynamic truncation: the low nibble of the last byte picks where
    to read four bytes, the high bit is dropped so the number is positive on
    every platform, and the rest is a modulo.
    """
    mac = hmac.new(_key(secret), counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    truncated = int.from_bytes(mac[offset:offset + 4], "big") & 0x7FFFFFFF
    return str(truncated % (10 ** digits)).zfill(digits)


def code_at(secret: str, when: float | None = None, digits: int = DIGITS) -> str:
    """The code an app would be showing at this moment."""
    return code_for_counter(secret, counter_at(when), digits)


def matching_counter(secret: str, code: str, *, when: float | None = None,
                     digits: int = DIGITS) -> int | None:
    """The step this code belongs to, or None.

    Returns the counter rather than a boolean so the caller can refuse a code
    it has already accepted. Without that, a code stays usable for its whole
    window and somebody who read it over a shoulder can walk to a machine and
    use it.

    Every candidate is compared in constant time: a comparison that returns
    early on the first wrong digit tells an attacker how much of the code was
    right, one digit at a time.
    """
    # Digits only: apps show `123 456` and people copy what they see. No
    # length check follows, deliberately. One would read as a guard and guard
    # nothing, because a comparison against a generated code of `digits`
    # length already refuses anything shorter or longer. A line that looks
    # like a check and is not is worse than no line, because it gets trusted.
    text = "".join(character for character in (code or "") if character.isdigit())
    here = counter_at(when)
    found = None
    for candidate in range(here - DRIFT_STEPS, here + DRIFT_STEPS + 1):
        if hmac.compare_digest(code_for_counter(secret, candidate, digits), text):
            found = candidate
    return found


def provisioning_uri(secret: str, *, account: str, issuer: str) -> str:
    """The otpauth:// URI an authenticator app reads."""
    label = quote(f"{issuer}:{account}", safe="")
    return (
        f"otpauth://totp/{label}?secret={secret}"
        f"&issuer={quote(issuer, safe='')}&algorithm=SHA1"
        f"&digits={DIGITS}&period={STEP_SECONDS}"
    )


__all__ = [
    "DIGITS", "DRIFT_STEPS", "STEP_SECONDS", "TOTPError", "code_at",
    "code_for_counter", "counter_at", "matching_counter", "new_secret",
    "provisioning_uri",
]
