"""A step in progress outlives the process, and is answerable exactly once.

These are the two acceptance criteria the roadmap sets for persisted steps:
restarting must not lose the step, and a duplicated message must not run the
action twice.
"""
import asyncio

import pytest

from core.handlers import legal_message
from core.models import StaffMember

pytestmark = pytest.mark.usefixtures("isolate_message_rate_limits")

_OWNER = StaffMember(id=1, chat_id="123", name="Paulo", role="owner")
_TARGET = StaffMember(id=7, chat_id="700", name="Ana", role="advogado")


def _update(chat_id: int, text: str) -> dict:
    return {
        "update_id": 900,
        "message": {
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "first_name": "Pessoa"},
            "text": text,
        },
    }


def _wire(monkeypatch: pytest.MonkeyPatch):
    """Everything the promotion path touches, minus the step store."""
    promotions: list[tuple[int, str]] = []

    async def get_staff(*args, **kwargs):
        return _OWNER

    async def get_target(_conn, identifier):
        return _TARGET if identifier == _TARGET.id else None

    async def list_users(*args, **kwargs):
        return [_OWNER, _TARGET]

    async def set_role(_conn, identifier, role):
        promotions.append((identifier, role))
        return _TARGET.model_copy(update={"role": role})

    async def send(*args, **kwargs):
        return None

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "get_staff_by_id", get_target)
    monkeypatch.setattr(legal_message, "list_staff", list_users)
    monkeypatch.setattr(legal_message, "set_staff_role", set_role)
    monkeypatch.setattr(legal_message, "_send", send)
    return promotions


def test_a_step_written_before_a_restart_is_still_answerable(monkeypatch, pending_store):
    """The step is reconstructed from storage, not from process memory.

    The store is seeded directly, the way a previous process would have left it.
    Nothing in this test ever asks the handler to open the flow, so the only
    place the answer can come from is persistence.
    """
    promotions = _wire(monkeypatch)

    asyncio.run(pending_store.remember(
        None, 123,
        kind="identifier",
        action="promote",
        ttl_seconds=legal_message._PENDING_DECISION_TTL,
    ))

    asyncio.run(legal_message.process_legal_message("connection", _update(123, "7")))

    assert promotions == [(7, "admin")], "the step survived and completed"
    assert not pending_store.waiting(123)


def test_the_same_answer_delivered_twice_runs_the_action_once(monkeypatch, pending_store):
    """Telegram can redeliver an update; the second one must find nothing."""
    promotions = _wire(monkeypatch)

    asyncio.run(legal_message.process_legal_message("connection", _update(123, "/promover")))
    assert pending_store.waiting(123)

    asyncio.run(legal_message.process_legal_message("connection", _update(123, "7")))
    asyncio.run(legal_message.process_legal_message("connection", _update(123, "7")))

    assert promotions == [(7, "admin")], "the duplicate must not promote again"


def test_starting_a_flow_replaces_the_one_already_open(monkeypatch, pending_store):
    """One chat waits on exactly one thing — enforced by the store, not by hand."""
    _wire(monkeypatch)

    asyncio.run(legal_message.process_legal_message("connection", _update(123, "/promover")))
    assert pending_store.kind(123) == "identifier"

    asyncio.run(pending_store.remember(
        None, 123, kind="decision", approved=True,
        ttl_seconds=legal_message._PENDING_DECISION_TTL,
    ))

    assert pending_store.kind(123) == "decision"
    assert len(pending_store.rows) == 1


def test_an_expired_step_is_swept(pending_store):
    """Steps nobody came back to answer do not accumulate."""
    clock = [100.0]
    original = legal_message.time.monotonic
    legal_message.time.monotonic = lambda: clock[0]
    try:
        asyncio.run(pending_store.remember(
            None, 123, kind="export", ttl_seconds=legal_message._PENDING_DECISION_TTL,
        ))
        clock[0] += legal_message._PENDING_DECISION_TTL + 1

        assert not pending_store.waiting(123)
        assert asyncio.run(pending_store.purge_expired(None)) == 1
        assert pending_store.rows == {}
    finally:
        legal_message.time.monotonic = original
