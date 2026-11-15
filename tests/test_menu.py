"""The command list, and the sentences that open a flow without one.

The firm used to get a keyboard of navigation buttons: a main menu, a document
submenu, a way back. Telegram offers that list itself the moment somebody types
a slash, filtered as they keep typing, and it does it without leaving a
screenful of buttons behind in the conversation, so the navigation keyboard is
gone. What is left here is the list itself and the doors: a sentence that only
names an action ("quero fazer uma pesquisa") still opens the same flow, through
the same authorization action, because that was never about buttons.

The buttons that decide something specific are untouched and live elsewhere:
approving an access request, confirming a deletion. A static command list
cannot approve request NS-A7B2988C, and a confirmation is not navigation.
"""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core.handlers import legal_message
from core.services import channels
from core.models import AccessRequest, OfficialSource, StaffMember
from core.repositories.callback_actions import CallbackClaim

pytestmark = pytest.mark.usefixtures("isolate_message_rate_limits")

_INTERN = StaffMember(id=4, chat_id="400", name="Rita", role="estagiario")
_LAWYER = StaffMember(id=6, chat_id="600", name="Lia", role="advogado")
_DOCUMENT = SimpleNamespace(
    id=42, title="Contrato", doc_type="contrato", matter="Acme", created_at=None,
)


def _update(chat_id: int, text: str) -> dict:
    return {
        "update_id": 1,
        "message": {
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "first_name": "Pessoa"},
            "text": text,
        },
    }


def _wire(monkeypatch, staff):
    # The last answer delivered to each chat lives in a module-level dict. Left
    # alone it leaks from one test into the next, and a sentence is then read as
    # a follow-up to an answer that belonged to another test.
    legal_message._last_deliveries.clear()
    sent: list[str] = []

    async def get_staff(_conn, chat_id):
        return staff if str(chat_id) == str(staff.chat_id) else None

    async def answer(*args, **kwargs):
        return True

    async def edit(*args, **kwargs):
        return True

    async def send(_chat_id, text, reply_markup=None):
        sent.append(text)
        return 1

    async def typing(*args, **kwargs):
        return None

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(channels, "answer_callback_query", answer)
    monkeypatch.setattr(channels, "edit_message_text", edit)
    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(channels, "send_typing", typing)
    return sent


def _wire_fully(monkeypatch, *staff):
    """Like _wire, but keeps the chat and the keyboard each message carried."""
    legal_message._last_deliveries.clear()
    by_chat = {str(member.chat_id): member for member in staff}
    seen = SimpleNamespace(sent=[], answers=[])

    async def get_staff(_conn, chat_id):
        return by_chat.get(str(chat_id))

    async def answer(_query_id, text=None, **_kwargs):
        seen.answers.append(text)
        return True

    async def edit(*args, **kwargs):
        return True

    async def send(chat_id, text, reply_markup=None):
        seen.sent.append((str(chat_id), text, reply_markup))
        return 1

    async def typing(*args, **kwargs):
        return None

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(channels, "answer_callback_query", answer)
    monkeypatch.setattr(channels, "edit_message_text", edit)
    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(channels, "send_typing", typing)
    return seen


async def _no_folder(_conn, _doc_id):
    """Where a document lives, for the tests that are not about folders."""
    return None


def _run(update):
    asyncio.run(legal_message.process_legal_message("connection", update))


# ── asking for the list ─────────────────────────────────────────────────────


@pytest.mark.parametrize("message", ["/comandos", "/ajuda", "/start", "/help"])
def test_the_list_arrives_as_plain_text(monkeypatch, callback_store, message):
    """/start is here because it is what Telegram sends the first time anybody
    opens the bot, which is the one moment the list is pushed rather than
    asked for. /ajuda and /help stay because they are the words a person
    reaches for, and answering "comando desconhecido" to a request for help is
    a poor way to answer it."""
    seen = _wire_fully(monkeypatch, _LAWYER)

    _run(_update(600, message))

    _chat, text, markup = seen.sent[-1]
    assert text == legal_message._help_for(_LAWYER)
    assert markup is None, f"{message!r} still came back with a keyboard"
    assert not callback_store.rows, "nothing here should mint a button"


def test_the_list_names_the_command_that_shows_it():
    """The first line of the help is the command that produced it, so a person
    who scrolls back knows how to ask again."""
    assert "/comandos" in legal_message._HELP
    assert "lista os comandos" in legal_message._HELP
    assert "/menu" not in legal_message._HELP, "the old navigation name is gone"


def test_the_list_still_says_more_to_a_rank_that_may_do_more():
    """Removing the buttons must not flatten what each rank is told about."""
    owner = StaffMember(id=1, chat_id="100", name="Paulo", role="owner")

    intern_help = legal_message._help_for(_INTERN)
    owner_help = legal_message._help_for(owner)

    assert "/promover" in owner_help
    assert "/promover" not in intern_help
    assert len(owner_help) > len(intern_help)


@pytest.mark.parametrize("sentence", ["quais comandos", "menu", "ajuda"])
def test_asking_in_prose_points_at_the_command(monkeypatch, callback_store, sentence):
    """The list is pushed once, at the first contact. After that it is a
    command away: twenty lines arriving because somebody wrote "quais comandos"
    mid-conversation is the noise this change exists to remove.

    "menu" is still recognised in prose even though /menu is gone, because it
    stays the word people reach for and it has no legal meaning to protect.
    """
    sent = _wire(monkeypatch, _LAWYER)

    _run(_update(600, sentence))

    assert "/comandos" in sent[-1]
    assert sent[-1] != legal_message._help_for(_LAWYER), "prose should point, not repeat"
    assert not callback_store.rows


def test_the_welcome_is_the_list_itself(monkeypatch, callback_store):
    """The one moment it is pushed: somebody who has just been let in has no
    way to know what to ask for yet."""
    admin = StaffMember(id=9, chat_id="111", name="Admin", role="admin")
    newcomer = StaffMember(id=12, chat_id="987", name="Pessoa Nova", role="estagiario")
    seen = _wire_fully(monkeypatch, admin, newcomer)
    request = AccessRequest(
        id=3, request_code="NS-12AB34CD", chat_id="987",
        name="Pessoa Nova", status="approved",
    )

    async def decide(*_args, **_kwargs):
        return request

    monkeypatch.setattr(legal_message, "decide_access_request", decide)

    _run(_update(111, "/autorizar ns-12ab34cd"))

    _chat, text, markup = next(item for item in seen.sent if item[0] == "987")
    assert text.startswith("✅ Seu acesso foi liberado")
    # The newcomer's own list, not the admin's: an intern must not be welcomed
    # with the commands of whoever approved them.
    assert legal_message._help_for(newcomer) in text
    assert "/promover" not in text
    assert markup is None
    assert not [r for r in callback_store.rows.values() if r["chat_id"] == "987"]


# ── a capability that does not exist is never offered ───────────────────────


def test_word_and_excel_editing_is_offered_nowhere():
    """No code answers for it yet. The roadmap's rule is that a capability that
    does not exist must never be advertised, because the honest way to keep
    that promise is to not offer the door at all. It used to be checked against
    the button labels; the command list is where the offering lives now.
    """
    offered = " ".join(
        f"{entry['command']} {entry['description']}"
        for entry in legal_message.command_menu()
    ).lower()

    assert "word" not in offered
    assert "excel" not in offered


# ── the doors: a sentence that only names an action ─────────────────────────
# These never depended on the buttons. A sentence that names what somebody
# wants opens the flow; a sentence carrying the content is the question itself.


@pytest.mark.parametrize("sentence, question", [
    ("quero fazer uma pesquisa", "prazo para embargos de declaração"),
    ("preciso redigir um documento", "cláusula de confidencialidade"),
])
def test_naming_the_action_opens_the_flow(
    monkeypatch, callback_store, pending_store, sentence, question,
):
    _wire(monkeypatch, _LAWYER)
    reached = []

    async def answer_question(_conn, text, **kwargs):
        reached.append(text)
        return SimpleNamespace(text="resposta", sources=[])

    monkeypatch.setattr(legal_message, "answer_question", answer_question)

    _run(_update(600, sentence))
    assert pending_store.waiting(600), "the sentence did not open the flow"

    _run(_update(600, question))
    assert reached == [question], "the question after the door must reach the specialist"
    assert not pending_store.waiting(600)


def test_asking_to_see_the_documents_points_at_the_command(monkeypatch, callback_store):
    """It used to open a submenu of eight buttons. The command does the same
    thing in one line, and /docs is in the Telegram list beside it."""
    sent = _wire(monkeypatch, _LAWYER)

    _run(_update(600, "quero ver um documento"))

    assert "/docs" in sent[-1]
    assert not callback_store.rows


@pytest.mark.parametrize("sentence, action", [
    ("quero fazer uma pesquisa", "research.external"),
    ("preciso redigir um documento", "drafting.compose"),
    ("quero ver um documento", "document.browse"),
])
def test_a_door_is_gated_by_the_action_it_opens(
    monkeypatch, callback_store, pending_store, sentence, action,
):
    """The gate is on the action, never on how it was asked. Denying the action
    must close the sentence too, or typing becomes a way around a door somebody
    was not given. "quero ver um documento" is gated on document.browse now,
    which is the action it actually reaches, rather than on the navigation
    action that no longer exists.
    """
    sent = _wire(monkeypatch, _LAWYER)
    real_decide = legal_message.authorization.decide

    def deny(staff, asked):
        if asked == action:
            return SimpleNamespace(allowed=False, reason="negado neste teste")
        return real_decide(staff, asked)

    monkeypatch.setattr(legal_message.authorization, "decide", deny)

    _run(_update(600, sentence))

    assert sent == ["negado neste teste"]
    assert not pending_store.waiting(600)
    assert not callback_store.rows


def test_a_door_right_after_an_answer_is_not_an_export(
    monkeypatch, callback_store, pending_store,
):
    """Right after an answer, "quero ver um documento" was read as "export that
    answer": "quero" is an export verb, "documento" is a file word, and the
    export follow-up runs before anything else. Reproduced the way it happens,
    with a real answer first.
    """
    sent = _wire(monkeypatch, _LAWYER)

    async def answer_question(_conn, text, **kwargs):
        return SimpleNamespace(text="resposta", sources=[])

    monkeypatch.setattr(legal_message, "answer_question", answer_question)

    _run(_update(600, "prazo para embargos de declaração"))
    _run(_update(600, "quero ver um documento"))

    assert "/docs" in sent[-1]
    assert "formato" not in sent[-1]


def test_asking_for_the_answer_as_a_file_still_asks_the_format(
    monkeypatch, callback_store, pending_store,
):
    """The fix above must not cost the export it was protecting from."""
    sent = _wire(monkeypatch, _LAWYER)

    async def answer_question(_conn, text, **kwargs):
        return SimpleNamespace(text="resposta", sources=[])

    monkeypatch.setattr(legal_message, "answer_question", answer_question)

    _run(_update(600, "prazo para embargos de declaração"))
    _run(_update(600, "quero isso em arquivo"))

    assert "formato" in sent[-1]


# ── the commands that never needed a button ─────────────────────────────────


def test_detalhes_command_works_directly(monkeypatch):
    sent = _wire(monkeypatch, _LAWYER)

    async def get_summary(_conn, doc_id, scope=None):
        return _DOCUMENT if doc_id == 42 else None

    async def no_official_page(_conn, _doc_id, scope=None):
        return None

    monkeypatch.setattr(legal_message, "get_document_summary", get_summary)
    monkeypatch.setattr(legal_message, "get_official_source", no_official_page)
    monkeypatch.setattr(legal_message, "folder_of", _no_folder)

    _run(_update(600, "/detalhes 42"))

    assert "[42] Contrato" in sent[-1]
    assert "Página oficial" not in sent[-1], "a contract has no official page"


def test_detalhes_names_the_page_a_law_is_kept_current_against(monkeypatch):
    sent = _wire(monkeypatch, _LAWYER)
    page = "https://www.planalto.gov.br/ccivil_03/leis/l8245.htm"

    async def get_summary(_conn, doc_id, scope=None):
        return _DOCUMENT if doc_id == 42 else None

    async def linked(_conn, doc_id, scope=None):
        return OfficialSource(id=doc_id, title="Lei 8.245", source_url=page,
                              checked_at=datetime(2026, 9, 21, 15, 0, tzinfo=timezone.utc))

    monkeypatch.setattr(legal_message, "get_document_summary", get_summary)
    monkeypatch.setattr(legal_message, "get_official_source", linked)
    monkeypatch.setattr(legal_message, "folder_of", _no_folder)

    _run(_update(600, "/detalhes 42"))

    assert f"Página oficial: {page}" in sent[-1]
    assert "Conferida em: 21/09/2026" in sent[-1]


def test_detalhes_of_a_missing_document_says_so(monkeypatch):
    sent = _wire(monkeypatch, _LAWYER)

    async def get_summary(_conn, _doc_id, scope=None):
        return None

    monkeypatch.setattr(legal_message, "get_document_summary", get_summary)

    _run(_update(600, "/detalhes 999"))

    assert "Não encontrei" in sent[-1]


# ── the buttons that stayed ─────────────────────────────────────────────────


def test_an_expired_access_decision_keeps_its_own_wording():
    """Approving an access request is still a button, and still a decision on
    one specific request, so an expired one still sends a manager to
    /pendentes."""
    claim = CallbackClaim(outcome="expired", action="access.review", operation="approve")

    assert "/pendentes" in legal_message._refusal_for(claim)


@pytest.mark.parametrize("action", ["document.delete", "staff.deactivate"])
def test_an_expired_confirmation_says_to_ask_again(action):
    """The other surviving button confirms something irreversible. It must not
    borrow the access decision's wording, which would send an owner to a list
    of pending requests they were never asked about."""
    claim = CallbackClaim(outcome="expired", action=action, operation="confirm")

    refusal = legal_message._refusal_for(claim)

    assert "/pendentes" not in refusal
    assert "confirm" in refusal.lower()
