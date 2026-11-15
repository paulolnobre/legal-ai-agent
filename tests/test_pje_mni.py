"""Reading the PJe, and refusing to do the one thing that starts a deadline.

Consulting the teor of a pending intimation *is* ciência under Lei
11.419/2006 art. 5º: the deadline begins at that read. An agent that opened
pending notices to summarise them would start deadlines nobody at the firm
chose to start, silently, possibly overnight. The firm confirmed this reading
on 2026-09-22.

So the first test below is the one that matters, and it is written to survive
a refactor: it reads the module's own source and fails if the forbidden
operation names appear anywhere in it. A test that only checked "we do not
call it today" passes the moment somebody adds the call.
"""
import asyncio
import base64
import inspect
from types import SimpleNamespace

import httpx
import pytest

from core.config import settings
from core.handlers import legal_message
from core.services import channels
from core.integrations import pje_mni
from core.models import StaffMember
from core.services import pje as pje_service

pytestmark = pytest.mark.usefixtures(
    "isolate_message_rate_limits", "pending_store", "callback_store",
)

_LAWYER = StaffMember(id=9, chat_id="900", name="Caio", role="advogado")
_NUMBER = "0710957-85.2024.8.14.0301"


def _soap(inner: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<S:Envelope xmlns:S="http://schemas.xmlsoap.org/soap/envelope/"><S:Body>'
        f"{inner}</S:Body></S:Envelope>"
    ).encode()


# ── the rule the whole integration exists to respect ─────────────────────────


def test_no_operation_that_gives_ciencia_appears_in_the_module():
    """The guard names the forbidden thing rather than describing it, so it
    still bites after somebody refactors around it. Reading the teor of a
    pending intimation starts the deadline; confirming receipt gives ciência
    outright; filing a manifestação acts in a real process."""
    source = inspect.getsource(pje_mni)
    body = source.split('FORBIDDEN = (', 1)[1].split(")", 1)[1]

    for operation in ("consultarTeorComunicacao", "confirmarRecebimento",
                      "entregarManifestacaoProcessual", "criarProcesso"):
        assert operation not in body, (
            f"{operation} appears in core/integrations/pje_mni.py. Reading the "
            "teor of a pending intimation is ciência under Lei 11.419/2006 "
            "art. 5º and starts the deadline; the others act in the process. "
            "None of them belongs to a read-only integration."
        )


def test_the_forbidden_list_still_names_all_four():
    """A guard emptied of its contents guards nothing."""
    assert set(pje_mni.FORBIDDEN) == {
        "consultarTeorComunicacao", "confirmarRecebimento",
        "entregarManifestacaoProcessual", "criarProcesso",
    }


def test_building_an_envelope_for_a_forbidden_operation_is_refused():
    for operation in pje_mni.FORBIDDEN:
        with pytest.raises(pje_mni.MNIError, match="not an operation"):
            pje_mni._envelope(operation, "<x/>")


def test_a_pending_notice_carries_no_teor():
    """Not an omission: the field does not exist, so no later code can fill it
    from a call that would have started the deadline."""
    fields = set(pje_mni.PendingNotice.__dataclass_fields__)

    assert not fields & {"teor", "conteudo", "texto", "body", "content"}


def test_the_warning_explains_why_the_agent_stops_short():
    """A lawyer who does not know why will ask the agent to go further."""
    message = pje_mni.deadline_warning([
        pje_mni.PendingNotice(notice_id="1", process_number=_NUMBER,
                              kind="Intimação", made_available="20260922"),
    ])

    assert "11.419" in message and "art. 5º" in message
    assert "ciência" in message and "prazo" in message


def test_with_nothing_pending_the_message_says_so_plainly():
    assert "não tem intimações pendentes" in pje_mni.deadline_warning([]).casefold()


# ── the parser, against a hostile answer ─────────────────────────────────────


@pytest.fixture
def court(monkeypatch):
    state = SimpleNamespace(requests=[], response=None)

    def handler(request):
        state.requests.append(request)
        return state.response or httpx.Response(200, content=_soap("<ok/>"))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(pje_mni, "_http", lambda: client)
    monkeypatch.setattr(settings, "pje_enabled", True)
    monkeypatch.setattr(settings, "pje_mni_url", "https://pje.exemplo.jus.br/mni")
    return state


def test_a_document_declaring_entities_is_refused(court):
    """The billion laughs. The standard parser expands internal entities, and
    a byte-level check for `<!DOCTYPE` is bypassed by a UTF-16 document, which
    is why this is `defusedxml` and not a regular expression."""
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
        '<!ENTITY lol2 "&lol;&lol;&lol;">]><x>&lol2;</x>'
    ).encode()

    with pytest.raises(pje_mni.MNIError, match="formato"):
        pje_mni._parse(bomb)


def test_a_document_pulling_in_a_local_file_is_refused():
    """XXE. The classic way a parser is asked to read /etc/passwd."""
    xxe = (
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
        "<x>&e;</x>"
    ).encode()

    with pytest.raises(pje_mni.MNIError):
        pje_mni._parse(xxe)


def test_an_answer_larger_than_the_cap_is_refused_before_it_is_parsed():
    with pytest.raises(pje_mni.MNIError, match="grande demais"):
        pje_mni._parse(b"x" * (pje_mni.MAX_RESPONSE_BYTES + 1))


def test_an_unparseable_answer_says_so_rather_than_crashing():
    with pytest.raises(pje_mni.MNIError, match="não consegui ler"):
        pje_mni._parse(b"isto nao e xml")


def test_a_credential_with_xml_in_it_cannot_break_out_of_the_envelope(court):
    """A password is somebody else's input as far as this envelope is
    concerned, and one containing `</tip:senhaConsultante>` would otherwise
    rewrite the request."""
    asyncio.run(pje_mni.consultar_processo(
        cpf="123", password='</tip:senhaConsultante><evil/>', number=_NUMBER,
    ))

    sent = court.requests[0].content.decode()
    assert "<evil/>" not in sent
    assert "&lt;evil/&gt;" in sent


# ── what comes back ──────────────────────────────────────────────────────────


def test_a_process_is_read_with_its_movements_and_documents(court):
    court.response = httpx.Response(200, content=_soap(
        "<processo><dadosBasicos><siglaTribunal>TJPA</siglaTribunal>"
        "<classeProcessual>Procedimento Comum</classeProcessual></dadosBasicos>"
        "<movimento><dataHora>20260901</dataHora><descricao>Distribuído</descricao>"
        "</movimento>"
        "<documento><idDocumento>d1</idDocumento><descricao>Petição inicial</descricao>"
        "<dataHora>20260901</dataHora><mimetype>application/pdf</mimetype></documento>"
        "</processo>"
    ))

    found = asyncio.run(pje_mni.consultar_processo(
        cpf="1", password="2", number=_NUMBER,
    ))

    assert found.court == "TJPA"
    assert [m.description for m in found.movements] == ["Distribuído"]
    assert [d.document_id for d in found.documents] == ["d1"]


def test_a_refusal_by_the_court_is_told_apart_from_an_outage(court):
    court.response = httpx.Response(200, content=_soap(
        "<resposta><sucesso>false</sucesso>"
        "<mensagem>Consultante não é parte no processo</mensagem></resposta>"
    ))

    with pytest.raises(pje_mni.MNIRefused, match="não é parte"):
        asyncio.run(pje_mni.consultar_processo(cpf="1", password="2", number=_NUMBER))


def test_a_number_that_is_not_a_cnj_number_never_reaches_the_court(court):
    with pytest.raises(ValueError):
        asyncio.run(pje_mni.consultar_processo(cpf="1", password="2", number="123"))

    assert court.requests == []


def test_a_document_is_decoded_and_capped(court):
    court.response = httpx.Response(200, content=_soap(
        "<processo><documento><idDocumento>d1</idDocumento>"
        f"<conteudo>{base64.b64encode(b'conteudo real').decode()}</conteudo>"
        "<mimetype>application/pdf</mimetype></documento></processo>"
    ))

    content, mime = asyncio.run(pje_mni.documento(
        cpf="1", password="2", number=_NUMBER, document_id="d1", max_bytes=1024,
    ))

    assert content == b"conteudo real" and mime == "application/pdf"


def test_a_document_over_the_archive_limit_is_refused_after_decoding(court):
    """Base64 inflates by a third, so something that passed the response cap
    can still be larger than the archive accepts."""
    court.response = httpx.Response(200, content=_soap(
        "<processo><documento><idDocumento>d1</idDocumento>"
        f"<conteudo>{base64.b64encode(b'x' * 4096).decode()}</conteudo>"
        "</documento></processo>"
    ))

    with pytest.raises(pje_mni.MNIError, match="limite"):
        asyncio.run(pje_mni.documento(
            cpf="1", password="2", number=_NUMBER, document_id="d1", max_bytes=1024,
        ))


def test_asking_for_one_document_does_not_return_another(court):
    court.response = httpx.Response(200, content=_soap(
        "<processo><documento><idDocumento>outro</idDocumento>"
        f"<conteudo>{base64.b64encode(b'errado').decode()}</conteudo>"
        "</documento></processo>"
    ))

    with pytest.raises(pje_mni.MNIError, match="não encontrei"):
        asyncio.run(pje_mni.documento(
            cpf="1", password="2", number=_NUMBER, document_id="d1", max_bytes=1024,
        ))


# ── whose credential is spent ────────────────────────────────────────────────


@pytest.fixture
def enrolled(monkeypatch):
    from datetime import datetime
    from core.repositories.pje_credentials import Credential

    state = SimpleNamespace(
        credentials={9: ("11122233344", "senha-do-caio"),
                     5: ("99988877766", "senha-da-ana")},
        used=[], touched=[], ingested=[],
    )

    async def get_credential(_conn, staff_id):
        found = state.credentials.get(staff_id)
        if not found:
            return None
        return Credential(staff_id=staff_id, cpf=found[0], court="TJPA",
                          enrolled_at=datetime.now())

    async def get_password(_conn, staff_id):
        found = state.credentials.get(staff_id)
        return found[1] if found else None

    async def touch(_conn, staff_id):
        state.touched.append(staff_id)

    async def consultar_processo(*, cpf, password, number, with_documents=True):
        state.used.append((cpf, password))
        return pje_mni.Process(number=number, documents=[
            pje_mni.Document(document_id="d1", kind="Petição",
                             description="Inicial", filed_at="20260901"),
        ])

    async def avisos_pendentes(*, cpf, password):
        state.used.append((cpf, password))
        return []

    async def documento(*, cpf, password, number, document_id, max_bytes):
        state.used.append((cpf, password))
        return b"bytes do documento", "application/pdf"

    async def ingest_document(_conn, data, **kwargs):
        state.ingested.append((data, kwargs))
        return SimpleNamespace(document_id=55, chunk_count=3, text="...")

    monkeypatch.setattr(pje_service, "get_credential", get_credential)
    monkeypatch.setattr(pje_service, "get_password", get_password)
    monkeypatch.setattr(pje_service, "touch", touch)
    monkeypatch.setattr(pje_service, "ingest_document", ingest_document)
    monkeypatch.setattr(pje_service.pje_mni, "consultar_processo", consultar_processo)
    monkeypatch.setattr(pje_service.pje_mni, "avisos_pendentes", avisos_pendentes)
    monkeypatch.setattr(pje_service.pje_mni, "documento", documento)
    return state


def test_each_person_spends_their_own_credential(enrolled):
    asyncio.run(pje_service.process("conn", 9, _NUMBER))
    asyncio.run(pje_service.process("conn", 5, _NUMBER))

    assert enrolled.used == [
        ("11122233344", "senha-do-caio"), ("99988877766", "senha-da-ana"),
    ]


def test_somebody_who_never_enrolled_is_told_so_and_nothing_is_called(enrolled):
    with pytest.raises(pje_service.NotEnrolled):
        asyncio.run(pje_service.process("conn", 404, _NUMBER))

    assert enrolled.used == []


def test_a_document_from_a_case_lands_in_that_person_own_folder(enrolled):
    """A peça from somebody's case on the firm's shelf is the same leak the
    folders were built to prevent, arriving by a new door."""
    document = pje_mni.Document(document_id="d1", kind="Petição",
                                description="Inicial", filed_at="20260901")

    result = asyncio.run(pje_service.import_document(
        "conn", 9, number=_NUMBER, document=document,
    ))

    _data, kwargs = enrolled.ingested[0]
    assert kwargs["visibility"] == "private"
    assert kwargs["uploaded_by"] == 9
    assert result.document_id == 55


def test_the_imported_file_is_named_so_a_person_recognises_it(enrolled):
    document = pje_mni.Document(document_id="d1", kind="Petição",
                                description="Inicial", filed_at="20260901",
                                mime_type="application/pdf")

    asyncio.run(pje_service.import_document(
        "conn", 9, number=_NUMBER, document=document,
    ))

    name = enrolled.ingested[0][1]["filename"]
    assert name.startswith("Inicial") and name.endswith(".pdf")
    assert "d1" not in name, "the court's internal id is not a name"


# ── the commands ─────────────────────────────────────────────────────────────


@pytest.fixture
def chat(monkeypatch):
    state = SimpleNamespace(sent=[], notices=[], process=None, imported=None,
                            error=None)

    async def get_staff(_conn, _chat_id):
        return _LAWYER

    async def send(_chat_id, message, reply_markup=None):
        state.sent.append(message)
        return 1

    async def typing(*_args, **_kwargs):
        return None

    async def pending_notices(_conn, _staff_id):
        if state.error:
            raise state.error
        return state.notices

    async def process(_conn, _staff_id, _number):
        if state.error:
            raise state.error
        return state.process

    async def import_document(_conn, staff_id, *, number, document):
        state.imported = (staff_id, number, document)
        return SimpleNamespace(document_id=55, chunk_count=3)

    legal_message._LAST_PROCESS_LISTING.clear()
    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(channels, "send_typing", typing)
    monkeypatch.setattr(legal_message.pje_service, "pending_notices", pending_notices)
    monkeypatch.setattr(legal_message.pje_service, "process", process)
    monkeypatch.setattr(legal_message.pje_service, "import_document", import_document)
    return state


def _command(text: str):
    update = {"update_id": 1, "message": {
        "chat": {"id": 900, "type": "private"}, "from": {"id": 900}, "text": text,
    }}
    asyncio.run(legal_message.process_legal_message("connection", update))


def test_pending_notices_are_listed_with_the_reason_they_are_not_opened(chat):
    chat.notices = [
        pje_mni.PendingNotice(notice_id="1", process_number=_NUMBER,
                              kind="Intimação", made_available="22/09/2026"),
    ]

    _command("/intimacoes")

    assert _NUMBER in chat.sent[-1]
    assert "11.419" in chat.sent[-1], "the message says why it stops short"


def test_somebody_not_enrolled_is_pointed_at_the_enrolment_command(chat):
    chat.error = pje_service.NotEnrolled("none")

    _command("/intimacoes")

    assert "/pje" in chat.sent[-1]


def test_a_process_lists_its_documents_numbered(chat):
    chat.process = pje_mni.Process(number=_NUMBER, court="TJPA", documents=[
        pje_mni.Document(document_id="d1", kind="Petição", description="Inicial",
                         filed_at="20260901"),
        pje_mni.Document(document_id="d2", kind="Decisão", description="Liminar",
                         filed_at="20260905"),
    ])

    _command(f"/autos {_NUMBER}")

    assert "1. Inicial" in chat.sent[-1] and "2. Liminar" in chat.sent[-1]
    assert "pasta pessoal" in chat.sent[-1]


def test_bringing_a_document_uses_the_number_from_the_last_listing(chat):
    chat.process = pje_mni.Process(number=_NUMBER, documents=[
        pje_mni.Document(document_id="d1", kind="a", description="Primeiro",
                         filed_at=""),
        pje_mni.Document(document_id="d2", kind="b", description="Segundo",
                         filed_at=""),
    ])
    _command(f"/autos {_NUMBER}")

    _command("/autos trazer 2")

    assert chat.imported is not None
    assert chat.imported[2].document_id == "d2"


def test_a_line_nobody_listed_is_refused(chat):
    chat.process = pje_mni.Process(number=_NUMBER, documents=[
        pje_mni.Document(document_id="d1", kind="a", description="Único", filed_at=""),
    ])
    _command(f"/autos {_NUMBER}")

    _command("/autos trazer 9")

    assert chat.imported is None


def test_asking_to_bring_before_listing_says_to_list(chat):
    _command("/autos trazer 1")

    assert chat.imported is None
    assert "/autos" in chat.sent[-1]


def test_reading_the_pje_is_open_to_every_authorized_rank():
    from core.services import authorization

    for role in ("estagiario", "advogado", "admin", "owner"):
        staff = StaffMember(id=1, chat_id="1", name="x", role=role)
        assert authorization.decide(staff, "pje.read").allowed, role


def test_the_menu_offers_the_two_commands_in_the_firms_words():
    menu = {entry["command"]: entry["description"]
            for entry in legal_message.command_menu()}

    assert "intimacoes" in menu and "autos" in menu
