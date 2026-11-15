"""Sealing a secret that must survive in the database but not in the backup.

Most rows here need no encryption: an attacker who can read the database can
read the corpus, and encrypting against that reader buys nothing. Two kinds of
row are different, and they are different for the same specific reason. The
nightly backup leaves this host for Magalu Cloud, and the keys that open these
rows do not go with it: they live in `.env`, which the deploy writes and the
backup never touches. So a leaked backup is a leaked corpus, which is bad, and
not a set of live credentials to ten people's accounts, which is worse.

**One implementation, several keys.** The sealing is written once here because
two copies of a crypto routine drift, and the one that drifts is the one
nobody re-reads. The keys stay separate on purpose: a Microsoft refresh token
reads somebody's files, and a PJe password acts as the lawyer in court. One
leaking must not open the other.

Losing a key means the people affected enrol again. That is a nuisance
measured in one command each, and it is the right side of the trade.
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class SealError(RuntimeError):
    """A secret could not be sealed or opened. Never carries the secret."""


class Sealer:
    """Encrypt and decrypt with one named key, failing with an actionable message."""

    def __init__(self, key: str | None, *, env_var: str, subject: str,
                 on_open_failure: str) -> None:
        self._key = (key or "").strip()
        self._env_var = env_var
        self._subject = subject
        self._on_open_failure = on_open_failure

    def configured(self) -> bool:
        return bool(self._key)

    def _cipher(self) -> Fernet:
        if not self._key:
            raise SealError(
                f"{self._env_var} is not set, so {self._subject} cannot be stored. "
                'Generate one with: python -c "from cryptography.fernet import '
                'Fernet; print(Fernet.generate_key().decode())"'
            )
        try:
            return Fernet(self._key.encode())
        except (ValueError, TypeError) as exc:
            raise SealError(f"{self._env_var} is not a valid Fernet key.") from exc

    def seal(self, secret: str) -> bytes:
        """Encrypt a secret for storage."""
        return self._cipher().encrypt(secret.encode())

    def unseal(self, sealed: bytes) -> str:
        """Decrypt a stored secret, or fail without echoing anything of it."""
        try:
            return self._cipher().decrypt(bytes(sealed)).decode()
        except (InvalidToken, ValueError) as exc:
            raise SealError(self._on_open_failure) from exc


__all__ = ["SealError", "Sealer"]
