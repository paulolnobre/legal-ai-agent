"""The seam between the queue and the handler.

Every test in this suite used to exercise one side of this seam or the other:
the handler was called directly, the queue was measured on its own, and the
webhook was checked for what it enqueues. Nobody asked what the worker does
with a payload after it reserves one, and that is where a button press died
for days. It reserved the press, decided it had no chat, returned, and
acknowledged the payload as done. No queue entry, no log line, no error.

So the invariant lives here: every update kind the webhook is registered to
receive must reach the handler. Whatever the answer to a press turns out to
be, it has to be the handler that gives it.
"""
import asyncio

import pytest

from core.services.telegram_registration import ALLOWED_UPDATES as _ALLOWED_UPDATES
from core import legal_agent


class _Connection:
    async def __aenter__(self):
        return "connection"

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def connection(self):
        return _Connection()


def _sample(kind: str) -> dict:
    """One minimal update of each kind Telegram is told to forward."""
    chat = {"id": 5, "type": "private"}
    bodies = {
        "message": {"message": {"chat": chat, "text": "oi"}},
        "edited_message": {"edited_message": {"chat": chat, "text": "oi"}},
        "callback_query": {"callback_query": {
            "id": "q1",
            "data": "token",
            "from": {"id": 5},
            # The chat lives here, one level deeper than in a message.
            "message": {"message_id": 7, "chat": chat},
        }},
    }
    if kind not in bodies:
        raise AssertionError(
            f"{kind!r} was added to the webhook's allowed_updates but no sample "
            "exists here, so nothing checks that the worker delivers it"
        )
    return {"update_id": 1, **bodies[kind]}


@pytest.fixture
def delivered(monkeypatch):
    seen = []

    async def pool():
        return _Pool()

    async def handler(conn, update):
        seen.append(update)

    monkeypatch.setattr(legal_agent, "get_pool", pool)
    monkeypatch.setattr(legal_agent, "process_legal_message", handler)
    return seen


@pytest.mark.parametrize("kind", _ALLOWED_UPDATES)
def test_every_kind_the_webhook_accepts_reaches_the_handler(kind, delivered):
    asyncio.run(legal_agent._handle_update(_sample(kind)))

    assert delivered, (
        f"the worker dropped a {kind} update before the handler saw it, which "
        "leaves no log line and no error, only silence"
    )


def test_a_button_press_reaches_the_handler_whole(delivered):
    """The press must arrive intact, not reshaped into something else."""
    press = _sample("callback_query")

    asyncio.run(legal_agent._handle_update(press))

    assert delivered == [press]


def test_an_update_of_no_known_shape_is_still_the_handler_s_call(delivered):
    """The worker does not sort updates. Refusing one is a decision, and
    decisions belong in the handler, where they can be logged and audited."""
    asyncio.run(legal_agent._handle_update({"update_id": 2, "poll": {}}))

    assert delivered == [{"update_id": 2, "poll": {}}]
