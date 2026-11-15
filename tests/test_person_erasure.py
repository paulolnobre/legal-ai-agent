"""Erasing one person: what has to go, and what the firm is not allowed to lose.

LGPD art. 18 V gives a person the right to have their data erased. Art. 16 I
keeps what has to be kept to satisfy a legal obligation, and the record of which
client documents were read, and when, is that. The two are only in tension if
"erase" is read as "delete the row": the row is what every audit entry points
at, so deleting it takes the trail along.

So this is the one operation in the system that is deliberately not a delete.
The tests below are about the three ways it could go quietly wrong: the identity
surviving somewhere nobody looked, the audit trail disappearing with it, and
somebody other than the owner reaching it.
"""
import asyncio
from types import SimpleNamespace

import pytest

from core.handlers import legal_message
from core.models import StaffMember
from core.repositories.staff import (
    ERASED_NAME,
    PersonErasure,
    StaffDeactivationError,
    erase_person,
)
from core.services.authorization import decide

pytestmark = pytest.mark.usefixtures("isolate_message_rate_limits")

OWNER = StaffMember(id=1, chat_id="123", name="Paulo", role="owner")
ADMIN = StaffMember(id=9, chat_id="111", name="Admin", role="admin")
LAWYER = StaffMember(id=4, chat_id="222", name="Helena", role="advogado")


def _update(chat_id: int, text: str) -> dict:
    return {
        "update_id": 900,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": chat_id}},
    }


@pytest.fixture
def chat(monkeypatch):
    sent = []

    def as_staff(member):
        async def get_staff(*_args, **_kwargs):
            return member
        monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)

    async def send(chat_id, text, reply_markup=None, **_kwargs):
        sent.append((chat_id, text))

    monkeypatch.setattr(legal_message, "_send", send)
    return SimpleNamespace(sent=sent, as_staff=as_staff)


# ── who may ─────────────────────────────────────────────────────────────────

def test_only_the_owner_may_erase_a_person():
    assert decide(OWNER, "staff.erase").allowed
    for member in (ADMIN, LAWYER, None):
        assert not decide(member, "staff.erase").allowed


@pytest.mark.parametrize("member", [ADMIN, LAWYER], ids=["admin", "advogado"])
def test_a_rank_below_owner_never_reaches_the_erasure(chat, monkeypatch, member):
    async def must_not_erase(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("only an owner may erase a person")

    chat.as_staff(member)
    monkeypatch.setattr(legal_message, "erase_person", must_not_erase)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(int(member.chat_id), "/esquecer 4 APAGAR"),
    ))

    assert chat.sent == [(int(member.chat_id), "🔒 Este comando é exclusivo do owner.")]


# ── the confirmation ────────────────────────────────────────────────────────

def test_the_confirmation_says_what_stays_and_not_only_what_goes(chat, monkeypatch):
    """The surprise from an erasure is never what went, it is what stayed."""
    async def by_id(_conn, staff_id):
        return LAWYER

    async def must_not_erase(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("the confirmation must not erase")

    chat.as_staff(OWNER)
    monkeypatch.setattr(legal_message, "get_staff_by_id", by_id)
    monkeypatch.setattr(legal_message, "erase_person", must_not_erase)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(123, "/esquecer 4"),
    ))

    message = chat.sent[0][1]
    assert "Helena" in message
    assert "documentos que essa pessoa enviou" in message, "say the documents stay"
    assert "sem identificar quem" in message, "say the audit trail stays"
    assert "APAGAR" in message


def test_the_confirmation_word_is_not_the_one_every_other_command_uses(
    chat, monkeypatch, pending_store,
):
    """"confirmar" is the reflex answer here, and this is the one thing that
    cannot be undone. Answering it must not erase anybody."""
    async def by_id(_conn, staff_id):
        return LAWYER

    async def must_not_erase(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("'confirmar' must not be enough to erase a person")

    chat.as_staff(OWNER)
    monkeypatch.setattr(legal_message, "get_staff_by_id", by_id)
    monkeypatch.setattr(legal_message, "erase_person", must_not_erase)

    asyncio.run(legal_message.process_legal_message("connection", _update(123, "/esquecer 4")))
    asyncio.run(legal_message.process_legal_message("connection", _update(123, "confirmar")))

    assert "APAGAR" in chat.sent[-1][1]


@pytest.mark.parametrize("answer,erases", [
    ("APAGAR", True), ("  APAGAR ", True), ("apagar", False), ("Apagar", False),
])
def test_answering_the_confirmation_with_the_word_in_capitals_erases(
    chat, monkeypatch, pending_store, answer, erases,
):
    """The word asked for, exactly as asked: in capitals. The answer used to be
    read lowercased and compared with "APAGAR", so no answer ever matched and
    only the one-line form "/esquecer 4 APAGAR" erased anybody."""
    erased_ids: list[int] = []

    async def by_id(_conn, staff_id):
        return LAWYER

    async def erase(_conn, *, actor_id, target_id):
        erased_ids.append(target_id)
        return PersonErasure(
            "erased", LAWYER.model_copy(update={"name": ERASED_NAME, "chat_id": None}))

    chat.as_staff(OWNER)
    monkeypatch.setattr(legal_message, "get_staff_by_id", by_id)
    monkeypatch.setattr(legal_message, "erase_person", erase)

    asyncio.run(legal_message.process_legal_message("connection", _update(123, "/esquecer 4")))
    asyncio.run(legal_message.process_legal_message("connection", _update(123, answer)))

    assert erased_ids == ([4] if erases else [])
    if not erases:
        assert "APAGAR" in chat.sent[-1][1]


def test_the_owner_cannot_erase_themselves_or_another_owner(chat, monkeypatch):
    async def by_id(_conn, staff_id):
        return OWNER

    async def must_not_erase(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("an owner is not erasable by this command")

    chat.as_staff(OWNER)
    monkeypatch.setattr(legal_message, "get_staff_by_id", by_id)
    monkeypatch.setattr(legal_message, "erase_person", must_not_erase)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(123, "/esquecer 1"),
    ))

    assert "owner" in chat.sent[0][1].casefold()


def test_the_answer_reports_counts_rather_than_promising(chat, monkeypatch):
    async def erased(_conn, *, actor_id, target_id):
        return PersonErasure(
            "erased",
            LAWYER.model_copy(update={"name": ERASED_NAME, "chat_id": None}),
            access_log_entries=17,
            steps_removed=1,
            buttons_removed=3,
            requests_anonymised=1,
        )

    chat.as_staff(OWNER)
    monkeypatch.setattr(legal_message, "erase_person", erased)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(123, "/esquecer 4 APAGAR"),
    ))

    message = chat.sent[0][1]
    assert "17" in message and "3" in message
    assert ERASED_NAME in message


# ── The statements themselves, not the command around them ──────────────────
# Everything above stubs erase_person, so a repository that wrote the wrong
# thing would pass all of it. These read the SQL.

class _Cursor:
    def __init__(self, state):
        self.state = state
        self.rowcount = 1
        self._result = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.state.statements.append((flat, params))
        if flat.startswith("SELECT id, role, active FROM staff"):
            self._result = self.state.actor
        elif flat.startswith("SELECT id, chat_id, email, name, role, active"):
            self._result = self.state.target
        else:
            self._result = None

    async def fetchone(self):
        return self._result


class _Connection:
    def __init__(self, *, actor, target):
        self.statements = []
        self.actor = actor
        self.target = target
        self.commits = 0

    def cursor(self):
        return _Cursor(self)

    async def commit(self):
        self.commits += 1


def _erasable_conn(*, target_role="advogado", chat_id="222"):
    return _Connection(
        actor=(1, "owner", True),
        target=(4, chat_id, "helena@escritorio.br", "Helena", target_role, True),
    )


def _run_erasure(conn):
    return asyncio.run(erase_person(conn, actor_id=1, target_id=4))


def test_the_staff_row_survives_so_the_audit_trail_still_points_somewhere():
    """Deleting the person deletes the trail. The row is tombstoned instead."""
    conn = _erasable_conn()
    result = _run_erasure(conn)

    assert result.status == "erased"
    statements = [sql for sql, _ in conn.statements]
    assert not any(sql.split()[:3] == ["DELETE", "FROM", "staff"] for sql in statements), (
        "deleting the staff row would cascade the audit trail away with it"
    )
    update = next(sql for sql in statements if sql.startswith("UPDATE staff"))
    assert "name = %s" in update
    assert "chat_id = NULL" in update
    assert "email = NULL" in update
    assert "active = FALSE" in update


def test_every_identifying_column_is_named_somewhere():
    """The point of the operation. A column left out is data left behind."""
    conn = _erasable_conn()
    _run_erasure(conn)
    written = " | ".join(sql for sql, _ in conn.statements)

    assert "UPDATE document_access_log SET chat_id = NULL" in written
    assert "DELETE FROM pending_actions WHERE chat_id" in written
    assert "DELETE FROM callback_actions WHERE chat_id" in written
    assert "UPDATE access_requests" in written
    # An OAB followed in the DJEN is the person's, and so is what they were told.
    assert "DELETE FROM watched_oabs WHERE staff_id" in written
    assert "DELETE FROM watched_processes WHERE staff_id" in written
    # What they wrote to the bot, and what it answered them.
    assert "DELETE FROM conversation_turns WHERE staff_id" in written
    assert "DELETE FROM publication_notices WHERE staff_id" in written


def test_the_name_that_replaces_a_name_is_obviously_not_one():
    conn = _erasable_conn()
    _run_erasure(conn)
    update = next(
        params for sql, params in conn.statements if sql.startswith("UPDATE staff")
    )

    assert update is not None and ERASED_NAME in update


def test_the_documents_the_person_uploaded_are_never_touched():
    """They are the firm's client material. They never belonged to the sender."""
    conn = _erasable_conn()
    _run_erasure(conn)

    for sql, _ in conn.statements:
        assert not sql.startswith("DELETE FROM documents"), sql
        assert "uploaded_by" not in sql, sql
        assert "document_chunks" not in sql, sql


def test_the_erasure_itself_is_written_to_the_audit_log():
    """An erasure nobody can prove happened is not an erasure anybody can defend."""
    conn = _erasable_conn()
    _run_erasure(conn)

    entry = next(
        (sql, params) for sql, params in conn.statements
        if "INSERT INTO staff_management_log" in sql
    )
    assert "'erase'" in entry[0]
    assert entry[1] is not None and entry[1][0] == 1, "the owner who ordered it"


def test_the_chat_id_is_cleared_last():
    """Every statement before it finds the person by chat id.

    Clearing the staff row first would leave the conversation rows and the
    access request untouched, holding the very identifier this is removing, and
    the operation would still report success.
    """
    conn = _erasable_conn()
    _run_erasure(conn)
    order = [sql for sql, _ in conn.statements]
    staff_update = next(i for i, sql in enumerate(order) if sql.startswith("UPDATE staff"))

    for sql in ("DELETE FROM pending_actions WHERE chat_id = %s",
                "DELETE FROM callback_actions WHERE chat_id = %s"):
        assert order.index(sql) < staff_update, f"{sql} runs after the id is gone"


def test_an_owner_is_refused_by_the_repository_and_not_only_by_the_handler():
    conn = _Connection(
        actor=(1, "owner", True),
        target=(2, "999", None, "Sócio", "owner", True),
    )
    with pytest.raises(StaffDeactivationError) as refused:
        asyncio.run(erase_person(conn, actor_id=1, target_id=2))
    assert refused.value.code == "target_owner"


def test_a_non_owner_actor_is_refused_by_the_repository():
    conn = _Connection(
        actor=(9, "admin", True),
        target=(4, "222", None, "Helena", "advogado", True),
    )
    with pytest.raises(StaffDeactivationError) as refused:
        asyncio.run(erase_person(conn, actor_id=9, target_id=4))
    assert refused.value.code == "actor_not_owner"


def test_erasing_an_already_erased_person_claims_no_work():
    conn = _Connection(
        actor=(1, "owner", True),
        target=(4, None, None, ERASED_NAME, "advogado", False),
    )
    result = _run_erasure(conn)

    assert result.status == "already_erased"
    assert not any("UPDATE staff" in sql for sql, _ in conn.statements)
