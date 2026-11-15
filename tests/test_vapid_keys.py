"""The VAPID key pair, drawn by the command the runbook names.

The command is run here as written (`python -m core.tools.vapid_keys`), because
its output is appended straight to `.env` on the VPS: one extra line printed is
a broken `.env`, and a public key that does not belong to the private one is a
push service refusing every notice.
"""
import base64
import re
import subprocess
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid02

ROOT = Path(__file__).resolve().parent.parent


def _decoded(text: str) -> bytes:
    assert re.fullmatch(r"[A-Za-z0-9_-]+", text), "not unpadded base64url"
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _run() -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "core.tools.vapid_keys"], cwd=ROOT,
                          capture_output=True, text=True, timeout=60, check=True)


def test_vapid_keys_print_two_lines_that_round_trip():
    done = _run()

    assert done.stderr == ""
    lines = done.stdout.split("\n")
    assert lines[-1] == "" and len(lines) == 3, done.stdout.count("\n")
    public_line, private_line = lines[:2]
    assert public_line.startswith("WEB_PUSH_VAPID_PUBLIC_KEY=")
    assert private_line.startswith("WEB_PUSH_VAPID_PRIVATE_KEY=")
    public = _decoded(public_line.split("=", 1)[1])
    private = _decoded(private_line.split("=", 1)[1])
    assert len(public) == 65 and public[0] == 4, "an uncompressed P-256 point"
    assert len(private) == 32

    derived = ec.derive_private_key(int.from_bytes(private, "big"), ec.SECP256R1())
    assert derived.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint) == public
    # And the library that will sign with it reads it the same way.
    vapid = Vapid02.from_string(private_line.split("=", 1)[1])
    assert vapid.public_key is not None
    assert vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint) == public


def test_every_run_draws_a_new_pair():
    first, second = _run().stdout, _run().stdout

    assert first != second
    assert first.split("\n")[1] != second.split("\n")[1]
