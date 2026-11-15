"""Whether an answer helped, which nothing else in this system measures.

Every other metric says what happened. None of them separates a good answer
from a confident wrong one, and that is the question the pilot has to answer
before ten lawyers arrive. So the tests here are about the signal actually
being collected: that the buttons reach every answer, that a press is stored,
and that somebody who changes their mind ends with one row saying the later
thing rather than two rows contradicting each other.
"""
import asyncio
import inspect
import re
from types import SimpleNamespace

import pytest

from core.handlers import legal_message
from core.models import StaffMember
from core.repositories import answer_feedback
from core.services.legal_assistant.reply import AgentReply

pytestmark = pytest.mark.usefixtures(
    "isolate_message_rate_limits", "pending_store", "callback_store",
)

_LAWYER = StaffMember(id=4, chat_id="400", name="Rui", role="advogado")


@pytest.fixture
def delivery(monkeypatch):
    state = SimpleNamespace(sent=[], issued=[], recorded=[], remembered={})

    async def issue(_conn, *, chat_id, staff_id, issued_role, action,
                    operation, resource, ttl_seconds, channel):
        state.issued.append((action, operation, resource, staff_id))
        return f"token-{operation}"

    async def send(_chat_id, message, reply_markup=None):
        state.sent.append((message, reply_markup))
        return 1

    async def record(_conn, *, answer_ref, staff_id, chat_id, verdict,
                     question="", task=""):
        previous = state.remembered.get((answer_ref, staff_id))
        state.remembered[(answer_ref, staff_id)] = verdict
        state.recorded.append((answer_ref, staff_id, verdict, question, task))
        return previous is not None and previous != verdict

    monkeypatch.setattr(legal_message.callback_actions, "issue", issue)
    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(legal_message.answer_feedback, "record", record)
    return state


def _deliver(reply=None, title="a pergunta do advogado"):
    reply = reply or AgentReply(text="uma resposta longa o suficiente", task="internal_research")
    asyncio.run(legal_message._deliver(
        400, reply, title, conn="connection", staff=_LAWYER,
    ))


# ── the buttons reach the answer ─────────────────────────────────────────────


def test_every_answer_is_offered_for_rating(delivery):
    _deliver()

    _message, keyboard = delivery.sent[-1]
    labels = [button["text"] for button in keyboard["inline_keyboard"][0]]
    assert labels == ["👍 Ajudou", "👎 Não ajudou"]


def test_both_buttons_describe_the_same_answer(delivery):
    """One `answer_ref` across the pair is what lets a second press correct the
    first instead of adding a contradicting row."""
    _deliver()

    refs = {resource for _action, _op, resource, _staff in delivery.issued}
    assert len(refs) == 1


def test_the_buttons_carry_the_rating_action_and_the_two_verdicts(delivery):
    _deliver()

    assert [(a, o) for a, o, _r, _s in delivery.issued] == [
        ("answer.rate", "util"), ("answer.rate", "nao_util"),
    ]


def test_an_answer_still_goes_out_when_the_buttons_cannot_be_minted(
    delivery, monkeypatch,
):
    """Losing a rating is a gap in a metric. Losing the answer is the lawyer's
    work, so a failure to mint must never cost it."""
    async def broken(*_args, **_kwargs):
        raise RuntimeError("database is away")

    monkeypatch.setattr(legal_message.callback_actions, "issue", broken)

    _deliver(AgentReply(text="o texto da resposta"))

    message, keyboard = delivery.sent[-1]
    assert "o texto da resposta" in message
    assert keyboard is None


def test_a_delivery_path_cannot_forget_to_ask(delivery):
    """`conn` and `staff` are required, not defaulted: a new call site that
    omits them fails loudly instead of silently never rating again.

    Pyright refuses this call outright, which is the stronger guard of the two
    and the reason for the suppression: the mistake is caught before the code
    runs. This keeps the runtime half honest.
    """
    with pytest.raises(TypeError):
        asyncio.run(legal_message._deliver(  # pyright: ignore[reportCallIssue]
            400, AgentReply(text="oi"), "t",
        ))


def test_every_call_site_passes_them():
    """The guard above only bites at run time, and a delivery path that is not
    covered by a test would reach production before it did."""
    source = inspect.getsource(legal_message)
    calls = re.findall(r"await _deliver\((.*?)\)\n", source, re.S)

    assert calls, "no _deliver call found; this guard has stopped guarding"
    for call in calls:
        assert "conn=" in call and "staff=" in call, call


# ── the press ────────────────────────────────────────────────────────────────


def _press(delivery, verdict, answer_ref="ref-1"):
    answers: list[str] = []

    async def answered(message):
        answers.append(message)

    asyncio.run(legal_message._record_answer_feedback(
        "connection", 400, _LAWYER, answer_ref, verdict, answered,
    ))
    return answers


def test_a_thumbs_up_is_stored_and_acknowledged(delivery):
    answers = _press(delivery, "util")

    assert delivery.recorded[-1][:3] == ("ref-1", 4, "util")
    assert "ajudou" in answers[-1].casefold()


def test_a_thumbs_down_asks_what_was_wrong(delivery):
    """A 👎 with no reason is a number nobody can act on."""
    answers = _press(delivery, "nao_util")

    assert "o que faltou" in answers[-1] or "errado" in answers[-1]


def test_changing_your_mind_replaces_the_verdict_rather_than_adding_one(delivery):
    """A decision happens once; an opinion may be revised, and the corrected
    verdict is the better datum."""
    _press(delivery, "util")
    answers = _press(delivery, "nao_util")

    assert delivery.remembered[("ref-1", 4)] == "nao_util"
    assert "mudei" in answers[-1].casefold()


def test_the_repository_refuses_a_verdict_nobody_defined():
    """A column that quietly accepts anything stops being countable.

    No `delivery` fixture here on purpose: it patches `record` on the module,
    which is the same object this test imports, so the fixture's double would
    be what got asserted on. An earlier version did exactly that, asserted on
    its own stub, and a mutation removing the real check survived.
    """
    with pytest.raises(ValueError, match="unknown verdict"):
        asyncio.run(answer_feedback.record(
            object(), answer_ref="ref-1", staff_id=4, chat_id=400, verdict="talvez",
        ))


def test_a_refused_verdict_still_answers_the_press(delivery, monkeypatch):
    """A button that does nothing and says nothing reads as the bot crashing."""
    async def strict(*_args, **kwargs):
        raise ValueError(f"unknown verdict {kwargs.get('verdict')!r}")

    monkeypatch.setattr(legal_message.answer_feedback, "record", strict)

    assert _press(delivery, "talvez")


def test_a_storage_failure_is_told_to_the_person(delivery, monkeypatch):
    async def broken(*_args, **_kwargs):
        raise RuntimeError("database is away")

    monkeypatch.setattr(legal_message.answer_feedback, "record", broken)

    answers = _press(delivery, "util")

    assert "tente de novo" in answers[-1].casefold()


# ── what the question is stored as ───────────────────────────────────────────


def test_the_question_travels_with_the_verdict(delivery):
    """Without it a 👎 is a number nobody can act on."""
    _deliver(title="prescrição em ação de despejo")
    answer_ref = delivery.issued[0][2]

    _press(delivery, "nao_util", answer_ref=answer_ref)

    assert delivery.recorded[-1][3] == "prescrição em ação de despejo"


def test_an_answer_older_than_the_window_still_records_the_verdict(delivery):
    """The question is held in process and is lost on a deploy. The verdict is
    the part that must survive, and it does."""
    answers = _press(delivery, "util", answer_ref="a-ref-nobody-remembers")

    assert delivery.recorded[-1][2] == "util"
    assert delivery.recorded[-1][3] == ""
    assert answers


# ── the rank ─────────────────────────────────────────────────────────────────


def test_rating_is_open_to_every_authorized_rank():
    """The value is in how many people bother; a rank check would only cost
    signal."""
    from core.services import authorization

    for role in ("estagiario", "advogado", "admin", "owner"):
        staff = StaffMember(id=1, chat_id="1", name="x", role=role)
        assert authorization.decide(staff, "answer.rate").allowed, role


def test_the_repository_names_the_two_verdicts_the_buttons_issue():
    """The buttons are built from this tuple, so a third verdict added here
    without a button, or the reverse, is a drift worth failing on."""
    assert answer_feedback.VERDICTS == ("util", "nao_util")
