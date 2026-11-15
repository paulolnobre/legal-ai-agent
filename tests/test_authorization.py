"""One place decides who may do what, and it decides the same way every time.

The service exists because a rank check bound to a command string was walked
around by asking for the same thing in prose. So the property under test is not
"interns are refused" — it is that the command route and the language route
reach the *same action* and get the *same verdict*.
"""
import asyncio
from types import SimpleNamespace

import pytest

from core.handlers import legal_message
from core.models import StaffMember
from core.services import authorization

pytestmark = pytest.mark.usefixtures("isolate_message_rate_limits")

_INTERN = StaffMember(id=4, chat_id="400", name="Rita", role="estagiario")
_LAWYER = StaffMember(id=6, chat_id="600", name="Lia", role="advogado")
_ADMIN = StaffMember(id=9, chat_id="900", name="Ivo", role="admin")
_OWNER = StaffMember(id=1, chat_id="123", name="Paulo", role="owner")


# ── the service itself ───────────────────────────────────────────────────────

def test_every_action_has_a_required_rank():
    """A name added to Action without a rank would otherwise decide nothing."""
    missing = [a for a in authorization.known_actions()
               if authorization.required_rank(a) is None]
    assert not missing, f"{missing} have no required rank"


def test_an_unknown_action_is_refused():
    """The safe reading of a mistake is no, matching how an unreadable role falls."""
    decision = authorization.decide(_OWNER, "staff.delete_everything")

    assert not decision.allowed
    assert decision.reason


def test_a_missing_staff_member_is_refused():
    assert not authorization.decide(None, "document.read_original").allowed


@pytest.mark.parametrize("action", ["document.read_original", "document.revise",
                                    "document.ingest"])
def test_acting_on_the_archive_starts_at_lawyer(action):
    assert not authorization.decide(_INTERN, action).allowed
    assert authorization.decide(_LAWYER, action).allowed
    assert authorization.decide(_ADMIN, action).allowed


@pytest.mark.parametrize("action", ["staff.change_rank", "staff.deactivate"])
def test_changing_what_someone_is_stays_with_the_owner(action):
    assert not authorization.decide(_ADMIN, action).allowed
    assert authorization.decide(_OWNER, action).allowed


def test_a_corrupted_role_loses_privilege_rather_than_gaining_it():
    damaged = StaffMember(id=5, chat_id="500", name="?", role="SUPERUSER")

    assert not authorization.decide(damaged, "document.read_original").allowed
    assert not authorization.decide(damaged, "staff.deactivate").allowed


# ── the property that motivated the service ──────────────────────────────────

def _update(chat_id: int, text: str) -> dict:
    return {
        "update_id": 1,
        "message": {
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "first_name": "Pessoa"},
            "text": text,
        },
    }


def _run(monkeypatch, staff: StaffMember, text: str):
    sent: list[str] = []
    reached: list[str] = []

    async def get_staff(*args, **kwargs):
        return staff

    async def send(_chat_id, message, reply_markup=None):
        sent.append(message)

    async def original(*args, **kwargs):
        reached.append("original")

    async def get_document(*args, **kwargs):
        return SimpleNamespace(id=2, title="Contrato", doc_type="contrato",
                               matter="Acme", created_at=None)

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(legal_message, "_send_original_document", original)
    monkeypatch.setattr(legal_message, "get_document_summary", get_document)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(int(staff.chat_id or 0), text)))
    return sent, reached


@pytest.mark.parametrize("phrasing", [
    "/arquivo 2",
    "me manda o arquivo original do documento 2",
])
def test_the_command_and_the_sentence_are_refused_alike(monkeypatch, phrasing):
    """The bug this service exists for: prose walking around a locked command."""
    sent, reached = _run(monkeypatch, _INTERN, phrasing)

    assert reached == [], f"an intern reached the original via {phrasing!r}"
    assert any("advogados" in message for message in sent)


@pytest.mark.parametrize("phrasing", [
    "/arquivo 2",
    "me manda o arquivo original do documento 2",
])
def test_the_command_and_the_sentence_are_allowed_alike(monkeypatch, phrasing):
    _, reached = _run(monkeypatch, _LAWYER, phrasing)

    assert reached == ["original"], f"a lawyer was blocked via {phrasing!r}"


def test_process_track_is_every_rank():
    """Following a case and its deadlines: an intern keeps the calendar too.
    An unreadable role falls to intern, so it is allowed as every intern action."""
    from core.models import StaffMember
    for role in ("estagiario", "advogado", "admin", "owner", "bogus"):
        assert authorization.decide(StaffMember(id=1, chat_id="1", name="X", role=role),
                      "process.track").allowed
    assert not authorization.decide(None, "process.track").allowed
