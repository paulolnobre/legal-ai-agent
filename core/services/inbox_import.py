"""Bring documents from the firm's bucket into the archive.

The bucket exists because attaching one file at a time to a Telegram chat is
not how a law firm hands over an archive. Somebody drops a folder into it and
this reads what is new.

Everything here treats the bucket as untrusted input, because that is what it
is: a place other people write to. Names never touch the filesystem, the
extension allowlist is the same one a Telegram attachment must pass, the size
is checked before the bytes are fetched and again after, and the content goes
through the same ingestion pipeline with the same extraction and the same
deduplication. The bucket is a delivery route, not a second door with weaker
locks.

Nothing is deleted from the bucket. The firm's copy stays the firm's copy, and
what has been read is remembered here instead, keyed by the file's name and
content together, so a corrected file uploaded over an old name is read again
rather than skipped.
"""
import logging
from dataclasses import dataclass, field

from core.config import settings
from core.integrations import inbox_bucket
from core.repositories import inbox
from core.repositories.documents import MAX_MATTER_CHARS
from core.services.ingestion import DOCUMENT_EXTENSIONS, ingest_bytes
from core.services.legal_assistant.intents import normalize_doc_type

logger = logging.getLogger(__name__)


@dataclass
class ImportReport:
    """What one run did, in the terms the person who asked for it thinks in."""

    # (name, matter, doc_type): what the folders said about each one, so the
    # answer can show the classification instead of the person discovering it
    # later in /detalhes.
    ingested: list[tuple[str, str | None, str | None]] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    already_known: int = 0
    listed: int = 0

    @property
    def touched(self) -> int:
        return (
            len(self.ingested) + len(self.duplicates)
            + len(self.skipped) + len(self.failed)
        )


# Folder names that name a drawer rather than a client. Somebody who keeps
# everything under "Clientes/" did not name a client called Clientes, and
# importing one would put every document in the archive under the same matter.
_GENERIC_FOLDERS = frozenset({
    "clientes", "cliente", "casos", "caso", "processos", "processo",
    "documentos", "documento", "arquivos", "arquivo", "acervo", "geral",
    "diversos", "inbox", "entrada", "uploads", "upload", "escritorio",
    "escritório", "drive", "onedrive", "compartilhado", "shared",
})


def classify_from_path(name: str) -> tuple[str | None, str | None]:
    """Read the matter and the document type out of the folders a file sits in.

    The bucket is synchronised from the firm's Drive, so the folder tree is a
    classification the firm already maintains and keeps up to date. Throwing it
    away and asking somebody to run /editar afterwards is asking them to enter
    the same information twice, and the second time is the one that does not
    happen: every document imported before this landed with no type and no
    matter at all.

    "Clientes/Acme Ltda/Contratos/aditivo.pdf" is read as the matter "Acme Ltda"
    and the type "contrato".

    Two rules, and both are about not inventing:

    The deepest folder becomes the type only when it names one the catalogue
    already uses. Anything else stays part of the path, because a folder called
    "2024" or "Ana" is not a document type and guessing one would put a wrong
    label on every file under it.

    The matter is the deepest folder that names something, so
    "Clientes/Acme/Processo 123" gives "Processo 123" rather than "Acme": the
    deeper folder is the more specific answer, and it is the one somebody chose
    to create. A file at the root of the bucket is classified by nothing, which
    is correct and visible.
    """
    segments = [part.strip() for part in name.split("/")[:-1]]
    segments = [part for part in segments if part]
    if not segments:
        return None, None

    doc_type = normalize_doc_type(segments[-1])
    if doc_type:
        segments = segments[:-1]

    matter = None
    for candidate in reversed(segments):
        if candidate.casefold() in _GENERIC_FOLDERS:
            continue
        # A second type-shaped folder is not a matter either: in
        # "Contratos/Aditivos/x.pdf" neither names a client.
        if normalize_doc_type(candidate):
            continue
        matter = candidate[:MAX_MATTER_CHARS]
        break

    return matter, doc_type


def _unsupported(name: str) -> str | None:
    """Why this object cannot be ingested, or None when it can."""
    base = name.rsplit("/", 1)[-1]
    if not base:
        return "não é um arquivo"
    extension = "." + base.rsplit(".", 1)[-1].lower() if "." in base else ""
    if extension not in DOCUMENT_EXTENSIONS:
        return f"formato {extension or 'sem extensão'} não é aceito"
    return None


async def import_new_documents(conn, *, staff_id: int | None) -> ImportReport:
    """Read what is new in the bucket into the archive, once.

    `staff_id` is None for the scheduled sweep, which has no uploader. Filling
    it with the owner's id would be a small lie in an audit trail that exists
    to be believed.
    """
    if not settings.inbox_enabled:
        raise inbox_bucket.InboxError("a caixa de entrada de documentos está desligada")

    report = ImportReport()
    files = await inbox_bucket.list_files()
    report.listed = len(files)
    if not files:
        return report

    known = await inbox.known_file_ids(conn, [f.file_id for f in files])
    max_bytes = settings.max_doc_mb * 1024 * 1024

    for entry in files:
        if entry.file_id in known:
            report.already_known += 1
            continue
        if report.touched >= settings.inbox_max_files_per_run:
            # A folder somebody just synchronised must not become one unbounded
            # run. What is left is still new next time.
            break

        reason = _unsupported(entry.name)
        if reason is None and entry.size > max_bytes:
            reason = f"tem {entry.size // (1024 * 1024)} MB, acima do limite"
        if reason:
            # Deliberately not written down. A skip for format or size is
            # decided from the listing alone, costs nothing to reach, and is a
            # judgement about the rules rather than about the file: the day
            # .html joined the allowlist, every .html already recorded as
            # skipped would have stayed skipped forever, because the ledger
            # would have said it was dealt with. Only outcomes that cost a
            # download, or that produced a document, are remembered.
            report.skipped.append((entry.name, reason))
            continue

        try:
            data = await inbox_bucket.download(entry.key, max_bytes=max_bytes)
        except inbox_bucket.InboxError as exc:
            # Not written down. A download that failed says nothing about the
            # file: the transfer may have been interrupted, the bucket may have
            # been busy, or the fault may be ours, which is what it was the day
            # a comma in a file name was sent unencoded and the bucket refused
            # every name containing one. Recording it would have made those
            # files permanently unreachable by the fix written for them.
            report.failed.append((entry.name, str(exc)))
            continue

        try:
            matter, doc_type = classify_from_path(entry.name)
            document_id, chunk_count = await ingest_bytes(
                conn,
                data,
                filename=entry.name.rsplit("/", 1)[-1],
                doc_type=doc_type,
                matter=matter,
                uploaded_by=staff_id,
                # The shared inbox is the firm's shelf, not one person's folder.
                visibility="firm",
            )
        except Exception as exc:
            # This one is written down, and the asymmetry with the download
            # above is the point: a file whose text cannot be extracted fails
            # identically every run, because the fault is in the file.
            logger.exception("Could not ingest %s from the bucket", entry.name)
            report.failed.append((entry.name, "não consegui extrair o texto"))
            await inbox.record(
                conn, file_id=entry.file_id, file_name=entry.name,
                outcome="failed", detail=str(exc)[:200],
            )
            continue

        if chunk_count:
            report.ingested.append((entry.name, matter, doc_type))
            outcome = "ingested"
        else:
            # Ingestion deduplicates by the sha256 of the extracted text, so a
            # file already in the archive under another name lands here.
            report.duplicates.append(entry.name)
            outcome = "duplicate"
        await inbox.record(
            conn, file_id=entry.file_id, file_name=entry.name,
            outcome=outcome, document_id=document_id,
        )

    logger.info(
        "Bucket import: listed=%d new=%d ingested=%d duplicate=%d skipped=%d failed=%d",
        report.listed, report.touched, len(report.ingested),
        len(report.duplicates), len(report.skipped), len(report.failed),
    )
    return report


__all__ = ["ImportReport", "import_new_documents"]
