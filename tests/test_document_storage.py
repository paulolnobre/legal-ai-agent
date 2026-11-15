"""Tests for private retention of original documents."""
import hashlib

import pytest

from core.config import settings
from core.services import document_storage


def test_original_is_private_content_addressed_and_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    monkeypatch.setattr(settings, "document_storage_path", str(tmp_path))
    data = b"Contrato particular de prestacao de servicos"
    digest = hashlib.sha256(data).hexdigest()

    key, created = document_storage.store_original(
        data,
        raw_sha256=digest,
        filename="../contrato.DOCX",
    )
    repeated_key, repeated_created = document_storage.store_original(
        data,
        raw_sha256=digest,
        filename="contrato.DOCX",
    )

    assert created is True
    assert repeated_created is False
    assert repeated_key == key
    assert key.endswith(".docx")
    assert ".." not in key
    assert document_storage.read_original(key) == data


def test_storage_key_cannot_escape_the_private_root(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr(settings, "document_storage_path", str(tmp_path))

    with pytest.raises(ValueError, match="Invalid document storage key"):
        document_storage.read_original("../../etc/passwd")
