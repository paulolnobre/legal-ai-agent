"""Removing a document: who may, what goes, and what survives.

This is the only command in the system with nothing to undo it. The corpus is
the firm's own material and the original file is the client's, so the tests
here are less about the happy path than about the three ways it could go wrong
quietly: the wrong rank reaching it, a file deleted while another document
still needs it, and a deletion that leaves no trace of who ordered it.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

from core.handlers import legal_message
from core.services import channels
from core.repositories.callback_actions import CallbackClaim
from core.models import DocumentSummary, StaffMember
from core.repositories.documents import DocumentDeletion
from core.services import document_removal
from core.services.authorization import decide

pytestmark = pytest.mark.usefixtures("isolate_message_rate_limits")

OWNER = StaffMember(id=1, chat_id="123", name="Paulo", role="owner")
ADMIN = StaffMember(id=9, chat_id="111", name="Admin", role="admin")
LAWYER = StaffMember(id=4, chat_id="222", name="Helena", role="advogado")


def _update(chat_id: int, text: str) -> dict:
    return {
        "update_id": 700,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": chat_id}},
    }


def _document(document_id: int = 42, title: str = "Contrato Acme") -> DocumentSummary:
    return DocumentSummary(id=document_id, title=title)


@pytest.fixture
def chat(monkeypatch):
    """A conversation with a staff member, capturing what the bot says back."""
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

def test_only_the_owner_may_delete_a_document():
    assert decide(OWNER, "document.delete").allowed
    for member in (ADMIN, LAWYER, None):
        assert not decide(member, "document.delete").allowed


@pytest.mark.parametrize("member", [ADMIN, LAWYER], ids=["admin", "advogado"])
def test_a_rank_below_owner_never_reaches_the_deletion(chat, monkeypatch, member):
    async def must_not_remove(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("only an owner may remove a document")

    chat.as_staff(member)
    monkeypatch.setattr(legal_message, "remove_document", must_not_remove)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(int(member.chat_id), "/excluir_documento 42 confirmar"),
    ))

    assert chat.sent == [(int(member.chat_id), "🔒 Este comando é exclusivo do owner.")]


# ── the confirmation step ───────────────────────────────────────────────────

def test_the_owner_sees_the_document_named_before_anything_is_deleted(chat, monkeypatch):
    async def summary(_conn, document_id, scope=None):
        return _document(document_id)

    async def must_not_remove(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("the preview must not delete")

    chat.as_staff(OWNER)
    monkeypatch.setattr(legal_message, "get_document_summary", summary)
    monkeypatch.setattr(legal_message, "remove_document", must_not_remove)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(123, "/excluir_documento 42"),
    ))

    assert len(chat.sent) == 1
    message = chat.sent[0][1]
    assert "Contrato Acme" in message
    assert "arquivo original" in message
    assert "/excluir_documento 42 confirmar" in message


def test_answering_the_confirmation_deletes_that_document(chat, monkeypatch, pending_store):
    removed = []

    async def summary(_conn, document_id, scope=None):
        return _document(document_id)

    async def remove(_conn, document_id, *, staff_id, chat_id):
        removed.append((document_id, staff_id))
        return document_removal.RemovalOutcome(
            status="removed", title="Contrato Acme", original_removed=True,
        )

    chat.as_staff(OWNER)
    monkeypatch.setattr(legal_message, "get_document_summary", summary)
    monkeypatch.setattr(legal_message, "remove_document", remove)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(123, "/excluir_documento 42"),
    ))
    assert removed == []

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(123, "confirmar"),
    ))

    assert removed == [(42, 1)]
    assert "apagado do acervo" in chat.sent[-1][1]
    assert not pending_store.waiting(123)


def test_anything_other_than_the_word_confirmar_deletes_nothing(chat, monkeypatch):
    async def summary(_conn, document_id, scope=None):
        return _document(document_id)

    async def must_not_remove(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("only the exact word confirms")

    chat.as_staff(OWNER)
    monkeypatch.setattr(legal_message, "get_document_summary", summary)
    monkeypatch.setattr(legal_message, "remove_document", must_not_remove)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(123, "/excluir_documento 42"),
    ))
    asyncio.run(legal_message.process_legal_message(
        "connection", _update(123, "sim, pode apagar"),
    ))

    assert "confirmar" in chat.sent[-1][1]


def test_the_bare_command_asks_which_document(chat, monkeypatch, pending_store):
    async def count(_conn, scope=None):
        return 3

    async def catalog(_conn, chat_id, **_kwargs):
        chat.sent.append((chat_id, "[42] Contrato Acme"))

    chat.as_staff(OWNER)
    monkeypatch.setattr(legal_message, "count_documents", count)
    monkeypatch.setattr(legal_message, "_send_document_catalog", catalog)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(123, "/excluir_documento"),
    ))

    assert "excluir do acervo" in chat.sent[-1][1]
    assert pending_store.kind(123) == "identifier"


# ── what the reply admits to ────────────────────────────────────────────────

def test_a_file_kept_for_another_document_is_said_out_loud(chat, monkeypatch):
    """Two documents can share one file: the key is the sha256 of its bytes.

    Reporting "the original was deleted" here would be a lie, and the kind that
    only surfaces the day someone checks whether a client's file is really gone.
    """
    async def remove(*_args, **_kwargs):
        return document_removal.RemovalOutcome(
            status="removed",
            title="Contrato Acme",
            original_kept_for_another_document=True,
        )

    chat.as_staff(OWNER)
    monkeypatch.setattr(legal_message, "remove_document", remove)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(123, "/excluir_documento 42 confirmar"),
    ))

    assert "outro documento" in chat.sent[-1][1]


def test_a_file_that_could_not_be_removed_is_reported_with_its_location(chat, monkeypatch):
    """The row is gone, so nothing in the assistant can reach the file again.
    This message is the only place that still knows where it is."""
    async def remove(*_args, **_kwargs):
        return document_removal.RemovalOutcome(
            status="removed", title="Contrato Acme", orphaned_storage_key="ab/cd/abcd.pdf",
        )

    chat.as_staff(OWNER)
    monkeypatch.setattr(legal_message, "remove_document", remove)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(123, "/excluir_documento 42 confirmar"),
    ))

    assert "ab/cd/abcd.pdf" in chat.sent[-1][1]
    assert "à mão" in chat.sent[-1][1]


def test_status_describes_the_deletion_it_is_waiting_on(chat, monkeypatch, pending_store):
    """/status is what someone sends when they are unsure what they started.
    Answering "nothing in progress" while a deletion waits for confirmation
    would be wrong in the one flow where being wrong costs a document."""
    async def summary(_conn, document_id, scope=None):
        return _document(document_id)

    chat.as_staff(OWNER)
    monkeypatch.setattr(legal_message, "get_document_summary", summary)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(123, "/excluir_documento 42"),
    ))
    asyncio.run(legal_message.process_legal_message("connection", _update(123, "/status")))

    assert "exclusão de documento" in chat.sent[-1][1]


def test_every_step_a_flow_can_wait_on_can_be_described():
    """A pending action with no description makes /status silent about it."""
    described = set(legal_message._PENDING_STEPS)
    possible = set(legal_message._PendingAction.__args__)

    assert possible - described == set(), (
        "these steps can be waited on but /status cannot name them: "
        f"{sorted(possible - described)}"
    )


# ── the service, and the order it commits in ────────────────────────────────

class _Connection:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@pytest.fixture
def removal(monkeypatch):
    """The service with its database, disk and cache replaced by records."""
    calls = SimpleNamespace(deleted=[], logged=[], unlinked=[], invalidated=0)

    async def delete(_conn, document_id, *, commit=True):
        calls.deleted.append((document_id, commit))
        return DocumentDeletion(
            status="deleted", title="Contrato Acme", storage_key="ab/cd/abcd.pdf",
        )

    async def log(_conn, action, **kwargs):
        calls.logged.append((action, kwargs))

    async def unlink(key, wrapped_key=None):
        calls.unlinked.append(key)

    async def invalidate():
        calls.invalidated += 1
        return 7

    monkeypatch.setattr(document_removal, "delete_document", delete)
    monkeypatch.setattr(document_removal, "log_access", log)
    monkeypatch.setattr(document_removal.document_storage, "remove", unlink)
    monkeypatch.setattr(document_removal.cache, "invalidate_answers", invalidate)
    return calls


def test_the_deletion_and_its_audit_entry_are_one_transaction(removal):
    """There is no state where a document vanishes unattributed."""
    conn = _Connection()

    asyncio.run(document_removal.remove_document(conn, 42, staff_id=1, chat_id=123))

    assert removal.deleted == [(42, False)], "the delete must not commit on its own"
    action, kwargs = removal.logged[0]
    assert action == "delete_document"
    assert kwargs["staff_id"] == 1
    assert kwargs["document_ids"] == [42]
    assert kwargs["commit"] is False
    assert conn.commits == 1, "both rows are committed together, once"


def test_the_file_is_unlinked_only_after_the_commit(removal, monkeypatch):
    conn = _Connection()
    order = []

    async def commit():
        order.append("commit")
        conn.commits += 1

    async def unlink(key, wrapped_key=None):
        order.append("unlink")
        removal.unlinked.append(key)

    monkeypatch.setattr(conn, "commit", commit)
    monkeypatch.setattr(document_removal.document_storage, "remove", unlink)

    asyncio.run(document_removal.remove_document(conn, 42, staff_id=1, chat_id=123))

    assert order == ["commit", "unlink"]
    assert removal.unlinked == ["ab/cd/abcd.pdf"]


def test_a_file_that_will_not_unlink_does_not_undo_the_deletion(removal, monkeypatch):
    async def unlink(_key, _wrapped_key=None):
        raise OSError("read-only file system")

    monkeypatch.setattr(document_removal.document_storage, "remove", unlink)

    outcome = asyncio.run(
        document_removal.remove_document(_Connection(), 42, staff_id=1, chat_id=123)
    )

    assert outcome.status == "removed"
    assert outcome.original_removed is False
    assert outcome.orphaned_storage_key == "ab/cd/abcd.pdf"


def test_the_answer_cache_is_dropped_because_the_corpus_changed(removal):
    """A cached answer was true for an archive that included this document."""
    asyncio.run(document_removal.remove_document(_Connection(), 42, staff_id=1, chat_id=123))

    assert removal.invalidated == 1


def test_an_unreachable_cache_does_not_refuse_the_deletion(removal, monkeypatch):
    """A stale answer is wrong for at most its TTL. A deletion refused because
    Redis was down is wrong until somebody notices."""
    async def broken():
        raise RuntimeError("redis is unreachable")

    monkeypatch.setattr(document_removal.cache, "invalidate_answers", broken)

    outcome = asyncio.run(
        document_removal.remove_document(_Connection(), 42, staff_id=1, chat_id=123)
    )

    assert outcome.status == "removed"


def test_a_missing_document_changes_nothing(removal, monkeypatch):
    async def nothing(_conn, _document_id, *, commit=True):
        return DocumentDeletion(status="not_found")

    monkeypatch.setattr(document_removal, "delete_document", nothing)
    conn = _Connection()

    outcome = asyncio.run(document_removal.remove_document(conn, 99, staff_id=1, chat_id=123))

    assert outcome.status == "not_found"
    assert removal.logged == []
    assert removal.unlinked == []
    assert conn.commits == 0
    assert conn.rollbacks == 1


# ── The confirmation button ─────────────────────────────────────────────────
# /excluir_documento was the one irreversible command still confirmed only by
# typing. The button is a shortcut into the same removal, never a second path:
# the tests below check that it deletes the document it was minted for, once,
# through the function the typed word uses, and only for an owner.

def _press(token: str, *, chat_id: int = 123, text: str = "⚠️ Isso apagará") -> dict:
    return {
        "update_id": 701,
        "callback_query": {
            "id": "cbq-doc",
            "data": token,
            "from": {"id": chat_id},
            "message": {
                "message_id": 77,
                "chat": {"id": chat_id, "type": "private"},
                "text": text,
            },
        },
    }


def _say(chat_id: int, text: str) -> None:
    asyncio.run(legal_message.process_legal_message("connection", _update(chat_id, text)))


def _wire_button(monkeypatch, *, staff=OWNER):
    """The deletion path with Telegram and the removal service recorded."""
    calls = SimpleNamespace(removed=[], sent=[], answered=[], edited=[], staff=staff)

    async def get_staff(*_args, **_kwargs):
        return calls.staff

    async def summary(_conn, document_id, scope=None):
        return _document(document_id)

    async def remove(_conn, document_id, *, staff_id, chat_id):
        calls.removed.append((document_id, staff_id))
        return document_removal.RemovalOutcome(
            status="removed", title="Contrato Acme", original_removed=True,
        )

    async def send(chat_id, text, reply_markup=None, **_kwargs):
        calls.sent.append((chat_id, text, reply_markup))
        return 77

    async def answer(_query_id, text=None, base_url=None, show_alert=False):
        calls.answered.append(text)
        return True

    async def edit(chat_id, message_id, text, base_url=None, reply_markup=None):
        calls.edited.append((chat_id, message_id, text))
        return True

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "get_document_summary", summary)
    monkeypatch.setattr(legal_message, "remove_document", remove)
    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(channels, "answer_callback_query", answer)
    monkeypatch.setattr(channels, "edit_message_text", edit)
    return calls


def _preview_token(calls) -> str:
    return calls.sent[-1][2]["inline_keyboard"][0][0]["callback_data"]


def test_the_preview_offers_a_button_for_that_document(monkeypatch, callback_store):
    calls = _wire_button(monkeypatch)

    _say(123, "/excluir_documento 42")

    _, text, markup = calls.sent[-1]
    assert "Contrato Acme" in text
    button = markup["inline_keyboard"][0][0]
    assert button["text"] == "🗑️ Confirmar exclusão"
    row = callback_store.rows[button["callback_data"]]
    assert (row["action"], row["operation"]) == ("document.delete", "confirm")
    assert row["resource"] == "document:42", "the button must be about this document"
    assert calls.removed == [], "the preview must not delete"


def test_the_button_and_the_word_give_the_same_answer(
    monkeypatch, callback_store, pending_store,
):
    """One function behind both, so what is said about the original file cannot
    differ with the way the owner confirmed."""
    calls = _wire_button(monkeypatch)
    _say(123, "/excluir_documento 42")
    _say(123, "confirmar")
    by_word = calls.sent[-1][1]

    _say(123, "/excluir_documento 42")
    asyncio.run(legal_message.process_legal_message(
        "connection", _press(_preview_token(calls)),
    ))
    by_button = calls.sent[-1][1]

    assert by_button == by_word
    assert calls.removed == [(42, 1), (42, 1)]


def test_pressing_the_button_closes_the_typed_step_and_rewrites_the_preview(
    monkeypatch, callback_store, pending_store,
):
    calls = _wire_button(monkeypatch)
    _say(123, "/excluir_documento 42")
    assert pending_store.waiting(123)

    asyncio.run(legal_message.process_legal_message(
        "connection", _press(_preview_token(calls)),
    ))

    assert calls.removed == [(42, 1)]
    assert not pending_store.waiting(123), "a later 'confirmar' must not delete again"
    assert calls.edited and "apagado do acervo" in calls.edited[-1][2]


def test_a_second_press_deletes_nothing_and_repeats_what_happened(
    monkeypatch, callback_store, pending_store,
):
    """A repeated press is answered from the result recorded by the first one,
    which says exactly what was deleted, rather than with a generic refusal."""
    calls = _wire_button(monkeypatch)
    _say(123, "/excluir_documento 42")
    token = _preview_token(calls)

    asyncio.run(legal_message.process_legal_message("connection", _press(token)))
    asyncio.run(legal_message.process_legal_message("connection", _press(token)))

    assert calls.removed == [(42, 1)]
    assert calls.answered[-1] and "apagado do acervo" in calls.answered[-1]


def test_a_button_retired_by_the_typed_word_says_the_deletion_is_done(
    monkeypatch, callback_store, pending_store,
):
    """Here there is no recorded result to repeat: the deletion happened by
    typing, and the button was retired. The refusal is the only honest answer,
    and it used to be written for access decisions."""
    calls = _wire_button(monkeypatch)
    _say(123, "/excluir_documento 42")
    token = _preview_token(calls)
    _say(123, "confirmar")

    asyncio.run(legal_message.process_legal_message("connection", _press(token)))

    assert calls.removed == [(42, 1)]
    assert calls.answered[-1] == "Essa exclusão já foi concluída."


def test_confirming_by_text_retires_the_button_it_did_not_use(
    monkeypatch, callback_store, pending_store,
):
    calls = _wire_button(monkeypatch)
    _say(123, "/excluir_documento 42")
    token = _preview_token(calls)

    _say(123, "confirmar")

    assert calls.removed == [(42, 1)]
    assert callback_store.state(token) == "cancelled"


def test_the_button_stops_working_when_the_owner_is_no_longer_one(
    monkeypatch, callback_store, pending_store,
):
    """The same person, demoted after the preview. A different person would be
    refused earlier, for not owning the button, and never reach the rank check
    this is about."""
    calls = _wire_button(monkeypatch)
    _say(123, "/excluir_documento 42")
    token = _preview_token(calls)

    calls.staff = OWNER.model_copy(update={"role": "admin"})
    asyncio.run(legal_message.process_legal_message("connection", _press(token)))

    assert calls.removed == []
    assert calls.answered[-1] and "permissão" in calls.answered[-1]


def test_an_expired_confirmation_does_not_send_the_owner_to_pending_decisions(
    monkeypatch, callback_store, pending_store,
):
    calls = _wire_button(monkeypatch)
    _say(123, "/excluir_documento 42")
    token = _preview_token(calls)
    callback_store.rows[token]["expires_at"] = time.monotonic() - 1

    asyncio.run(legal_message.process_legal_message("connection", _press(token)))

    assert calls.removed == []
    assert calls.answered[-1] and "expirou" in calls.answered[-1]
    assert "/pendentes" not in calls.answered[-1]


def test_the_user_removal_button_had_the_same_wrong_wording():
    """Found while adding this button: an expired confirmation to remove a user
    also pointed at /pendentes. Access decisions keep that wording."""
    removal = CallbackClaim(outcome="expired", action="staff.deactivate", operation="confirm")
    decision = CallbackClaim(outcome="expired", action="access.review", operation="approve")

    assert "/pendentes" not in legal_message._refusal_for(removal)
    assert "/pendentes" in legal_message._refusal_for(decision)
