"""A button decides the same way the command does, once, and never silently.

The acceptance criterion this covers is the roadmap's: the same task works by
command and by button, no button skips authorization, and a repeated press
does not repeat the action.
"""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from core.handlers import legal_message
from core.services import channels
from core.models import AccessRequest, StaffMember

pytestmark = pytest.mark.usefixtures("isolate_message_rate_limits")

_ADMIN = StaffMember(id=9, chat_id="900", name="Ivo", role="admin")
_LAWYER = StaffMember(id=6, chat_id="600", name="Lia", role="advogado")
_REQUEST = AccessRequest(id=3, request_code="NS-A7B2988C", chat_id="987", name="Helena")


def _press(
    token: str,
    *,
    chat_id: int = 900,
    presser: int | None = None,
    message_id: int = 42,
) -> dict:
    return {
        "update_id": 5,
        "callback_query": {
            "id": "cbq-1",
            "data": token,
            "from": {"id": presser if presser is not None else chat_id},
            "message": {
                "message_id": message_id,
                "chat": {"id": chat_id, "type": "private"},
                "text": "🔔 Nova solicitação de acesso",
            },
        },
    }


def _wire(monkeypatch, staff=_ADMIN):
    """The access-decision path, minus the button store."""
    decided: list[tuple[str, bool]] = []
    answered: list[str | None] = []
    edited: list[tuple[int, str]] = []
    sent: list[tuple] = []

    async def get_staff(_conn, chat_id):
        return staff if str(chat_id) == str(staff.chat_id) else None

    async def decide(_conn, code, *, approved, decided_by):
        decided.append((code, approved))
        return _REQUEST

    async def answer(query_id, text=None, base_url=None, show_alert=False):
        answered.append(text)
        return True

    async def edit(chat_id, message_id, text, base_url=None, reply_markup=None):
        edited.append((message_id, text))
        assert reply_markup is None, "a decided message must not keep its keyboard"
        return True

    async def send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text, reply_markup))

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "decide_access_request", decide)
    monkeypatch.setattr(channels, "answer_callback_query", answer)
    monkeypatch.setattr(channels, "edit_message_text", edit)
    monkeypatch.setattr(legal_message, "_send", send)
    return decided, answered, edited, sent


def _run(update):
    asyncio.run(legal_message.process_legal_message("connection", update))


async def _issue(store, operation: str, *, staff=_ADMIN):
    return await store.issue(
        None, chat_id=staff.chat_id, staff_id=staff.id,
        issued_role=staff.role, action="access.review",
        operation=operation, resource=_REQUEST.request_code,
        ttl_seconds=legal_message._CALLBACK_TTL, channel="telegram",
    )


def test_pressing_authorize_decides_the_request(monkeypatch, callback_store):
    decided, answered, edited, _ = _wire(monkeypatch)
    token = asyncio.run(_issue(callback_store, "approve"))

    _run(_press(token))

    assert decided == [("NS-A7B2988C", True)]
    assert answered == ["Autorizado."], "the spinner must be cleared with the outcome"
    assert edited and "Autorizado." in edited[0][1], "the message must show what happened"


def test_pressing_refuse_decides_the_other_way(monkeypatch, callback_store):
    decided, _, _, _ = _wire(monkeypatch)
    token = asyncio.run(_issue(callback_store, "refuse"))

    _run(_press(token))

    assert decided == [("NS-A7B2988C", False)]


def test_the_same_button_pressed_twice_decides_once(monkeypatch, callback_store):
    decided, answered, _, _ = _wire(monkeypatch)
    token = asyncio.run(_issue(callback_store, "approve"))

    _run(_press(token))
    _run(_press(token))

    assert decided == [("NS-A7B2988C", True)], "a double tap must not decide twice"
    assert answered[-1] == "Autorizado.", "the repeat is answered from the stored result"


def test_deciding_retires_the_other_button_on_the_same_request(monkeypatch, callback_store):
    decided, answered, _, _ = _wire(monkeypatch)
    approve = asyncio.run(_issue(callback_store, "approve"))
    refuse = asyncio.run(_issue(callback_store, "refuse"))

    _run(_press(approve))
    assert callback_store.state(refuse) == "cancelled"

    _run(_press(refuse))
    assert decided == [("NS-A7B2988C", True)], "the retired button must not decide"
    assert "já foi decidida" in (answered[-1] or "")


def test_a_button_stops_working_when_the_rank_no_longer_allows_it(monkeypatch, callback_store):
    """The row stores the action, so authorization is re-read at press time."""
    token = asyncio.run(_issue(callback_store, "approve", staff=_ADMIN))
    # The same chat and staff id, but the person is now only a lawyer.
    demoted = _ADMIN.model_copy(update={"role": "advogado"})
    decided, answered, _, _ = _wire(monkeypatch, staff=demoted)

    _run(_press(token))

    assert decided == [], "a demoted manager must not decide"
    assert "permissão" in (answered[-1] or "")


def test_an_unknown_token_fails_closed_without_describing_anything(monkeypatch, callback_store):
    decided, answered, edited, _ = _wire(monkeypatch)

    _run(_press("nao-existe"))

    assert decided == [] and edited == []
    assert answered == ["Esse botão não é mais válido."]


def test_a_token_pressed_from_another_chat_is_refused(monkeypatch, callback_store):
    decided, answered, _, _ = _wire(monkeypatch)
    token = asyncio.run(_issue(callback_store, "approve"))

    # Forwarded to a chat that is not the one the button was minted for.
    _run(_press(token, chat_id=600))

    assert decided == []
    assert answered == ["Esse botão não é mais válido."], "must not say what it would do"


def test_a_press_whose_sender_does_not_own_the_chat_is_ignored(monkeypatch, callback_store):
    decided, answered, _, _ = _wire(monkeypatch)
    token = asyncio.run(_issue(callback_store, "approve"))

    _run(_press(token, chat_id=900, presser=111))

    assert decided == []
    assert answered == ["Esse botão não é mais válido."]


def test_the_notification_carries_buttons_holding_only_the_token(monkeypatch, callback_store):
    _, _, _, sent = _wire(monkeypatch)

    async def managers(*args, **kwargs):
        return [_ADMIN]

    monkeypatch.setattr(legal_message, "list_staff", managers)
    asyncio.run(legal_message._notify_managers("connection", _REQUEST))

    chat_id, text, keyboard = sent[-1]
    assert chat_id == "900"
    assert [b["text"] for b in keyboard["inline_keyboard"][0]] == [
        "✅ Autorizar", "🚫 Recusar",
    ]
    # The reading sits on its own row: the irreversible pair is never the
    # neighbour of the harmless one under a thumb.
    assert [b["text"] for b in keyboard["inline_keyboard"][1]] == ["ℹ️ Ver detalhes"]
    buttons = [b for row in keyboard["inline_keyboard"] for b in row]
    for button in buttons:
        payload = button["callback_data"]
        assert payload in callback_store.rows, "the payload must be a minted token"
        assert _REQUEST.request_code not in payload, "the code must not travel in the payload"
        assert "Helena" not in payload
        assert len(payload.encode()) <= 64
    # The commands stay in the text, so the button is an addition, not a gate.
    assert "/autorizar" in text and "/recusar" in text


def test_a_command_decision_still_works_alongside_the_buttons(monkeypatch, callback_store):
    """Buttons must not become a second path around the command."""
    decided, _, _, _ = _wire(monkeypatch)

    asyncio.run(legal_message.process_legal_message(
        "connection",
        {"update_id": 6, "message": {
            "chat": {"id": 900, "type": "private"},
            "from": {"id": 900},
            "text": "/autorizar NS-A7B2988C",
        }},
    ))

    assert decided == [("NS-A7B2988C", True)]


_ADMIN2 = StaffMember(id=10, chat_id="1000", name="Bia", role="admin")


def test_deciding_tells_every_other_manager_and_clears_their_buttons(
    monkeypatch, callback_store,
):
    """The request went to several chats; one decision has to reach all of them."""
    decided, answered, edited, sent = _wire(monkeypatch)
    # Only the two notifications carry buttons; every later message still needs
    # an id back, so the counter keeps going rather than running out.
    ids = {"900": 501, "1000": 502}
    counter = [900]

    async def managers(*args, **kwargs):
        return [_ADMIN, _ADMIN2]

    async def send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text, reply_markup))
        if reply_markup:
            return ids[str(chat_id)]
        counter[0] += 1
        return counter[0]

    monkeypatch.setattr(legal_message, "list_staff", managers)
    monkeypatch.setattr(legal_message, "_send", send)

    asyncio.run(legal_message._notify_managers("connection", _REQUEST))

    # Ivo presses approve on his own copy.
    ivo_approve = next(
        token for token, row in callback_store.rows.items()
        if row["chat_id"] == "900" and row["operation"] == "approve"
    )
    _run(_press(ivo_approve, chat_id=900, message_id=501))

    assert decided == [("NS-A7B2988C", True)]

    # A message carries both buttons and the presser's own copy is among the
    # retired rows, so a naive loop rewrites some of them twice.
    touched = [message_id for message_id, _ in edited]
    assert sorted(touched) == [501, 502], f"each copy exactly once, got {touched}"

    rewritten = dict(edited)
    assert "Autorizado." in rewritten[502], "Bia must be told the outcome"
    assert "Decidido por Ivo" in rewritten[502], "she should see who settled it"
    assert "Autorizado." in rewritten[501], "the presser's own copy is rewritten too"
    assert "Decidido por" not in rewritten[501], "the presser knows who decided"

    # Every one of Bia's buttons is retired, so a late press cannot decide.
    assert all(
        row["state"] == "cancelled"
        for token, row in callback_store.rows.items()
        if row["chat_id"] == "1000"
    )

    bia_refuse = next(
        token for token, row in callback_store.rows.items()
        if row["chat_id"] == "1000" and row["operation"] == "refuse"
    )
    _run(_press(bia_refuse, chat_id=1000, message_id=502))

    assert decided == [("NS-A7B2988C", True)], "a late press must not decide again"


def test_every_press_leaves_a_trace_naming_what_it_decided(
    monkeypatch, callback_store, caplog,
):
    """A press that goes nowhere must still say why.

    Two structural refusals — no chat, no data — used to return without a log
    line. The webhook recorded a 200, the handler recorded nothing, and the
    only symptom was a button that span and gave up, which is not enough to
    debug from.
    """
    _wire(monkeypatch, _ADMIN)
    token = asyncio.run(_issue(callback_store, "approve"))

    with caplog.at_level("INFO", logger="core.handlers.legal_message"):
        _run(_press(token))

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "Button press received" in logged, "the entry itself must be visible"
    assert "presser_owns_chat=True" in logged
    assert "Button press granted" in logged, "a working press proves it worked"
    assert token not in logged, "the token is a capability and must not be logged"


def test_a_press_carrying_no_data_says_so(monkeypatch, callback_store, caplog):
    _wire(monkeypatch, _ADMIN)
    press = _press("")

    with caplog.at_level("INFO", logger="core.handlers.legal_message"):
        _run(press)

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "no data" in logged or "no chat" in logged


# ── Ver detalhes: reading a request without deciding it ─────────────────────

async def _issue_details(store, *, staff=_ADMIN):
    return await store.issue(
        None, chat_id=staff.chat_id, staff_id=staff.id,
        issued_role=staff.role, action="access.view_request",
        operation="details", resource=_REQUEST.request_code,
        ttl_seconds=legal_message._CALLBACK_TTL, channel="telegram",
    )


def _request(**overrides) -> AccessRequest:
    fields = {
        "id": 3, "request_code": "NS-A7B2988C", "chat_id": "987",
        "name": "Helena Lobato", "username": "helena_l", "status": "pending",
        "requested_at": datetime(2026, 9, 9, 14, 32, tzinfo=ZoneInfo("America/Sao_Paulo")),
    }
    return AccessRequest(**{**fields, **overrides})


def test_pressing_details_says_who_asked_and_when(monkeypatch, callback_store):
    """The notification carries a name and a code, which is enough to decide
    when the person is expected and not enough when they are not."""
    decided, answered, edited, sent = _wire(monkeypatch)

    async def lookup(_conn, code):
        assert code == _REQUEST.request_code
        return _request()

    monkeypatch.setattr(legal_message, "get_access_request", lookup)
    token = asyncio.run(_issue_details(callback_store))

    _run(_press(token))

    details = sent[-1][1]
    assert "Helena Lobato" in details
    assert "@helena_l" in details
    assert "987" in details, "the Telegram account that asked"
    assert "09/09/2026" in details
    assert "pendente" in details
    assert decided == [], "reading a request must not decide it"


def test_reading_a_request_leaves_its_decision_buttons_alone(monkeypatch, callback_store):
    """Nothing is retired and no message is rewritten: the manager pressed for
    information, and the decision they still have to make is untouched."""
    _wire(monkeypatch)

    async def lookup(_conn, _code):
        return _request()

    monkeypatch.setattr(legal_message, "get_access_request", lookup)
    approve = asyncio.run(_issue(callback_store, "approve"))
    refuse = asyncio.run(_issue(callback_store, "refuse"))
    details = asyncio.run(_issue_details(callback_store))

    _run(_press(details))

    assert callback_store.state(approve) == "pending"
    assert callback_store.state(refuse) == "pending"


def test_the_details_message_is_never_rewritten(monkeypatch, callback_store):
    _, _, edited, _ = _wire(monkeypatch)

    async def lookup(_conn, _code):
        return _request()

    monkeypatch.setattr(legal_message, "get_access_request", lookup)
    token = asyncio.run(_issue_details(callback_store))

    _run(_press(token))

    assert edited == [], "a read must not edit the notification"


def test_pressing_details_twice_says_so_instead_of_sending_them_again(
    monkeypatch, callback_store,
):
    """Every row in this table is single-use by design. A second press is
    answered from the recorded result rather than doing nothing visible."""
    _, answered, _, sent = _wire(monkeypatch)

    async def lookup(_conn, _code):
        return _request()

    monkeypatch.setattr(legal_message, "get_access_request", lookup)
    token = asyncio.run(_issue_details(callback_store))

    _run(_press(token))
    before = len(sent)
    _run(_press(token))

    assert len(sent) == before, "the details are not sent twice"
    toast = answered[-1]
    assert toast and "já foram enviados" in toast


def test_details_for_a_request_that_is_gone_says_that(monkeypatch, callback_store):
    _, answered, _, sent = _wire(monkeypatch)

    async def lookup(_conn, _code):
        return None

    monkeypatch.setattr(legal_message, "get_access_request", lookup)
    token = asyncio.run(_issue_details(callback_store))

    _run(_press(token))

    toast = answered[-1]
    assert toast and "não existe mais" in toast


def test_a_decided_request_reads_back_as_decided(monkeypatch, callback_store):
    """Not filtered to pending on purpose: a manager looking at a notification
    for something already settled should be told that, not told it is gone."""
    _, _, _, sent = _wire(monkeypatch)

    async def lookup(_conn, _code):
        return _request(status="approved")

    monkeypatch.setattr(legal_message, "get_access_request", lookup)
    token = asyncio.run(_issue_details(callback_store))

    _run(_press(token))

    assert "autorizada" in sent[-1][1]
    assert "/autorizar" not in sent[-1][1], "a settled request offers no next step"


def test_a_rank_that_cannot_decide_cannot_read_either(monkeypatch, callback_store):
    _, answered, _, sent = _wire(monkeypatch, staff=_LAWYER)

    async def must_not_look(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("a lawyer must not reach an access request")

    monkeypatch.setattr(legal_message, "get_access_request", must_not_look)
    token = asyncio.run(_issue_details(callback_store, staff=_LAWYER))

    _run(_press(token, chat_id=600))

    toast = answered[-1]
    assert toast and "permissão" in toast
