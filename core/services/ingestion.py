"""Legal document ingestion: extract text -> chunk -> embed -> store (async).

Supports PDF and DOCX. Deduplication by content SHA-256: re-ingesting the same
file does not create a duplicate. File size and extracted-text length are
capped so a hostile or corrupted document cannot exhaust memory or run up
embedding costs.
"""
import asyncio
import hashlib
import io
import logging
import os
import re
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from core.config import CHUNK_OVERLAP, CHUNK_SIZE, settings
from core.integrations import cache
from core.integrations.embeddings import embed_texts
from core.integrations.web_fetch import html_to_text
from core.repositories.documents import (
    add_chunks,
    create_document,
    link_official_source,
    find_document_by_sha,
    get_document,
    get_document_source,
    log_access,
    mark_source_checked,
    replace_document_original,
    replace_document_text,
    set_document_source,
)
from core.repositories.teams import Scope
from core.services import document_storage

logger = logging.getLogger(__name__)


def _ocr_images(images) -> str:
    try:
        import pytesseract
    except ImportError as exc:
        raise RuntimeError("OCR unavailable: install pytesseract and Tesseract.") from exc
    return "\n".join(
        pytesseract.image_to_string(image, lang=settings.ocr_language) for image in images
    )


def _extract_pdf_bytes(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    if len(text.strip()) >= settings.ocr_pdf_min_chars or not settings.ocr_enabled:
        return text
    if len(reader.pages) > settings.max_ocr_pages:
        raise ValueError(f"PDF exceeds the {settings.max_ocr_pages}-page OCR limit.")
    try:
        from pdf2image import convert_from_bytes
    except ImportError as exc:
        raise RuntimeError("PDF OCR unavailable: install pdf2image and Poppler.") from exc
    return _ocr_images(convert_from_bytes(data, dpi=200, fmt="jpeg", thread_count=1))


def _extract_docx_bytes(data: bytes) -> str:
    import docx

    document = docx.Document(io.BytesIO(data))
    return "\n".join(p.text for p in document.paragraphs)


def _extract_image_bytes(data: bytes) -> str:
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Image OCR unavailable: install Pillow.") from exc
    image = Image.open(io.BytesIO(data))
    image.load()
    return _ocr_images([image])


def _decode_markup(data: bytes) -> str:
    """Text from bytes, honouring what the page says about itself.

    Not a detail here. Planalto still serves pages announcing themselves as
    `Microsoft FrontPage 6.0`, in a Windows codepage, and decoding those as
    UTF-8 either raises or turns every accented word in the Civil Code into
    mojibake. The declared charset is tried first, then the encodings such
    pages actually use.
    """
    declared = _META_CHARSET.search(data[:4096].decode("ascii", errors="ignore"))
    candidates = ([declared.group(1)] if declared else []) + ["utf-8", "cp1252", "latin-1"]
    for encoding in candidates:
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _extract_html_bytes(data: bytes) -> str:
    """The readable text of a saved page, refusing what is obviously not one.

    The markup-to-text step is the one the page reader already uses for pages
    it fetches itself, so a law saved from a browser and a law read from a URL
    become the same text.
    """
    text = html_to_text(_decode_markup(data)).strip()
    if len(text) < _MIN_HTML_CHARS:
        raise ValueError(
            "a página tem quase nenhum texto: normalmente é um aviso do site "
            "(verificação de segurança, erro ou redirecionamento) salvo no "
            "lugar do documento"
        )
    return text


def extract_text_bytes(data: bytes, filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        return _extract_pdf_bytes(data)
    if ext == ".docx":
        return _extract_docx_bytes(data)
    if ext in (".txt", ".md"):
        return data.decode("utf-8")
    if ext in (".htm", ".html"):
        return _extract_html_bytes(data)
    if ext in (".png", ".jpg", ".jpeg", ".tif", ".tiff"):
        return _extract_image_bytes(data)
    raise ValueError(f"Unsupported format: {ext}")


def extract_text(path: str) -> str:
    with open(path, "rb") as file:
        return extract_text_bytes(file.read(), os.path.basename(path))


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split by paragraphs, grouping up to ~size characters, with overlap."""
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(current) + len(paragraph) + 1 > size and current:
            chunks.append(current.strip())
            # overlap: keep the tail of the previous chunk to preserve context
            current = current[-overlap:] + "\n" + paragraph if overlap else paragraph
        else:
            current = f"{current}\n{paragraph}" if current else paragraph
    if current.strip():
        chunks.append(current.strip())
    return chunks


# What this system accepts, defined once. A Telegram attachment, a file from
# the firm's bucket and the prose that names one all answer to this: two copies
# of an allowlist drift, and the drift is invisible until the day one of them
# lets something through.
DOCUMENT_EXTENSIONS = frozenset({
    ".pdf", ".docx", ".txt", ".md", ".htm", ".html",
    ".png", ".jpg", ".jpeg", ".tif", ".tiff",
})

# What a person who sends a document reads, on Telegram and in the panel's
# chat alike: one wording, kept here so the two cannot drift apart.
DOCUMENT_FORMAT_REFUSED = "Formato não suportado. Envie PDF, DOCX, TXT, Markdown ou uma imagem."
DOCUMENT_UNIDENTIFIED = "Não consegui identificar o arquivo enviado."


def document_too_large() -> str:
    return f"O documento excede o limite de {settings.max_doc_mb} MB."


def document_received(filename: str) -> str:
    return f"📄 Recebi '{filename}'. Iniciando o processamento."


def document_failed(filename: str) -> str:
    return f"Não consegui processar '{filename}'. Verifique o formato e os limites do arquivo."


def document_done(filename: str) -> str:
    return (f"Concluído! O documento '{filename}' foi processado e integrado "
            "à base de conhecimento. Você já pode fazer perguntas sobre ele.")


def document_known(filename: str) -> str:
    return f"O documento '{filename}' já fazia parte da base de conhecimento."


def _is_docx(data: bytes) -> bool:
    """A zip holding `word/document.xml`: what python-docx will open."""
    if not data.startswith(b"PK\x03\x04"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return "word/document.xml" in archive.namelist()
    except (zipfile.BadZipFile, ValueError, OSError):
        return False


def _is_utf8_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _is_markup(data: bytes) -> bool:
    """A saved page: text, whatever its codepage, that opens with markup."""
    if b"\x00" in data:
        return False
    return data.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<")


# A document's first bytes, by the extension its name claims (lesson 21: the
# name is a claim, the bytes are the fact).
_DOCUMENT_BYTES = {
    ".pdf": lambda data: data.startswith(b"%PDF-"),
    ".docx": _is_docx,
    ".txt": _is_utf8_text,
    ".md": _is_utf8_text,
    ".htm": _is_markup,
    ".html": _is_markup,
    ".png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
    ".jpg": lambda data: data.startswith(b"\xff\xd8\xff"),
    ".jpeg": lambda data: data.startswith(b"\xff\xd8\xff"),
    ".tif": lambda data: data[:4] in (b"II*\x00", b"MM\x00*"),
    ".tiff": lambda data: data[:4] in (b"II*\x00", b"MM\x00*"),
}


def document_matches(data: bytes, filename: str) -> bool:
    """Whether the bytes are the kind of document the name says, for every
    extension this system accepts; False for an empty file or another name."""
    check = _DOCUMENT_BYTES.get(Path(filename).suffix.lower())
    return bool(data) and check is not None and check(data)


# Below this, an HTML file is almost certainly not the document somebody meant
# to archive. Brazilian legal sites answer a browser with a bot-protection page
# and let it save that instead: the firm's first upload to the document bucket
# was a 9KB "Verificação de segurança — Senado Federal" wearing the filename
# "Código Civil 2 ed.htm". Ingested, it would have sat in the archive as the
# Civil Code and been cited as one.
_MIN_HTML_CHARS = 500
_TITLE_SEPARATORS = re.compile(r"[\s_]+")
_META_CHARSET = re.compile(r"charset=[\"']?([\w-]+)", re.IGNORECASE)


def readable_title(filename: str) -> str:
    """The filename as a person would have written it, not as a disk stored it.

    Files arrive named by whatever produced them, and legal material arrives
    worst of all: `Código_De_Processo_Civil_LEI_Nº_13_105,_DE_16_DE_MARÇO_DE_2015.docx`
    is a real example from this firm's archive. The extension is noise in a
    catalogue and underscores are a filesystem's idea of a space.

    Nothing else is touched. Capitalisation is left exactly as it came, because
    a statute's name is cited in the case it was published in, and a title that
    guesses is worse than one that is merely ugly. What guessing cannot fix,
    /renomear fixes by hand.
    """
    stem = Path(filename).stem
    cleaned = _TITLE_SEPARATORS.sub(" ", stem).strip()
    return cleaned or Path(filename).name


@dataclass(frozen=True)
class Ingested:
    """What one ingestion produced, including the text that was indexed.

    The text is here because the caller often has to hand the same document
    back to a person: /ingerir sends the piece it just read as a file, and
    extracting a 1.8 MB page twice to do that is work nobody needs.
    """

    document_id: int
    chunk_count: int
    text: str


async def ingest_bytes(
    conn,
    data: bytes,
    *,
    filename: str,
    title=None,
    doc_type=None,
    matter=None,
    uploaded_by=None,
    source_url: str | None = None,
    visibility: str = "private",
    team_id: int | None = None,
) -> tuple[int, int]:
    """Extract and ingest bytes from a trusted document source.

    Returns the document id and how many chunks were indexed. A document that
    was already ingested returns its existing id with a chunk count of zero, so
    the caller can tell "stored now" from "already here" without a second query.
    """
    result = await ingest_document(
        conn, data, filename=filename, title=title, doc_type=doc_type,
        matter=matter, uploaded_by=uploaded_by, source_url=source_url,
        visibility=visibility, team_id=team_id,
    )
    return result.document_id, result.chunk_count


async def ingest_document(
    conn,
    data: bytes,
    *,
    filename: str,
    title=None,
    doc_type=None,
    matter=None,
    uploaded_by=None,
    source_url: str | None = None,
    visibility: str = "private",
    team_id: int | None = None,
) -> Ingested:
    """`ingest_bytes`, and the extracted text with it.

    A document lands in the narrowest folder by default: whoever sent it, and
    nobody else. What belongs to the whole firm (a law, a model pleading, the
    shared inbox) says so explicitly.

    Both halves in one go, for a caller that holds no lock; the panel's worker
    runs them apart, with only `write_document` under its publication guard.
    """
    prepared = await prepare_document(conn, data, filename=filename)
    try:
        result, kept = await write_document(
            conn, prepared, title=title, doc_type=doc_type, matter=matter,
            uploaded_by=uploaded_by, source_url=source_url, visibility=visibility,
            team_id=team_id,
        )
        await conn.commit()
    except Exception:
        await conn.rollback()
        await discard_document(prepared)
        raise
    if not kept:
        await discard_document(prepared)
    if result.chunk_count:
        # Cached answers were true for the corpus as it stood a moment ago;
        # this document may change them, so the answer namespace is dropped.
        # Cached embeddings survive: they depend only on the text that
        # produced them.
        await cache.invalidate_answers()
        logger.info("Document ingested: id=%s, %d chunks", result.document_id,
                    result.chunk_count)
    return result


@dataclass(frozen=True)
class PreparedDocument:
    """A document ready to be written: its text extracted, and, when that
    text is new to the archive, chunked and embedded, with the original
    stored under a name of its own. `stored` is None when nothing had to be
    stored (the text is here already, with its original)."""

    filename: str
    text: str
    sha256: str
    raw_sha256: str
    size_bytes: int
    chunks: list[str] | None
    embeddings: list[list[float]] | None
    stored: document_storage.StoredOriginal | None


async def prepare_document(conn, data: bytes, *, filename: str) -> PreparedDocument:
    """The slow half of `ingest_bytes`: extraction, embeddings and the
    stored original, with nothing written to the database (the look for the
    same text is a read). Raises ValueError for a file out of bounds."""
    if len(data) > settings.max_doc_mb * 1024 * 1024:
        raise ValueError(
            f"File exceeds the {settings.max_doc_mb} MB ingestion limit ({len(data)} bytes)."
        )

    # OCR, pypdf and python-docx are CPU-bound and synchronous. This process
    # also serves the Telegram webhook, so extraction runs in a worker thread:
    # a 100-page scanned PDF must not stall everyone else's messages.
    text = await asyncio.to_thread(extract_text_bytes, data, filename)
    if not text.strip():
        raise ValueError("Could not extract text from the document.")
    if len(text) > settings.max_doc_chars:
        raise ValueError(
            f"Extracted text exceeds the {settings.max_doc_chars}-char ingestion limit."
        )

    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    raw_sha = hashlib.sha256(data).hexdigest()
    prepared = PreparedDocument(filename=filename, text=text, sha256=sha, raw_sha256=raw_sha,
                                size_bytes=len(data), chunks=None, embeddings=None,
                                stored=None)
    existing = await find_document_by_sha(conn, sha)
    if existing:
        # Documents stored before original-file retention can be repaired by
        # re-uploading the same source; preserve the already-indexed corpus.
        if await get_document_source(conn, existing, Scope.unrestricted()):
            return prepared
        return replace(prepared, stored=await _store_own(data, raw_sha, filename))

    chunks = chunk_text(text)
    embeddings = await embed_texts(chunks)
    if embeddings is None:
        raise RuntimeError("Failed to generate embeddings, document NOT stored.")
    return replace(prepared, chunks=chunks, embeddings=embeddings,
                   stored=await _store_own(data, raw_sha, filename))


async def _store_own(data: bytes, raw_sha: str, filename: str) -> document_storage.StoredOriginal:
    # A file of its own: it may be taken back (discard_document) after
    # another ingestion of the same bytes was written, and must never take
    # that one's original with it.
    return await document_storage.store(data, raw_sha256=raw_sha, filename=filename,
                                        exclusive=True)


async def write_document(
    conn,
    prepared: PreparedDocument,
    *,
    title=None,
    doc_type=None,
    matter=None,
    uploaded_by=None,
    source_url: str | None = None,
    visibility: str = "private",
    team_id: int | None = None,
) -> tuple[Ingested, bool]:
    """The quick half: the database writes, in the caller's transaction and
    never committed here. The same text already in the archive (looked for
    again now: another ingestion may have written it meanwhile) is that
    document, with a chunk count of zero. Returns the result and whether
    the stored original is now held by a document; when it is not, the
    caller takes it back with `discard_document` once this is settled."""
    existing = await find_document_by_sha(conn, prepared.sha256)
    if existing:
        kept = False
        if prepared.stored is not None and not await get_document_source(
                conn, existing, Scope.unrestricted()):
            await set_document_source(
                conn,
                existing,
                source_filename=os.path.basename(prepared.filename),
                source_storage_key=prepared.stored.key,
                source_sha256=prepared.raw_sha256,
                source_size_bytes=prepared.size_bytes,
                source_wrapped_key=prepared.stored.wrapped_key or None,
                source_kek_version=prepared.stored.kek_version or None,
                commit=False,
            )
            kept = True
        # An official page this document came from is worth recording even
        # when the text is already here: it is what keeps the law current.
        if source_url:
            await link_official_source(conn, existing, source_url, commit=False)
        logger.info("Document already ingested (sha=%s, id=%s)", prepared.sha256[:12], existing)
        return Ingested(existing, 0, prepared.text), kept
    if prepared.chunks is None or prepared.embeddings is None or prepared.stored is None:
        # The document whose text this matched was deleted meanwhile, and
        # nothing was embedded for a new one.
        raise RuntimeError("The matching document is gone; ingest the file again.")

    stored = prepared.stored
    # Atomic: document + chunks + audit entry commit together or not at all.
    doc_id = await create_document(
        conn,
        title=title or readable_title(prepared.filename),
        full_text=prepared.text,
        sha256=prepared.sha256,
        doc_type=doc_type,
        matter=matter,
        source_filename=os.path.basename(prepared.filename),
        source_storage_key=stored.key,
        source_sha256=prepared.raw_sha256,
        source_size_bytes=prepared.size_bytes,
        source_wrapped_key=stored.wrapped_key or None,
        source_kek_version=stored.kek_version or None,
        source_url=source_url,
        uploaded_by=uploaded_by,
        visibility=visibility,
        team_id=team_id,
        commit=False,
    )
    await add_chunks(conn, doc_id, prepared.chunks, prepared.embeddings, commit=False)
    await log_access(
        conn,
        action="ingest",
        staff_id=uploaded_by,
        document_ids=[doc_id],
        commit=False,
    )
    return Ingested(doc_id, len(prepared.chunks), prepared.text), True


async def discard_document(prepared: PreparedDocument) -> None:
    """Take back an original stored for a document that does not hold it.
    `prepare_document` stores each under a name of its own, so it is this
    preparation's alone."""
    if prepared.stored is not None and prepared.stored.newly_created:
        await document_storage.remove(prepared.stored.key, prepared.stored.wrapped_key)


async def _extract_bounded(data: bytes, filename: str, document_id: int) -> str:
    """Extract text for an existing document, refusing what ingestion refuses."""
    text = await asyncio.to_thread(extract_text_bytes, data, filename)
    if not text.strip():
        raise ValueError(f"Extracting document {document_id} again produced no text.")
    if len(text) > settings.max_doc_chars:
        raise ValueError(
            f"Extracted text exceeds the {settings.max_doc_chars}-char ingestion limit."
        )
    return text


@dataclass(frozen=True)
class Reprocessed:
    """What reprocessing one document did, in numbers an operator can check."""

    document_id: int
    changed: bool
    chars_before: int
    chars_after: int
    chunks_after: int


async def reprocess_document(conn, document_id: int, *, staff_id=None) -> Reprocessed:
    """Extract a stored original again with today's extractors, in place.

    For when an extractor was wrong and the corpus inherited it: the Planalto's
    struck-through, revoked wording, which reached the text as if it were in
    force. Ingesting the same file again would not repair it, because documents
    are deduplicated by the hash of their extracted text: the corrected text
    hashes differently, so it would arrive as a second document beside the
    wrong one. This rewrites the same row instead, from the original kept on
    disk, so nothing is fetched again and nothing the audit log points at moves.

    Text, chunks and the audit entry commit together. When the extraction comes
    out identical nothing is written and nothing is spent on embeddings.
    """
    source = await get_document_source(conn, document_id, Scope.unrestricted())
    if source is None:
        raise ValueError(f"Document {document_id} has no stored original to reprocess.")
    data = await document_storage.read(
        source.source_storage_key, source.source_wrapped_key,
        raw_sha256=source.source_sha256,
    )
    if data is None:
        raise ValueError(f"The stored original of document {document_id} is missing.")
    record = await get_document(conn, document_id, Scope.unrestricted())
    if record is None:
        raise ValueError(f"Document {document_id} does not exist.")

    text = await _extract_bounded(data, source.source_filename, document_id)
    if text == record.full_text:
        return Reprocessed(document_id, False, len(record.full_text), len(text), 0)

    chunks = chunk_text(text)
    embeddings = await embed_texts(chunks)
    if embeddings is None:
        raise RuntimeError("Failed to generate embeddings, document left as it was.")

    try:
        await replace_document_text(
            conn, document_id,
            full_text=text,
            content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            chunks=chunks, embeddings=embeddings, commit=False,
        )
        await log_access(
            conn, action="reprocess", staff_id=staff_id,
            document_ids=[document_id], commit=False,
        )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise

    # Answers cached from the old text may quote exactly what was removed.
    await cache.invalidate_answers()
    logger.info(
        "Document reprocessed: id=%s, %d -> %d chars, %d chunks",
        document_id, len(record.full_text), len(text), len(chunks),
    )
    return Reprocessed(document_id, True, len(record.full_text), len(text), len(chunks))


RefreshOutcome = Literal["unchanged", "updated", "needs_review"]


@dataclass(frozen=True)
class Refreshed:
    """What comparing one document with a newer original did."""

    document_id: int
    title: str
    outcome: RefreshOutcome
    chars_before: int
    chars_after: int
    chunks_after: int = 0

    @property
    def change(self) -> float:
        """How much the text grew (positive) or shrank, as a share of before."""
        return (self.chars_after - self.chars_before) / max(self.chars_before, 1)


async def refresh_from_original(conn, document_id: int, data: bytes, filename: str, *,
                                source_url: str, max_change: float, force: bool = False,
                                staff_id=None) -> Refreshed:
    """Replace a document's text with a newer original's, in place, when it changed.

    The sibling of `reprocess_document`: that one extracts the stored original
    again, this one takes a new original, the official page as it reads today.
    Both land on the same id for the same reason. A new original also replaces
    the stored one, so /arquivo hands out the version the text came from.

    Nothing is written when the text is identical, or when it changed by more
    than `max_change` (a share of the stored text) without `force`: the caller
    decides what an unchanged or held document means. When it is written, text,
    chunks, original, the page it came from and the audit entry commit together.
    """
    record = await get_document(conn, document_id, Scope.unrestricted())
    if record is None:
        raise LookupError(f"Document {document_id} does not exist.")
    text = await _extract_bounded(data, filename, document_id)
    before = len(record.full_text)

    if text == record.full_text:
        return Refreshed(document_id, record.title, "unchanged", before, len(text))
    held = Refreshed(document_id, record.title, "needs_review", before, len(text))
    if abs(held.change) > max_change and not force:
        return held

    chunks = chunk_text(text)
    embeddings = await embed_texts(chunks)
    if embeddings is None:
        raise RuntimeError("Failed to generate embeddings, document left as it was.")

    raw_sha = hashlib.sha256(data).hexdigest()
    stored = await document_storage.store(data, raw_sha256=raw_sha, filename=filename)
    # The previous original stays where it is. Storage is content-addressed, so
    # another document may share that file, and removing one follows the
    # removal service's rules rather than being decided here in passing.
    try:
        await replace_document_text(
            conn, document_id,
            full_text=text,
            content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            chunks=chunks, embeddings=embeddings, commit=False,
        )
        await replace_document_original(
            conn, document_id,
            source_filename=filename, source_storage_key=stored.key,
            source_sha256=raw_sha, source_size_bytes=len(data),
            source_wrapped_key=stored.wrapped_key or None,
            source_kek_version=stored.kek_version or None, commit=False,
        )
        await mark_source_checked(conn, document_id, source_url=source_url, commit=False)
        await log_access(
            conn, action="refresh", staff_id=staff_id,
            document_ids=[document_id], commit=False,
        )
        await conn.commit()
    except Exception:
        await conn.rollback()
        if stored.newly_created:
            await document_storage.remove(stored.key, stored.wrapped_key)
        raise

    # Answers cached from the old text may quote exactly what changed.
    await cache.invalidate_answers()
    logger.info(
        "Document refreshed from a newer original: id=%s, %d -> %d chars, %d chunks",
        document_id, before, len(text), len(chunks),
    )
    return Refreshed(document_id, record.title, "updated", before, len(text), len(chunks))


async def ingest_file(conn, path, title=None, doc_type=None, matter=None, uploaded_by=None):
    """Ingest a local file. Kept for the administrative CLI."""
    size = os.path.getsize(path)
    if size > settings.max_doc_mb * 1024 * 1024:
        raise ValueError(
            f"File exceeds the {settings.max_doc_mb} MB ingestion limit ({size} bytes)."
        )
    with open(path, "rb") as file:
        data = file.read()
    return await ingest_bytes(
        conn,
        data,
        filename=os.path.basename(path),
        title=title,
        doc_type=doc_type,
        matter=matter,
        uploaded_by=uploaded_by,
    )


# What an archived attachment is stored as. The type was read from its magic
# bytes when it was uploaded; the name is ours, never the sender's.
_MEDIA_EXTENSIONS = {
    "image/jpeg": ".jpg", "image/png": ".png", "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/ogg": ".ogg",
    "audio/webm": ".webm", "audio/aac": ".aac",
}


@dataclass(frozen=True)
class PreparedMedia:
    """An attachment ready to be archived: its text chunked and embedded and
    its original stored, sealed as every original is, with nothing yet in
    the database. `write_media` makes it a document; `discard_media` takes
    the stored original back when it never becomes one."""

    text: str
    chunks: list[str]
    embeddings: list[list[float]]
    stored: document_storage.StoredOriginal
    source_sha256: str
    source_filename: str
    source_size_bytes: int


def _media_limit(media_type: str) -> int:
    if media_type.startswith("image/"):
        return settings.panel_photo_max_bytes
    return settings.panel_audio_max_bytes


async def prepare_media(*, data: bytes, media_type: str, text: str, staff) -> PreparedMedia:
    """The slow half of archiving an attachment, the network calls
    (embeddings, the bucket), done before any lock is taken, so an erasure
    never waits on them. Raises PermissionError for a rank that may not
    archive, ValueError for a type, size or text out of bounds."""
    from core.services.authorization import decide

    if not decide(staff, "document.ingest").allowed:
        raise PermissionError("document.ingest refused")
    extension = _MEDIA_EXTENSIONS.get(media_type)
    if extension is None or not data or len(data) > _media_limit(media_type):
        raise ValueError("Media type or size out of bounds.")
    if not text.strip() or len(text) > settings.max_doc_chars:
        raise ValueError("Media text empty or over the ingestion limit.")
    chunks = chunk_text(text)
    embeddings = await embed_texts(chunks)
    if embeddings is None:
        raise RuntimeError("Failed to generate embeddings, document NOT stored.")
    digest = hashlib.sha256(data).hexdigest()
    filename = "anexo" + extension
    # A file of its own: discard_media may take it back, and must never take
    # one another document holds.
    stored = await document_storage.store(data, raw_sha256=digest, filename=filename,
                                          exclusive=True)
    return PreparedMedia(text=text, chunks=chunks, embeddings=embeddings, stored=stored,
                         source_sha256=digest, source_filename=filename,
                         source_size_bytes=len(data))


async def write_media(conn, prepared: PreparedMedia, *, title: str, staff) -> int:
    """The quick half: the document, its chunks and the audit entry, in the
    caller's transaction and never committed here, so it can run inside the
    publication guard. Always a new private document of `staff`: no text
    fingerprint is stored or looked up, so two attachments with the same
    reading are two documents, each its sender's."""
    stored = prepared.stored
    document_id = await create_document(
        conn, title=title, full_text=prepared.text, sha256=None,
        source_filename=prepared.source_filename, source_storage_key=stored.key,
        source_sha256=prepared.source_sha256, source_size_bytes=prepared.source_size_bytes,
        source_wrapped_key=stored.wrapped_key or None,
        source_kek_version=stored.kek_version or None,
        uploaded_by=staff.id, visibility="private", commit=False,
    )
    await add_chunks(conn, document_id, prepared.chunks, prepared.embeddings, commit=False)
    await log_access(conn, action="ingest_media", staff_id=staff.id,
                     document_ids=[document_id], commit=False)
    return document_id


async def discard_media(prepared: PreparedMedia) -> None:
    """Take back an original stored for a document that was never written.
    `prepare_media` stores each one under a name of its own, so the file is
    this preparation's alone."""
    if prepared.stored.newly_created:
        await document_storage.remove(prepared.stored.key, prepared.stored.wrapped_key)


async def ingest_media(conn, *, data: bytes, media_type: str, text: str,
                       title: str, staff) -> int:
    """Archive one attachment as a new private document of `staff`, with
    `text` (its reading or transcript) as the indexed text: both halves in
    one go, for a caller that holds no lock. The panel worker runs them
    apart, with only `write_media` under the publication guard."""
    prepared = await prepare_media(data=data, media_type=media_type, text=text, staff=staff)
    try:
        async with conn.transaction():
            document_id = await write_media(conn, prepared, title=title, staff=staff)
    except BaseException:
        await discard_media(prepared)
        raise
    await cache.invalidate_answers()
    return document_id
