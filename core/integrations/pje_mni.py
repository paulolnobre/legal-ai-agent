"""Reading a process from the PJe over the CNJ's MNI, and nothing else.

The MNI (Modelo Nacional de Interoperabilidade, 2.2.2) is a SOAP service every
PJe installation exposes. It offers six operations. This module implements two
of them, and the four it leaves out are left out deliberately rather than for
lack of time.

**`consultarTeorComunicacao` is forbidden here, and it is the important one.**
Reading the teor of a pending intimation *is* ciência under Lei 11.419/2006
art. 5º: the deadline starts counting from that read. An agent that opens
pending notices to summarise them would start deadlines nobody at the firm
decided to start, silently, possibly at three in the morning. The firm
confirmed this reading on 2026-09-22. So the agent lists what is pending and
the lawyer opens it themselves, in the PJe, as a deliberate act.

`confirmarRecebimento` and `entregarManifestacaoProcessual` are absent for the
same shape of reason: one gives ciência explicitly, the other files a document
in a real process. Neither belongs to a read-only integration, and a scope
that is never written cannot be reached by a bug meanwhile.

`FORBIDDEN` names all four, and a test asserts that none of them appears in
this module's source. A guard that names the forbidden thing is the one that
still works after somebody refactors around it.

**On validation.** This is written against the MNI 2.2.2 specification with
recorded responses, not against a live court. The exact namespacing of request
elements varies between installations, so the envelope below must be checked
against the TJPA's own WSDL before this is switched on. It is behind
`PJE_ENABLED` for that reason.
"""
from __future__ import annotations

import base64
import binascii
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from xml.etree.ElementTree import Element

import httpx
from defusedxml.ElementTree import ParseError, fromstring

from core.config import settings
from core.integrations.datajud import normalize_process_number
from core.metrics import observe_external
from core.observability import traced

logger = logging.getLogger(__name__)

# The operations this integration must never perform, and why, in one place.
# `consultarTeorComunicacao` gives ciência and starts the deadline;
# `confirmarRecebimento` gives it explicitly; `entregarManifestacaoProcessual`
# files a document in a real process; `criarProcesso` opens one.
FORBIDDEN = (
    "consultarTeorComunicacao",
    "confirmarRecebimento",
    "entregarManifestacaoProcessual",
    "criarProcesso",
)

_ENVELOPE_NS = "http://schemas.xmlsoap.org/soap/envelope/"
_SERVICE_NS = "http://www.cnj.jus.br/servico-intercomunicacao-2.2.2/"
_TYPES_NS = "http://www.cnj.jus.br/intercomunicacao-2.2.2"

# A court answering with megabytes is a court answering wrongly, and the
# parser reads whatever it is given into memory before anything checks it.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

_client: httpx.AsyncClient | None = None


class MNIError(RuntimeError):
    """A failure worth showing somebody, never carrying a credential."""


class MNIRefused(MNIError):
    """The court rejected the credential, or the person is not a party."""


@dataclass(frozen=True)
class Document:
    """One document in the process, as the court lists it."""

    document_id: str
    kind: str
    description: str
    filed_at: str
    mime_type: str = ""

    def described(self) -> str:
        label = self.description or self.kind or "documento"
        return f"{label}" + (f" ({self.filed_at})" if self.filed_at else "")


@dataclass(frozen=True)
class Movement:
    """One movement, newest first when a process is rendered."""

    happened_at: str
    description: str


@dataclass(frozen=True)
class Process:
    """What the court returned about one process."""

    number: str
    court: str = ""
    matter: str = ""
    distributed_at: str = ""
    secrecy: str = ""
    movements: list[Movement] = field(default_factory=list)
    documents: list[Document] = field(default_factory=list)


@dataclass(frozen=True)
class PendingNotice:
    """One pending intimation, WITHOUT its teor.

    The teor is absent by construction, not by omission: fetching it would be
    ciência and would start the deadline. What is here is enough to know that
    something is waiting and to go and open it in the PJe.
    """

    notice_id: str
    process_number: str
    kind: str
    made_available: str
    court: str = ""


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))
    return _client


def configured() -> bool:
    """Whether there is an endpoint to talk to."""
    return bool(settings.pje_enabled and (settings.pje_mni_url or "").strip())


def _escape(value: str) -> str:
    """A value going into the envelope. The credential goes through here too."""
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;"))


def _envelope(operation: str, body: str) -> str:
    if operation in FORBIDDEN:
        # Belt and braces: the operation names are not built dynamically
        # anywhere here, so this can only fire if somebody adds a caller.
        raise MNIError(f"{operation} is not an operation this agent performs")
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<soap:Envelope xmlns:soap="{_ENVELOPE_NS}" xmlns:ser="{_SERVICE_NS}" '
        f'xmlns:tip="{_TYPES_NS}"><soap:Header/><soap:Body>'
        f"<ser:{operation}>{body}</ser:{operation}>"
        f"</soap:Body></soap:Envelope>"
    )


def _parse(raw: bytes) -> Element:
    """Parse a court's answer, refusing the shapes that are attacks.

    `defusedxml` rather than the standard parser, and this is a considered
    dependency rather than a reflex. The stdlib parser expands internal
    entities, which turns a small document into gigabytes of memory, and the
    obvious hand-rolled guard, refusing a response whose bytes contain
    `<!DOCTYPE`, is bypassed by the same document encoded as UTF-16. A court
    endpoint is not hostile, but a compromised or impersonated one is exactly
    the case this has to survive.

    The size cap stays in front of it: the parser reads whatever it is handed
    into memory, and a court answering with megabytes is answering wrongly.
    """
    if len(raw) > MAX_RESPONSE_BYTES:
        raise MNIError("o tribunal respondeu com um documento grande demais")
    try:
        return fromstring(raw)
    except ParseError as exc:
        raise MNIError("não consegui ler a resposta do tribunal") from exc
    except Exception as exc:  # DTDForbidden, EntitiesForbidden and friends
        logger.error("An MNI response was refused by the hardened XML parser")
        raise MNIError("a resposta do tribunal veio em um formato que não aceito") from exc


def _text(node, *names: str) -> str:
    """The first of these attributes or child tags that carries anything.

    MNI responses differ between installations in whether a field arrives as
    an attribute or an element, so both are tried rather than assuming one.
    """
    for name in names:
        if (value := (node.get(name) or "").strip()):
            return value
    for name in names:
        for child in node.iter():
            if child.tag.rsplit("}", 1)[-1] == name and (child.text or "").strip():
                return (child.text or "").strip()
    return ""


def _local(node) -> str:
    return node.tag.rsplit("}", 1)[-1]


def _find_all(root, name: str):
    return [node for node in root.iter() if _local(node) == name]


def _first(root, name: str):
    for node in root.iter():
        if _local(node) == name:
            return node
    return None


def _fault(root) -> str:
    """The court's own words when it refuses, bounded."""
    for name in ("faultstring", "mensagem", "Reason", "Text"):
        node = _first(root, name)
        if node is not None and (node.text or "").strip():
            return (node.text or "").strip()[:300]
    return ""


async def _call(operation: str, body: str) -> Element:
    if not configured():
        raise MNIError("o acesso ao PJe não está configurado neste servidor")
    started = time.perf_counter()
    try:
        response = await _http().post(
            settings.pje_mni_url,
            content=_envelope(operation, body).encode("utf-8"),
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": operation,
            },
        )
    except httpx.HTTPError as exc:
        observe_external("pje_mni", "error", time.perf_counter() - started)
        raise MNIError("o PJe não respondeu") from exc

    root = _parse(response.content)
    if response.status_code >= 400:
        observe_external("pje_mni", "error", time.perf_counter() - started)
        detail = _fault(root)
        if response.status_code in (401, 403):
            raise MNIRefused(detail or "o PJe recusou as suas credenciais")
        raise MNIError(detail or f"o PJe respondeu HTTP {response.status_code}")

    success = _text(root, "sucesso")
    if success.casefold() == "false":
        observe_external("pje_mni", "refused", time.perf_counter() - started)
        message = _text(root, "mensagem") or _fault(root)
        raise MNIRefused(message or "o PJe recusou a consulta")
    observe_external("pje_mni", "success", time.perf_counter() - started)
    return root


@traced(name="pje.consultar_processo", run_type="tool")
async def consultar_processo(*, cpf: str, password: str, number: str,
                             with_documents: bool = True) -> Process:
    """One process, with its movements and the list of its documents.

    The document *list* is not the documents: their content is fetched one at
    a time, deliberately, by `documento`. A process can carry hundreds.
    """
    digits = normalize_process_number(number)
    body = (
        f"<tip:idConsultante>{_escape(cpf)}</tip:idConsultante>"
        f"<tip:senhaConsultante>{_escape(password)}</tip:senhaConsultante>"
        f"<tip:numeroProcesso>{_escape(digits)}</tip:numeroProcesso>"
        f"<tip:movimentos>true</tip:movimentos>"
        f"<tip:incluirCabecalho>true</tip:incluirCabecalho>"
        f"<tip:incluirDocumentos>{'true' if with_documents else 'false'}"
        f"</tip:incluirDocumentos>"
    )
    root = await _call("consultarProcesso", body)

    movements = [
        Movement(
            happened_at=_text(node, "dataHora", "data"),
            description=_text(node, "descricao", "complemento", "movimentoNacional"),
        )
        for node in _find_all(root, "movimento")
    ]
    documents = [
        Document(
            document_id=_text(node, "idDocumento", "id"),
            kind=_text(node, "tipoDocumento", "tipo"),
            description=_text(node, "descricao", "nome"),
            filed_at=_text(node, "dataHora", "data"),
            mime_type=_text(node, "mimetype", "mimeType"),
        )
        for node in _find_all(root, "documento")
    ]
    header = _first(root, "dadosBasicos") or root
    return Process(
        number=digits,
        court=_text(header, "siglaTribunal", "orgaoJulgador"),
        matter=_text(header, "classeProcessual", "classe"),
        distributed_at=_text(header, "dataAjuizamento"),
        secrecy=_text(header, "nivelSigilo"),
        movements=movements,
        documents=[doc for doc in documents if doc.document_id],
    )


@traced(name="pje.avisos_pendentes", run_type="tool")
async def avisos_pendentes(*, cpf: str, password: str) -> list[PendingNotice]:
    """What is waiting, never what it says.

    This is the operation whose sibling is forbidden. Listing a pending notice
    does not give ciência; reading its teor does, and starts the deadline. So
    the agent says "there are three waiting, on these processes" and the
    lawyer opens them in the PJe themselves.
    """
    body = (
        f"<tip:idRepresentanteProcessual>{_escape(cpf)}</tip:idRepresentanteProcessual>"
        f"<tip:idManifestante>{_escape(cpf)}</tip:idManifestante>"
        f"<tip:senhaManifestante>{_escape(password)}</tip:senhaManifestante>"
    )
    root = await _call("consultarAvisosPendentes", body)
    notices = []
    for node in _find_all(root, "aviso"):
        notice = PendingNotice(
            notice_id=_text(node, "idAviso", "id"),
            process_number=_text(node, "numeroProcesso", "processo"),
            kind=_text(node, "tipoComunicacao", "tipo"),
            made_available=_text(node, "dataDisponibilizacao", "dataHora", "data"),
            court=_text(node, "siglaTribunal"),
        )
        if notice.notice_id or notice.process_number:
            notices.append(notice)
    return notices


@traced(name="pje.documento", run_type="tool")
async def documento(*, cpf: str, password: str, number: str,
                    document_id: str, max_bytes: int) -> tuple[bytes, str]:
    """One document's content, asked for by id, one at a time.

    Deliberately not part of `consultar_processo`: a process can carry
    hundreds of documents, and `incluirDocumentos` with content would pull all
    of them through this server to answer a question about one.

    A document in the case file is not a pending intimation, and fetching it
    gives no ciência. The notice is the thing with the deadline attached, and
    the operation that opens one is not in this module at all.
    """
    digits = normalize_process_number(number)
    body = (
        f"<tip:idConsultante>{_escape(cpf)}</tip:idConsultante>"
        f"<tip:senhaConsultante>{_escape(password)}</tip:senhaConsultante>"
        f"<tip:numeroProcesso>{_escape(digits)}</tip:numeroProcesso>"
        f"<tip:movimentos>false</tip:movimentos>"
        f"<tip:incluirCabecalho>false</tip:incluirCabecalho>"
        f"<tip:incluirDocumentos>true</tip:incluirDocumentos>"
        f"<tip:documento>{_escape(document_id)}</tip:documento>"
    )
    root = await _call("consultarProcesso", body)

    for node in _find_all(root, "documento"):
        if _text(node, "idDocumento", "id") != document_id:
            continue
        encoded = _text(node, "conteudo")
        if not encoded:
            raise MNIError("o tribunal não devolveu o conteúdo desse documento")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise MNIError("o conteúdo do documento veio ilegível") from exc
        # Checked after decoding as well as before parsing: base64 inflates by
        # a third, so a payload that passed the response cap can still be
        # larger than the archive accepts.
        if len(content) > max_bytes:
            raise MNIError(
                f"o documento passa do limite de {max_bytes // (1024 * 1024)} MB"
            )
        return content, _text(node, "mimetype", "mimeType")
    raise MNIError("não encontrei esse documento no processo")


def deadline_warning(notices: list[PendingNotice]) -> str:
    """What the firm is told about pending notices, and what it is not.

    Written here beside the rule rather than in the handler, so the sentence
    that explains why the agent will not open them sits next to the code that
    refuses to.
    """
    if not notices:
        return "Você não tem intimações pendentes de leitura no PJe."
    count = len(notices)
    head = (f"⚠️ Você tem {count} intimação pendente" if count == 1
            else f"⚠️ Você tem {count} intimações pendentes")
    return (
        f"{head} de leitura no PJe.\n\n"
        "Não abro o teor delas de propósito: pela Lei 11.419/2006, art. 5º, "
        "consultar o teor já é ciência e começa a contar o prazo. Quem decide "
        "quando isso acontece é você, no próprio PJe."
    )


def local_datetime(value: str) -> datetime | None:
    """The MNI's `yyyyMMddHHmmss`, when it is that. None when it is anything else."""
    digits = "".join(character for character in value if character.isdigit())
    for size, form in ((14, "%Y%m%d%H%M%S"), (8, "%Y%m%d")):
        if len(digits) >= size:
            try:
                return datetime.strptime(digits[:size], form)
            except ValueError:
                return None
    return None


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


__all__ = [
    "Document", "FORBIDDEN", "MAX_RESPONSE_BYTES", "MNIError", "MNIRefused",
    "Movement", "PendingNotice", "Process", "avisos_pendentes", "close",
    "configured", "consultar_processo", "deadline_warning", "documento",
    "local_datetime",
]
