"""Bare commands collect their missing identifier without involving the model."""
import asyncio
from types import SimpleNamespace

import pytest

from core.handlers import legal_message
from core.services import channels
from core.models import StaffMember
from core.repositories import staff as staff_repository

pytestmark = pytest.mark.usefixtures("isolate_message_rate_limits")

_OWNER = StaffMember(id=1, chat_id="123", name="Paulo", role="owner")
_LAWYER = StaffMember(id=6, chat_id="600", name="Lia", role="advogado")
_TARGET = StaffMember(id=7, chat_id="700", name="Ana", role="advogado")
_DOCUMENT = SimpleNamespace(
    id=42,
    title="Contrato",
    doc_type="contrato",
    matter="Acme",
    created_at=None,
)


def _update(chat_id: int, text: str) -> dict:
    return {
        "update_id": 900,
        "message": {
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "first_name": "Pessoa"},
            "text": text,
        },
    }


def _base(monkeypatch: pytest.MonkeyPatch, staff: StaffMember):
    sent: list[tuple[int | str, str]] = []

    async def get_staff(*args, **kwargs):
        return staff

    async def send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text))

    async def users(*args, **kwargs):
        return [_OWNER, _TARGET]

    async def get_target(_conn, identifier):
        return _TARGET if identifier == _TARGET.id else None

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "get_staff_by_id", get_target)
    monkeypatch.setattr(legal_message, "list_staff", users)
    monkeypatch.setattr(legal_message, "_send", send)
    return sent


def _turn(staff: StaffMember, text: str):
    assert staff.chat_id is not None, "the staff fixtures above all carry a chat id"
    return legal_message.process_legal_message(
        "connection", _update(int(staff.chat_id), text)
    )


def test_bare_promote_lists_eligible_users_and_the_next_id_completes_it(monkeypatch, pending_store):
    sent = _base(monkeypatch, _OWNER)
    changed = []

    async def set_role(_conn, identifier, role):
        changed.append((identifier, role))
        return _TARGET.model_copy(update={"role": role})

    monkeypatch.setattr(legal_message, "set_staff_role", set_role)

    asyncio.run(_turn(_OWNER, "/promover"))

    assert "Qual é o ID" in sent[0][1]
    assert "[7] Ana (advogado)" in sent[0][1]
    assert "Paulo" not in sent[0][1], "owner accounts are not eligible targets"
    assert changed == []

    asyncio.run(_turn(_OWNER, "[7]"))

    assert changed == [(7, "admin")]
    assert not pending_store.waiting(123)


def test_invalid_staff_id_keeps_the_context_open(monkeypatch, pending_store):
    _base(monkeypatch, _OWNER)
    changed = []

    async def set_role(_conn, identifier, role):
        changed.append((identifier, role))
        return _TARGET.model_copy(update={"role": role})

    monkeypatch.setattr(legal_message, "set_staff_role", set_role)

    asyncio.run(_turn(_OWNER, "/promover"))
    asyncio.run(_turn(_OWNER, "não sei"))

    assert pending_store.waiting(123)
    assert changed == []

    asyncio.run(_turn(_OWNER, "7"))
    assert changed == [(7, "admin")]


def test_cancel_closes_a_pending_identifier_without_running_the_action(monkeypatch, pending_store):
    sent = _base(monkeypatch, _OWNER)

    async def must_not_change(*args, **kwargs):  # pragma: no cover
        raise AssertionError("a cancelled action must not run")

    monkeypatch.setattr(legal_message, "set_staff_role", must_not_change)

    asyncio.run(_turn(_OWNER, "/rebaixar"))
    asyncio.run(_turn(_OWNER, "/cancelar"))

    assert sent[-1] == (123, "Ok, essa ação foi cancelada.")
    assert not pending_store.waiting(123)


def test_another_command_replaces_the_pending_context(
    monkeypatch, pending_store, callback_store,
):
    # /ajuda now carries the menu, so it mints buttons. Without the store
    # this would pass through the path where minting fails, which proves
    # nothing about the path that runs in production.
    sent = _base(monkeypatch, _OWNER)

    asyncio.run(_turn(_OWNER, "/promover"))
    asyncio.run(_turn(_OWNER, "/ajuda"))

    assert "/docs" in sent[-1][1]
    assert not pending_store.waiting(123)


def test_pending_identifier_expires_instead_of_becoming_free_text(monkeypatch, pending_store):
    sent = _base(monkeypatch, _OWNER)
    clock = [100.0]
    monkeypatch.setattr(legal_message.time, "monotonic", lambda: clock[0])

    asyncio.run(_turn(_OWNER, "/promover"))
    clock[0] += legal_message._PENDING_DECISION_TTL + 1
    asyncio.run(_turn(_OWNER, "7"))

    assert "expirou" in sent[-1][1]
    assert not pending_store.waiting(123)


def test_bare_deactivation_selects_the_user_then_requires_exact_confirmation(monkeypatch):
    sent = _base(monkeypatch, _OWNER)
    deactivated = []

    async def deactivate(_conn, **kwargs):
        deactivated.append(kwargs)
        return staff_repository.StaffDeactivation(
            "deactivated", _TARGET.model_copy(update={"active": False})
        )

    monkeypatch.setattr(legal_message, "deactivate_staff", deactivate)

    asyncio.run(_turn(_OWNER, "/excluir_usuario"))
    asyncio.run(_turn(_OWNER, "7"))

    assert "revogará imediatamente" in sent[-1][1]
    assert deactivated == []

    asyncio.run(_turn(_OWNER, "sim"))
    assert "exatamente confirmar" in sent[-1][1]
    assert deactivated == []

    asyncio.run(_turn(_OWNER, "confirmar"))
    assert deactivated == [{"actor_id": 1, "target_id": 7}]
    assert "acesso de Ana foi excluído" in sent[-2][1]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("/arquivo", "original"),
    ],
)
def test_bare_document_command_lists_documents_and_accepts_the_next_id(
    monkeypatch,
    command,
    expected,
):
    sent = _base(monkeypatch, _LAWYER)
    reached = []

    async def count(*args, **kwargs):
        return 1

    async def search(*args, **kwargs):
        return [_DOCUMENT]

    async def get_document(*args, **kwargs):
        return _DOCUMENT

    async def original(*args, **kwargs):
        reached.append("original")

    async def deliver(*args, **kwargs):
        return None

    async def typing(*args, **kwargs):
        return None

    monkeypatch.setattr(legal_message, "count_documents", count)
    monkeypatch.setattr(legal_message, "search_documents", search)
    monkeypatch.setattr(legal_message, "get_document_summary", get_document)
    monkeypatch.setattr(legal_message, "_send_original_document", original)
    monkeypatch.setattr(legal_message, "_deliver", deliver)
    monkeypatch.setattr(channels, "send_typing", typing)

    asyncio.run(_turn(_LAWYER, command))

    assert "[42] Contrato" in sent[0][1]
    assert "Qual é o ID do documento" in sent[1][1]

    asyncio.run(_turn(_LAWYER, "42"))
    assert reached == [expected]


def test_bare_process_command_accepts_the_cnj_number_in_the_next_message(monkeypatch, pending_store):
    sent = _base(monkeypatch, _LAWYER)
    looked_up = []

    async def report(_conn, _chat_id, _staff, number):
        looked_up.append(number)

    monkeypatch.setattr(legal_message, "_report_process", report)

    asyncio.run(_turn(_LAWYER, "/processo"))
    assert "Qual é o número CNJ" in sent[-1][1]

    asyncio.run(_turn(_LAWYER, "0000832-35.2018.4.01.3202"))

    assert looked_up == ["00008323520184013202"]
    assert not pending_store.waiting(600)


def test_status_reports_the_pending_flow_without_ending_it(monkeypatch, pending_store):
    sent = _base(monkeypatch, _OWNER)
    changed = []

    async def set_role(_conn, identifier, role):
        changed.append((identifier, role))
        return _TARGET.model_copy(update={"role": role})

    monkeypatch.setattr(legal_message, "set_staff_role", set_role)

    asyncio.run(_turn(_OWNER, "/promover"))
    asyncio.run(_turn(_OWNER, "/status"))

    assert "promoção de usuário" in sent[-1][1]
    assert "envie o ID" in sent[-1][1]
    assert "Ana" not in sent[-1][1], "/status must not leak who is involved"
    assert pending_store.waiting(123), "asking must not drop the flow"

    # The flow it just described is still the one that completes.
    asyncio.run(_turn(_OWNER, "7"))
    assert changed == [(7, "admin")]


def test_status_without_any_flow_says_nothing_is_pending(monkeypatch):
    sent = _base(monkeypatch, _OWNER)

    asyncio.run(_turn(_OWNER, "/status"))

    assert "Nenhum fluxo em andamento" in sent[-1][1]


def test_status_reports_the_awaited_access_code(monkeypatch, pending_store):
    sent = _base(monkeypatch, _OWNER)

    async def list_pending(*args, **kwargs):
        return [
            SimpleNamespace(
                request_code="NS-A7B2988C",
                name="Helena Lobato",
                chat_id="987",
                username=None,
            )
        ]

    monkeypatch.setattr(legal_message, "list_pending_access_requests", list_pending)

    asyncio.run(_turn(_OWNER, "/autorizar"))
    asyncio.run(_turn(_OWNER, "/status"))

    assert "autorização de acesso" in sent[-1][1]
    assert "código" in sent[-1][1]
    assert pending_store.waiting(123)


def test_status_reports_the_awaited_export_format(monkeypatch, pending_store):
    sent = _base(monkeypatch, _LAWYER)
    asyncio.run(pending_store.remember(
        None, 600, kind="export", ttl_seconds=legal_message._PENDING_DECISION_TTL,
    ))

    asyncio.run(_turn(_LAWYER, "/status"))

    assert "exportação da última resposta" in sent[-1][1]
    assert "docx, pdf ou xlsx" in sent[-1][1]
    assert pending_store.waiting(600)


def test_expired_flow_is_not_reported_as_active(monkeypatch):
    sent = _base(monkeypatch, _OWNER)
    clock = [100.0]
    monkeypatch.setattr(legal_message.time, "monotonic", lambda: clock[0])

    asyncio.run(_turn(_OWNER, "/promover"))
    clock[0] += legal_message._PENDING_DECISION_TTL + 1
    asyncio.run(_turn(_OWNER, "/status"))

    assert "Nenhum fluxo em andamento" in sent[-1][1]
