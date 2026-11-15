"""Spending one lawyer's PJe credential, on their behalf and nobody else's.

Every call here opens that person's own sealed password and no one else's,
which is what makes this safe to offer to ten people: the court decides what
each of them may see, and this code only has to avoid mixing up whose
credential it is holding. That is concentrated in `_credential`.

A document brought in from a process lands in the asker's **personal folder**.
It is client material from a case they act in, and the archive's rule since
the folders landed is that what a person brings is theirs until they share it.
A peça from somebody's case arriving on the firm's shelf would be the same
leak the folders were built to prevent, coming in by a new door.

Nothing here can read the teor of a pending intimation. The operation that
would is not in `core.integrations.pje_mni` at all.
"""
from __future__ import annotations

import logging

from core.config import settings
from core.integrations import pje_mni
from core.repositories import pje_credentials
from core.repositories.pje_credentials import get_credential, get_password, touch
from core.services.ingestion import Ingested, ingest_document

logger = logging.getLogger(__name__)


# What follows "/pje": the words that remove the access and the words that
# ask for a new enrolment while one exists. The handler acts on them and the
# panel's intake lets through only these, so both read the same words.
REMOVE_WORDS = frozenset({"remover", "remove", "esquecer", "sair"})
ENROL_WORDS = frozenset({"cadastrar", "trocar", "recadastrar"})
# The panel's page that opens an enrolment behind the signed-in session: the
# panel's /pje answers with this address, never with a token.
PANEL_PAGE = "/web/pje/"


async def open_enrolment(conn, staff_id: int) -> str:
    """A single-use enrolment link's token for this person, at the configured
    lifetime: the one way the bot's /pje and the panel's page open one."""
    return await pje_credentials.open_enrolment(
        conn, staff_id, ttl_seconds=settings.pje_enrolment_ttl,
    )


def enrolment_minutes() -> int:
    """How long a link lasts, in whole minutes, as the person is told."""
    return max(1, int(settings.pje_enrolment_ttl // 60))


class NotEnrolled(RuntimeError):
    """This person has no PJe access enrolled, or it stopped working."""


async def _credential(conn, staff_id: int) -> tuple[str, str]:
    """This person's CPF and password, or a clear reason there are none."""
    credential = await get_credential(conn, staff_id)
    if credential is None:
        raise NotEnrolled("no PJe credential is enrolled")
    password = await get_password(conn, staff_id)
    if not password:
        raise NotEnrolled("no PJe credential is enrolled")
    return credential.cpf, password


async def process(conn, staff_id: int, number: str) -> pje_mni.Process:
    """One process, as the court shows it to this person."""
    cpf, password = await _credential(conn, staff_id)
    try:
        found = await pje_mni.consultar_processo(
            cpf=cpf, password=password, number=number,
        )
    except pje_mni.MNIRefused as refused:
        raise NotEnrolled(str(refused)) from refused
    await touch(conn, staff_id)
    return found


async def pending_notices(conn, staff_id: int) -> list[pje_mni.PendingNotice]:
    """What is waiting for this person, never what it says."""
    cpf, password = await _credential(conn, staff_id)
    try:
        notices = await pje_mni.avisos_pendentes(cpf=cpf, password=password)
    except pje_mni.MNIRefused as refused:
        raise NotEnrolled(str(refused)) from refused
    await touch(conn, staff_id)
    return notices


async def import_document(conn, staff_id: int, *, number: str,
                          document: pje_mni.Document) -> Ingested:
    """Bring one document from a process into this person's own folder."""
    cpf, password = await _credential(conn, staff_id)
    ceiling = settings.max_doc_mb * 1024 * 1024
    try:
        content, mime_type = await pje_mni.documento(
            cpf=cpf, password=password, number=number,
            document_id=document.document_id, max_bytes=ceiling,
        )
    except pje_mni.MNIRefused as refused:
        raise NotEnrolled(str(refused)) from refused
    await touch(conn, staff_id)
    logger.info(
        "Importing %s bytes from the PJe for staff %s", len(content), staff_id,
    )
    return await ingest_document(
        conn, content,
        filename=_filename(number, document, mime_type),
        uploaded_by=staff_id,
        # Their case, their client's material. Sharing it is a separate act.
        visibility="private",
    )


def _filename(number: str, document: pje_mni.Document, mime_type: str) -> str:
    """A name a person recognises in `/docs`, not the court's internal id."""
    label = (document.description or document.kind or "documento").strip()
    safe = "".join(
        character if character.isalnum() or character in " -_." else " "
        for character in label
    )
    extension = {
        "application/pdf": ".pdf",
        "text/html": ".html",
        "image/jpeg": ".jpg",
        "image/png": ".png",
    }.get((mime_type or document.mime_type or "").split(";")[0].strip(), ".pdf")
    return f"{' '.join(safe.split())[:80]} - {number}{extension}"


__all__ = ["ENROL_WORDS", "NotEnrolled", "PANEL_PAGE", "REMOVE_WORDS", "enrolment_minutes", "import_document",
           "open_enrolment", "pending_notices", "process"]
