"""The one door through the wall the folders build, and what keeps it narrow.

Private folders exist so a client's papers stay between the lawyer and the
client. That promise has a cost the firm cannot pay: when somebody leaves, the
papers of the clients they served go dark, and the firm still owes those
clients a duty.

So the owner can reach one named document. The tests here are not about
whether the reach works, which is the easy half. They are about the three
things that make it a door rather than a hole: it does not widen into
browsing, it is written down, and the person it was taken from is told.
"""
import asyncio
from types import SimpleNamespace

import pytest

from core.handlers import legal_message
from core.services import channels
from core.models import StaffMember
from core.services import authorization

pytestmark = pytest.mark.usefixtures(
    "isolate_message_rate_limits", "pending_store", "callback_store",
)

_OWNER = StaffMember(id=1, chat_id="100", name="Paulo", role="owner")
_LAWYER = StaffMember(id=4, chat_id="400", name="Rui", role="advogado")


@pytest.fixture
def firm(monkeypatch):
    state = SimpleNamespace(
        sent=[], audit=[], taken=None, previous=_LAWYER.id,
        title="Contrato do cliente Acme", exists=True, holder=_LAWYER,
    )

    async def get_staff(_conn, chat_id):
        return _OWNER if str(chat_id) == "100" else _LAWYER

    async def send(chat_id, message, reply_markup=None):
        state.sent.append((str(chat_id), message))
        return 1

    async def typing(*_args, **_kwargs):
        return None

    async def take_over(_conn, document_id, *, new_owner_id):
        if not state.exists:
            return None
        state.taken = (document_id, new_owner_id)
        return state.title, state.previous

    async def log_access(_conn, **kwargs):
        state.audit.append(kwargs)

    async def get_staff_by_id(_conn, staff_id):
        return state.holder

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(channels, "send_typing", typing)
    monkeypatch.setattr(legal_message, "take_over_document", take_over)
    monkeypatch.setattr(legal_message, "log_access", log_access)
    monkeypatch.setattr(legal_message, "get_staff_by_id", get_staff_by_id)
    return state


def _as_owner(text: str):
    update = {"update_id": 1, "message": {
        "chat": {"id": 100, "type": "private"}, "from": {"id": 100}, "text": text,
    }}
    asyncio.run(legal_message.process_legal_message("connection", update))


# ── who may ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["estagiario", "advogado", "admin"])
def test_nobody_below_the_owner_may_reach_another_folder(role):
    staff = StaffMember(id=9, chat_id="9", name="x", role=role)

    assert not authorization.decide(staff, "document.claim").allowed


def test_the_owner_may():
    assert authorization.decide(_OWNER, "document.claim").allowed


# ── the reach is narrow ──────────────────────────────────────────────────────


def test_it_takes_one_named_id_and_offers_no_listing(firm):
    """The difference between a door and a hole. There is nothing here that
    answers "what is in other people's folders", so the owner has to already
    know which document they are reaching for."""
    _as_owner("/assumir")

    assert firm.taken is None
    assert "/assumir <id>" in firm.sent[-1][1]
    assert "Contrato" not in firm.sent[-1][1], "no titles are volunteered"


def test_a_document_that_does_not_exist_says_so_and_takes_nothing(firm):
    firm.exists = False

    _as_owner("/assumir 77")

    assert firm.taken is None
    assert "77" in firm.sent[-1][1]


def test_the_document_lands_in_the_owners_own_folder_not_the_firms(firm):
    """Taking something out of a private folder and publishing it to everyone
    is two decisions. The second one belongs to /compartilhar."""
    _as_owner("/assumir 12")

    assert firm.taken == (12, _OWNER.id)
    reply = firm.sent[0][1]
    assert "pasta pessoal" in reply
    assert "/compartilhar 12 escritorio" in reply, "the second step is offered, not taken"


# ── it is written down ───────────────────────────────────────────────────────


def test_every_takeover_is_audited_with_who_and_which(firm):
    """The rank says who may. The log is what makes the reach answerable."""
    _as_owner("/assumir 12")

    assert len(firm.audit) == 1
    entry = firm.audit[0]
    assert entry["action"] == "claim_document"
    assert entry["staff_id"] == _OWNER.id
    assert entry["document_ids"] == [12]
    assert "4" in str(entry.get("query", "")), "the previous holder is recorded"


# ── the person is told ───────────────────────────────────────────────────────


def test_the_person_it_was_taken_from_is_told(firm):
    """This is the control, not the rank. An owner may reach, and cannot do it
    quietly."""
    _as_owner("/assumir 12")

    recipients = [chat for chat, _ in firm.sent]
    assert _LAWYER.chat_id in recipients

    notice = next(m for chat, m in firm.sent if chat == _LAWYER.chat_id)
    assert "Paulo" in notice and "12" in notice
    assert "registrada" in notice, "they are told it is on the record"


def test_the_owner_is_told_that_the_other_person_was_told(firm):
    """Otherwise the owner cannot know the accountability happened."""
    _as_owner("/assumir 12")

    owner_reply = next(m for chat, m in firm.sent if chat == _OWNER.chat_id)
    assert "avisado" in owner_reply


def test_a_takeover_still_stands_when_the_person_cannot_be_told(firm, monkeypatch):
    """Somebody who left has no chat to reach. The document must still be
    recovered: that case is the reason this command exists."""
    firm.holder = StaffMember(id=4, chat_id="", name="Rui", role="advogado")

    _as_owner("/assumir 12")

    assert firm.taken == (12, _OWNER.id)
    assert len(firm.audit) == 1, "and it is still on the record"


def test_taking_your_own_document_tells_nobody(firm):
    """No wall was crossed, so there is nothing to announce."""
    firm.previous = _OWNER.id

    _as_owner("/assumir 12")

    assert [chat for chat, _ in firm.sent] == [_OWNER.chat_id]


# ── the write itself ─────────────────────────────────────────────────────────


def test_the_takeover_reads_the_previous_holder_before_it_writes():
    """The tests above stub the repository, so nothing there exercises this
    SQL. It is read from the source instead, because two properties of it are
    load-bearing and neither survives a careless edit.

    The previous holder is read in its own statement **before** the update,
    not from a subquery inside RETURNING: whether such a subquery sees the row
    before or after is snapshot semantics, and that value is exactly what the
    notification and the audit entry depend on.

    And the read takes the row lock. Without it two takeovers at once both
    read the same previous holder, and one of the two notifications goes to
    somebody who no longer had the document.
    """
    import inspect

    from core.repositories import documents

    source = inspect.getsource(documents.take_over_document)

    select_at = source.index("SELECT title, uploaded_by")
    update_at = source.index("UPDATE documents SET uploaded_by")

    assert select_at < update_at, "the previous holder must be read before the write"
    assert "FOR UPDATE" in source, "the read must lock the row it is about to change"

    # There was a third assertion here, that RETURNING appears nowhere. It was
    # dropped: the docstring and a comment both name RETURNING to explain why
    # it is avoided, so the test was failing on its own explanation. A
    # source-level test cannot tell code from prose, and the ordering above
    # already pins the property that mattered.


def test_the_takeover_lands_in_a_private_folder():
    """Not the firm's shelf. Publishing somebody's private document to
    everyone in the same step that takes it is two decisions, and the second
    belongs to /compartilhar."""
    import inspect

    from core.repositories import documents

    body = inspect.getsource(documents.take_over_document)

    assert "visibility = 'private'" in body
    assert "visibility = 'firm'" not in body


# ── what goes dark when somebody leaves ──────────────────────────────────────


def test_deactivating_somebody_names_what_nobody_can_reach_anymore(monkeypatch):
    """Visibility is decided by `uploaded_by`, so a deactivated person's
    private folder is reachable by nobody. The documents are not deleted, they
    are dark, and the firm still owes a duty to those clients. Finding out on
    the day a client asks is too late."""
    sent = []

    async def send(chat_id, message, reply_markup=None):
        sent.append(message)
        return 1

    async def deactivate(_conn, *, actor_id, target_id):
        return SimpleNamespace(status="deactivated", staff=_LAWYER)

    async def private_documents_of(_conn, staff_id):
        return [(7, "Contrato Acme"), (9, "Parecer Beta")]

    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(legal_message, "deactivate_staff", deactivate)
    monkeypatch.setattr(legal_message, "private_documents_of", private_documents_of)

    asyncio.run(legal_message._apply_deactivation("conn", 100, _OWNER, _LAWYER.id))

    warning = next((m for m in sent if "sem ninguém" in m), "")
    assert warning, "the owner must be told what just went dark"
    assert "[7]" in warning and "[9]" in warning
    assert "não foram apagados" in warning
    assert "/assumir" in warning, "and how to get them back"


def test_deactivating_somebody_with_nothing_private_says_nothing_extra(monkeypatch):
    """A warning that fires when there is nothing to warn about is a warning
    people stop reading."""
    sent = []

    async def send(chat_id, message, reply_markup=None):
        sent.append(message)
        return 1

    async def deactivate(_conn, *, actor_id, target_id):
        return SimpleNamespace(status="deactivated", staff=_LAWYER)

    async def none_private(_conn, staff_id):
        return []

    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(legal_message, "deactivate_staff", deactivate)
    monkeypatch.setattr(legal_message, "private_documents_of", none_private)

    asyncio.run(legal_message._apply_deactivation("conn", 100, _OWNER, _LAWYER.id))

    assert not any("sem ninguém" in m for m in sent)


def test_a_failure_to_list_does_not_block_the_deactivation(monkeypatch):
    """Revoking access is the urgent half. A database hiccup counting
    documents must not leave somebody's access alive."""
    sent = []

    async def send(chat_id, message, reply_markup=None):
        sent.append(message)
        return 1

    async def deactivate(_conn, *, actor_id, target_id):
        return SimpleNamespace(status="deactivated", staff=_LAWYER)

    async def broken(_conn, staff_id):
        raise RuntimeError("database is away")

    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(legal_message, "deactivate_staff", deactivate)
    monkeypatch.setattr(legal_message, "private_documents_of", broken)

    result = asyncio.run(
        legal_message._apply_deactivation("conn", 100, _OWNER, _LAWYER.id),
    )

    assert "excluído" in result
