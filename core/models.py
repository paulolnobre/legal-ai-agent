"""Validated models used by the legal corpus."""
from datetime import datetime

from pydantic import BaseModel


class StaffMember(BaseModel):
    id: int
    chat_id: str | None = None
    email: str | None = None
    name: str
    role: str = "lawyer"
    active: bool = True


class AccessRequest(BaseModel):
    """A pending Telegram access request, never authorized by the model."""

    id: int
    request_code: str
    chat_id: str
    name: str
    username: str | None = None
    status: str = "pending"
    requested_at: datetime | None = None


class SearchHit(BaseModel):
    chunk_id: int
    document_id: int
    title: str
    doc_type: str | None = None
    matter: str | None = None
    content: str
    distance: float
    # Which folder the document behind this passage lives in. It decides
    # whether an answer built on it may be cached for the whole firm.
    visibility: str = "firm"


class DocumentRecord(BaseModel):
    id: int
    title: str
    doc_type: str | None = None
    matter: str | None = None
    source_filename: str | None = None
    source_storage_key: str | None = None
    source_sha256: str | None = None
    source_size_bytes: int | None = None
    full_text: str


class DocumentSource(BaseModel):
    """Private-original metadata used for authorized retrieval only."""

    id: int
    source_filename: str
    source_storage_key: str
    source_sha256: str
    source_size_bytes: int
    # Set when the original is in the bucket, empty when it is on local disk.
    # The reader needs it to open the object, so it travels with the metadata.
    source_wrapped_key: bytes | None = None


class OfficialSource(BaseModel):
    """The official page a document is kept current against."""

    id: int
    title: str
    source_url: str | None = None
    checked_at: datetime | None = None


class DocumentSummary(BaseModel):
    id: int
    title: str
    doc_type: str | None = None
    matter: str | None = None
    created_at: datetime | None = None
    # Which folder it is in: private, team or firm. Filled where a listing
    # shows it; None where nothing asked.
    visibility: str | None = None
