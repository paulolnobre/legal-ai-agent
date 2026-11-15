"""Role hierarchy and human approval flow for Telegram access."""
import asyncio
import inspect
import re
from types import SimpleNamespace

import pytest

from core.handlers import legal_message
from core.services import channels
from core.models import AccessRequest, StaffMember
from core.repositories import staff as staff_repository

pytestmark = pytest.mark.usefixtures("isolate_message_rate_limits")


def _update(chat_id: int, text: str, *, name: str = "Pessoa Nova") -> dict:
    return {
        "update_id": 501,
        "message": {
            "chat": {"id": chat_id},
            "text": text,
            "from": {"first_name": name, "username": "pessoa_nova"},
        },
    }


def test_request_codes_are_short_unique_and_telegram_friendly():
    code = staff_repository.new_request_code()
    assert re.fullmatch(r"NS-[A-F0-9]{8}", code)
    assert staff_repository.new_request_code() != code


def test_new_person_receives_code_and_managers_are_notified(monkeypatch):
    sent = []
    request = AccessRequest(
        id=1,
        request_code="NS-12AB34CD",
        chat_id="987",
        name="Pessoa Nova",
        username="pessoa_nova",
    )

    async def unknown_staff(*args, **kwargs):
        return None

    async def create_request(*args, **kwargs):
        return request, True

    async def managers(*args, **kwargs):
        return [StaffMember(id=1, chat_id="123", name="Paulo", role="owner")]

    async def send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text))

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", unknown_staff)
    monkeypatch.setattr(legal_message, "create_or_refresh_access_request", create_request)
    monkeypatch.setattr(legal_message, "list_staff", managers)
    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(
        legal_message,
        "_denied_limiter",
        SimpleNamespace(check=lambda _chat_id: (True, False)),
    )

    asyncio.run(legal_message.process_legal_message("connection", _update(987, "olá")))

    assert sent[0][0] == 987
    assert "NS-12AB34CD" in sent[0][1]
    assert sent[1][0] == "123"
    assert "/autorizar NS-12AB34CD" in sent[1][1]


def test_admin_can_approve_code_and_user_receives_welcome(monkeypatch, callback_store):
    # The welcome now mints the newcomer's menu. The store keeps that on the
    # real path instead of the one where minting fails.
    sent = []
    admin = StaffMember(id=9, chat_id="111", name="Admin", role="admin")
    request = AccessRequest(
        id=3,
        request_code="NS-12AB34CD",
        chat_id="987",
        name="Pessoa Nova",
        status="approved",
    )

    async def get_staff(*args, **kwargs):
        return admin

    async def decide(*args, **kwargs):
        assert kwargs == {"approved": True, "decided_by": 9}
        return request

    async def send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text))

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "decide_access_request", decide)
    monkeypatch.setattr(legal_message, "_send", send)

    asyncio.run(
        legal_message.process_legal_message("connection", _update(111, "/autorizar ns-12ab34cd"))
    )

    assert "liberado, como estagiário(a)" in sent[0][1]
    assert sent[1][0] == "987"
    assert "Seu acesso foi liberado" in sent[1][1]


def test_only_owner_can_promote_a_user_to_admin(monkeypatch):
    sent = []
    owner = StaffMember(id=1, chat_id="123", name="Paulo", role="owner")
    target = StaffMember(id=7, chat_id="777", name="Ana", role="user")
    promoted = target.model_copy(update={"role": "admin"})

    async def get_staff(*args, **kwargs):
        return owner

    async def get_target(*args, **kwargs):
        return target

    async def set_role(*args, **kwargs):
        assert args[1:] == (7, "admin")
        return promoted

    async def send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text))

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "get_staff_by_id", get_target)
    monkeypatch.setattr(legal_message, "set_staff_role", set_role)
    monkeypatch.setattr(legal_message, "_send", send)

    asyncio.run(legal_message.process_legal_message("connection", _update(123, "/promover 7")))

    assert sent[0] == (123, "✅ Ana agora é admin.")
    assert sent[1] == ("777", "Seu papel no Assistente Legal Demo & Associados foi alterado para admin.")


def test_owner_can_ask_for_team_directory_in_natural_language(monkeypatch):
    sent = []
    owner = StaffMember(id=1, chat_id="123", name="Paulo", role="owner")

    async def get_staff(*args, **kwargs):
        return owner

    async def users(*args, **kwargs):
        return [
            owner,
            StaffMember(id=7, chat_id="777", name="Ana", role="admin"),
            StaffMember(id=8, chat_id="888", name="Bruno", role="user"),
        ]

    async def send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text))

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "list_staff", users)
    monkeypatch.setattr(legal_message, "_send", send)

    asyncio.run(
        legal_message.process_legal_message(
            "connection", _update(123, "tem como eu ver a lista de funcionarios?", name="Paulo")
        )
    )

    assert sent == [
        (123, "👥 Equipe autorizada:\n[1] Paulo (owner)\n[7] Ana (admin)\n[8] Bruno (advogado)")
    ]



def _directory_case(monkeypatch, staff, text):
    sent = []

    async def get_staff(*args, **kwargs):
        return staff

    async def users(*args, **kwargs):
        return [
            StaffMember(id=1, chat_id="123", name="Paulo", role="owner"),
            StaffMember(id=7, chat_id="777", name="Ana", role="admin"),
        ]

    async def send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text))

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "list_staff", users)
    monkeypatch.setattr(legal_message, "_send", send)
    assert staff.chat_id is not None, "the staff fixtures above all carry a chat id"
    asyncio.run(
        legal_message.process_legal_message("connection", _update(int(staff.chat_id), text))
    )
    return sent


_DIRECTORY = "👥 Equipe autorizada:\n[1] Paulo (owner)\n[7] Ana (admin)"


def test_admin_can_ask_for_team_directory_in_natural_language(monkeypatch):
    admin = StaffMember(id=7, chat_id="777", name="Ana", role="admin")
    sent = _directory_case(
        monkeypatch, admin, "Você consegue mandar para mim a equipe do escritório?"
    )
    assert sent == [(777, _DIRECTORY)]


def test_admin_can_list_the_team_through_the_command(monkeypatch):
    admin = StaffMember(id=7, chat_id="777", name="Ana", role="admin")
    assert _directory_case(monkeypatch, admin, "/usuarios") == [(777, _DIRECTORY)]


def test_equipe_is_an_alias_for_the_team_directory_command(monkeypatch):
    # Reported in production: the chat agent itself suggested "/equipe" as the
    # command name, which then fell through to the LLM instead of the roster.
    admin = StaffMember(id=7, chat_id="777", name="Ana", role="admin")
    assert _directory_case(monkeypatch, admin, "/equipe") == [(777, _DIRECTORY)]


def test_plain_user_is_still_refused_the_team_directory(monkeypatch):
    user = StaffMember(id=8, chat_id="888", name="Bruno", role="user")
    sent = _directory_case(monkeypatch, user, "me manda a equipe do escritorio")
    assert sent == [(888, "🔒 Este comando é exclusivo de admins e do owner.")]


def test_admin_cannot_promote_users(monkeypatch):
    sent = []
    admin = StaffMember(id=9, chat_id="111", name="Admin", role="admin")

    async def get_staff(*args, **kwargs):
        return admin

    async def send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text))

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "_send", send)

    asyncio.run(legal_message.process_legal_message("connection", _update(111, "/promover 7")))

    assert sent == [(111, "🔒 Este comando é exclusivo do owner.")]


def test_admin_cannot_exclude_users(monkeypatch):
    sent = []
    admin = StaffMember(id=9, chat_id="111", name="Admin", role="admin")

    async def get_staff(*args, **kwargs):
        return admin

    async def send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text))

    async def must_not_deactivate(*args, **kwargs):  # pragma: no cover
        raise AssertionError("an admin must never reach account deactivation")

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "deactivate_staff", must_not_deactivate)
    monkeypatch.setattr(legal_message, "_send", send)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(111, "/excluir_usuario 7 confirmar")
    ))

    assert sent == [(111, "🔒 Este comando é exclusivo do owner.")]


def test_owner_sees_the_target_before_confirming_user_exclusion(monkeypatch):
    sent = []
    owner = StaffMember(id=1, chat_id="123", name="Paulo", role="owner")
    target = StaffMember(id=7, chat_id="777", name="Ana", role="admin")

    async def get_staff(_conn, identifier):
        return owner if identifier == 123 else target

    async def must_not_deactivate(*args, **kwargs):  # pragma: no cover
        raise AssertionError("the preview must not revoke access")

    async def send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text))

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "get_staff_by_id", get_staff)
    monkeypatch.setattr(legal_message, "deactivate_staff", must_not_deactivate)
    monkeypatch.setattr(legal_message, "_send", send)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(123, "/excluir_usuario 7")
    ))

    assert len(sent) == 1
    assert "Ana (admin)" in sent[0][1]
    assert "/excluir_usuario 7 confirmar" in sent[0][1]
    assert "histórico de auditoria" in sent[0][1]


def test_owner_can_confirm_user_exclusion(monkeypatch):
    sent = []
    calls = []
    owner = StaffMember(id=1, chat_id="123", name="Paulo", role="owner")
    target = StaffMember(id=7, chat_id="777", name="Ana", role="admin", active=False)

    async def get_staff(*args, **kwargs):
        return owner

    async def deactivate(conn, **kwargs):
        calls.append((conn, kwargs))
        return staff_repository.StaffDeactivation("deactivated", target)

    async def send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text))

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "deactivate_staff", deactivate)
    monkeypatch.setattr(legal_message, "_send", send)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(123, "/excluir_usuario 7 confirmar")
    ))

    assert calls == [("connection", {"actor_id": 1, "target_id": 7})]
    assert sent[0][0] == 123 and "acesso de Ana foi excluído" in sent[0][1]
    assert sent[1] == (
        "777",
        "Seu acesso ao Assistente Legal Demo & Associados foi revogado pelo owner.",
    )


def test_revogar_is_a_deactivation_alias_not_a_role_demotion(monkeypatch):
    owner = StaffMember(id=1, chat_id="123", name="Paulo", role="owner")
    called = []

    async def get_staff(*args, **kwargs):
        return owner

    async def deactivate(*args, **kwargs):
        called.append(kwargs)
        return staff_repository.StaffDeactivation("already_inactive")

    async def must_not_change_role(*args, **kwargs):  # pragma: no cover
        raise AssertionError("/revogar must not demote a role")

    async def send(*args, **kwargs):
        return None

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "deactivate_staff", deactivate)
    monkeypatch.setattr(legal_message, "set_staff_role", must_not_change_role)
    monkeypatch.setattr(legal_message, "_send", send)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(123, "/revogar 7 confirmar")
    ))

    assert called == [{"actor_id": 1, "target_id": 7}]


def test_owner_accounts_cannot_be_excluded_by_command(monkeypatch):
    sent = []
    owner = StaffMember(id=1, chat_id="123", name="Paulo", role="owner")
    other_owner = StaffMember(id=2, chat_id="222", name="Outro", role="owner")

    async def get_actor(*args, **kwargs):
        return owner

    async def get_target(*args, **kwargs):
        return other_owner

    async def send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text))

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_actor)
    monkeypatch.setattr(legal_message, "get_staff_by_id", get_target)
    monkeypatch.setattr(legal_message, "_send", send)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(123, "/excluir_usuario 2")
    ))

    assert sent == [(123, "Contas owner não podem ser excluídas por este comando.")]


def test_bare_autorizar_asks_for_the_code_and_decides_on_the_next_message(monkeypatch, pending_store):
    sent = []
    admin = StaffMember(id=9, chat_id="111", name="Admin", role="admin")
    pending = AccessRequest(
        id=3,
        request_code="NS-A7B2988C",
        chat_id="987",
        name="Helena Lobato",
        username="helena",
    )
    approved = pending.model_copy(update={"status": "approved"})
    decided = []

    async def get_staff(*args, **kwargs):
        return admin

    async def list_pending(*args, **kwargs):
        return [pending]

    async def decide(_conn, code, **kwargs):
        decided.append((code, kwargs))
        return approved

    async def send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text))

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "list_pending_access_requests", list_pending)
    monkeypatch.setattr(legal_message, "decide_access_request", decide)
    monkeypatch.setattr(legal_message, "_send", send)

    asyncio.run(legal_message.process_legal_message("connection", _update(111, "/autorizar")))

    assert "código" in sent[0][1]
    assert "NS-A7B2988C" in sent[0][1]
    assert not decided

    # The bare code typed as the next message completes the decision.
    asyncio.run(legal_message.process_legal_message("connection", _update(111, "a7b2988c")))

    assert decided == [("NS-A7B2988C", {"approved": True, "decided_by": 9})]
    assert "liberado, como estagiário(a)" in sent[1][1]
    assert not pending_store.waiting(111)


def test_invalid_access_code_keeps_the_pending_decision_open(monkeypatch, pending_store):
    sent = []
    admin = StaffMember(id=9, chat_id="111", name="Admin", role="admin")
    pending = AccessRequest(
        id=3,
        request_code="NS-A7B2988C",
        chat_id="987",
        name="Helena Lobato",
    )
    attempts = []

    async def get_staff(*args, **kwargs):
        return admin

    async def list_pending(*args, **kwargs):
        return [pending]

    async def decide(_conn, code, **kwargs):
        attempts.append(code)
        return pending.model_copy(update={"status": "approved"}) if code == pending.request_code else None

    async def send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text))

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "list_pending_access_requests", list_pending)
    monkeypatch.setattr(legal_message, "decide_access_request", decide)
    monkeypatch.setattr(legal_message, "_send", send)

    asyncio.run(legal_message.process_legal_message("connection", _update(111, "/autorizar")))
    asyncio.run(legal_message.process_legal_message("connection", _update(111, "código errado")))

    assert pending_store.waiting(111)
    assert "Não encontrei" in sent[-1][1]

    asyncio.run(legal_message.process_legal_message("connection", _update(111, "a7b2988c")))

    assert attempts == ["CÓDIGO ERRADO", "NS-A7B2988C"]
    assert not pending_store.waiting(111)


def test_pending_code_question_is_dropped_by_cancel(monkeypatch, pending_store):
    sent = []
    admin = StaffMember(id=9, chat_id="111", name="Admin", role="admin")

    async def get_staff(*args, **kwargs):
        return admin

    async def send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text))

    async def decide(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("no decision should be taken after /cancelar")

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "decide_access_request", decide)
    monkeypatch.setattr(legal_message, "_send", send)
    asyncio.run(legal_message._remember_pending_decision("connection", 111, True))

    asyncio.run(legal_message.process_legal_message("connection", _update(111, "/cancelar")))

    assert "nenhuma decisão" in sent[0][1]
    assert not pending_store.waiting(111)


def test_bare_recusar_without_pending_requests_says_so(monkeypatch, pending_store):
    sent = []
    admin = StaffMember(id=9, chat_id="111", name="Admin", role="admin")

    async def get_staff(*args, **kwargs):
        return admin

    async def list_pending(*args, **kwargs):
        return []

    async def send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text))

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "list_pending_access_requests", list_pending)
    monkeypatch.setattr(legal_message, "_send", send)

    asyncio.run(legal_message.process_legal_message("connection", _update(111, "/recusar")))

    assert "Não há solicitações" in sent[0][1]
    assert not pending_store.waiting(111)


def test_asking_for_the_command_list_in_prose_points_at_the_command(monkeypatch):
    """The list is pushed once, at the first contact, and is a command away
    after that. Asking in prose gets the pointer rather than twenty lines
    arriving mid-conversation. That the list itself is role-specific is checked
    in tests/test_menu.py, where the list is actually produced."""
    sent = []
    owner = StaffMember(id=1, chat_id="123", name="Paulo", role="owner")

    async def get_staff(*args, **kwargs):
        return owner

    async def send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text))

    async def should_not_reach_agent(*args, **kwargs):
        raise AssertionError("the command list must not be produced by the model")

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(legal_message, "answer_question", should_not_reach_agent)

    asyncio.run(
        legal_message.process_legal_message(
            "connection", _update(123, "me passe a lista de comandos", name="Paulo")
        )
    )

    assert len(sent) == 1
    reply = sent[0][1]
    assert "/comandos" in reply
    assert "/promover" not in reply, "the pointer is not the list"


def _command_case(monkeypatch, staff, text):
    """Send one command, with the model wired to explode if it is ever reached."""
    sent = []

    async def get_staff(*args, **kwargs):
        return staff

    async def send(chat_id, text, reply_markup=None):
        sent.append(text)

    async def should_not_reach_agent(*args, **kwargs):
        raise AssertionError("a slash command must never be answered by the model")

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(legal_message, "answer_question", should_not_reach_agent)
    assert staff.chat_id is not None, "the staff fixtures above all carry a chat id"
    asyncio.run(
        legal_message.process_legal_message("connection", _update(int(staff.chat_id), text))
    )
    return sent


def test_an_unknown_command_is_answered_from_the_help_not_by_the_model(monkeypatch):
    owner = StaffMember(id=1, chat_id="123", name="Paulo", role="owner")
    sent = _command_case(monkeypatch, owner, "/listar")

    assert len(sent) == 1
    assert "Não conheço" in sent[0]
    assert "/docs" in sent[0], "the real command list must be shown"


def test_a_mistyped_command_gets_its_correction(monkeypatch):
    owner = StaffMember(id=1, chat_id="123", name="Paulo", role="owner")
    assert _command_case(monkeypatch, owner, "/usuario") == [
        "Não conheço /usuario. Você quis dizer /usuarios?"
    ]


def test_a_real_command_the_person_may_not_run_says_so_instead(monkeypatch):
    """Refused is not unknown: the command exists, the role does not allow it."""
    user = StaffMember(id=8, chat_id="888", name="Bruno", role="user")
    sent = _command_case(monkeypatch, user, "/autorizar NS-12AB34CD")

    assert sent == ["🔒 Este comando é exclusivo de admins e do owner."]


def test_the_unknown_command_list_respects_the_role(monkeypatch):
    user = StaffMember(id=8, chat_id="888", name="Bruno", role="user")
    sent = _command_case(monkeypatch, user, "/listar")

    assert "/docs" in sent[0]
    assert "/promover" not in sent[0], "a user must not learn the owner's commands"
    assert "/excluir_usuario" not in sent[0]


class _RepositoryCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.executed = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def execute(self, statement, params=None):
        self.executed.append((" ".join(statement.split()), params))

    async def fetchone(self):
        return self.rows.pop(0)


class _RepositoryConnection:
    def __init__(self, rows):
        self.cursor_instance = _RepositoryCursor(rows)
        self.commits = 0

    def cursor(self):
        return self.cursor_instance

    async def commit(self):
        self.commits += 1


def test_repository_deactivation_is_audited_and_preserves_the_staff_row():
    conn = _RepositoryConnection([
        (1, "owner", True),
        (7, "777", "ana@example.test", "Ana", "admin", True),
    ])

    result = asyncio.run(staff_repository.deactivate_staff(
        conn, actor_id=1, target_id=7,
    ))

    assert result.status == "deactivated"
    assert result.staff and result.staff.active is False
    statements = conn.cursor_instance.executed
    assert any("UPDATE staff SET active = FALSE" in sql for sql, _ in statements)
    audit = next(item for item in statements if "INSERT INTO staff_management_log" in item[0])
    assert audit[1] == (1, 7, "admin")
    assert not any("DELETE FROM staff" in sql for sql, _ in statements)
    assert conn.commits == 1


def test_repository_deactivation_is_idempotent():
    conn = _RepositoryConnection([
        (1, "owner", True),
        (7, "777", None, "Ana", "advogado", False),
    ])

    result = asyncio.run(staff_repository.deactivate_staff(
        conn, actor_id=1, target_id=7,
    ))

    assert result.status == "already_inactive"
    assert not any(
        "UPDATE staff" in sql or "INSERT INTO staff_management_log" in sql
        for sql, _ in conn.cursor_instance.executed
    )
    assert conn.commits == 1


def test_repository_repeats_owner_authorization_below_the_handler():
    conn = _RepositoryConnection([(9, "admin", True)])

    with pytest.raises(staff_repository.StaffDeactivationError) as error:
        asyncio.run(staff_repository.deactivate_staff(conn, actor_id=9, target_id=7))

    assert error.value.code == "actor_not_owner"
    assert len(conn.cursor_instance.executed) == 1


@pytest.mark.parametrize(
    ("target", "code"),
    [
        ((1, "123", None, "Paulo", "owner", True), "self"),
        ((2, "222", None, "Outro", "owner", True), "target_owner"),
    ],
)
def test_repository_never_deactivates_an_owner(target, code):
    conn = _RepositoryConnection([(1, "owner", True), target])

    with pytest.raises(staff_repository.StaffDeactivationError) as error:
        asyncio.run(staff_repository.deactivate_staff(
            conn, actor_id=1, target_id=target[0],
        ))

    assert error.value.code == code
    assert not any(
        "UPDATE staff" in sql or "INSERT INTO staff_management_log" in sql
        for sql, _ in conn.cursor_instance.executed
    )


def test_fresh_database_schema_contains_the_staff_management_audit():
    from pathlib import Path

    schema = " ".join(Path("infra/init.sql").read_text(encoding="utf-8").split())

    assert "CREATE TABLE IF NOT EXISTS staff_management_log" in schema
    assert "actor_staff_id INTEGER NOT NULL REFERENCES staff(id)" in schema
    assert "target_staff_id INTEGER NOT NULL REFERENCES staff(id)" in schema
    migration = " ".join(inspect.getsource(staff_repository.ensure_access_schema).split())
    assert "CREATE TABLE IF NOT EXISTS staff_management_log" in migration


# ── The removal button: same service, same confirmation ─────────────────────

def _press(token: str, *, chat_id: int = 123, text: str = "⚠️ Isso revogará") -> dict:
    return {
        "update_id": 9,
        "callback_query": {
            "id": "cbq-del",
            "data": token,
            "from": {"id": chat_id},
            "message": {
                "message_id": 77,
                "chat": {"id": chat_id, "type": "private"},
                "text": text,
            },
        },
    }


def _wire_removal(monkeypatch, *, staff, target=None):
    """The removal path with Telegram and the deactivation service recorded."""
    calls = SimpleNamespace(deactivated=[], sent=[], answered=[], edited=[], listed=[])

    async def get_staff_by_chat(*_args, **_kwargs):
        return staff

    async def get_by_id(_conn, identifier):
        return target

    async def deactivate(_conn, **kwargs):
        calls.deactivated.append(kwargs)
        return staff_repository.StaffDeactivation("deactivated", target)

    async def send(chat_id, text, reply_markup=None):
        calls.sent.append((chat_id, text, reply_markup))
        return 77

    async def answer(_query_id, text=None, base_url=None, show_alert=False):
        calls.answered.append(text)
        return True

    async def edit(chat_id, message_id, text, base_url=None, reply_markup=None):
        calls.edited.append((chat_id, message_id, text))
        return True

    async def listing(_conn, **_kwargs):
        calls.listed.append(True)
        return [target] if target else []

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff_by_chat)
    monkeypatch.setattr(legal_message, "get_staff_by_id", get_by_id)
    monkeypatch.setattr(legal_message, "deactivate_staff", deactivate)
    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(channels, "answer_callback_query", answer)
    monkeypatch.setattr(channels, "edit_message_text", edit)
    monkeypatch.setattr(legal_message, "list_staff", listing)
    return calls


_OWNER = StaffMember(id=1, chat_id="123", name="Paulo", role="owner")
_TARGET = StaffMember(id=7, chat_id="777", name="Ana", role="admin")


def test_the_preview_offers_a_button_that_finishes_what_it_describes(
    monkeypatch, callback_store,
):
    calls = _wire_removal(monkeypatch, staff=_OWNER, target=_TARGET)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(123, "/excluir_usuario 7"),
    ))

    _chat, text, keyboard = calls.sent[-1]
    assert calls.deactivated == [], "the preview must not revoke anything"
    button = keyboard["inline_keyboard"][0][0]
    assert button["text"] == "🗑️ Confirmar exclusão"
    row = callback_store.rows[button["callback_data"]]
    assert row["action"] == "staff.deactivate" and row["operation"] == "confirm"
    assert row["resource"] == "staff:7", "the button carries who, not what to say"
    assert "7" not in button["callback_data"], "the target must not travel in the payload"
    # The typed paths stay: the button is an addition, not a gate.
    assert "confirmar" in text and "/excluir_usuario 7 confirmar" in text


def test_pressing_confirm_revokes_the_access_through_the_same_service(
    monkeypatch, callback_store, pending_store,
):
    calls = _wire_removal(monkeypatch, staff=_OWNER, target=_TARGET)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(123, "/excluir_usuario 7"),
    ))
    token = calls.sent[-1][2]["inline_keyboard"][0][0]["callback_data"]
    asyncio.run(legal_message.process_legal_message("connection", _press(token)))

    assert calls.deactivated == [{"actor_id": 1, "target_id": 7}]
    owner_message = calls.sent[-2]
    assert owner_message[0] == 123 and "acesso de Ana foi excluído" in owner_message[1]
    assert calls.sent[-1][0] == "777", "the person is told they lost access"
    # The same step was also awaiting the word confirmar; a press closes it.
    assert not pending_store.waiting(123)


def test_the_confirmed_preview_stops_looking_live(monkeypatch, callback_store):
    calls = _wire_removal(monkeypatch, staff=_OWNER, target=_TARGET)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(123, "/excluir_usuario 7"),
    ))
    token = calls.sent[-1][2]["inline_keyboard"][0][0]["callback_data"]
    asyncio.run(legal_message.process_legal_message("connection", _press(token)))

    assert calls.edited, "the message that carried the button is rewritten"
    assert "excluído" in calls.edited[-1][2]


def test_pressing_confirm_twice_revokes_once(monkeypatch, callback_store):
    calls = _wire_removal(monkeypatch, staff=_OWNER, target=_TARGET)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(123, "/excluir_usuario 7"),
    ))
    token = calls.sent[-1][2]["inline_keyboard"][0][0]["callback_data"]
    asyncio.run(legal_message.process_legal_message("connection", _press(token)))
    asyncio.run(legal_message.process_legal_message("connection", _press(token)))

    assert len(calls.deactivated) == 1


def test_confirming_by_text_retires_the_button_it_did_not_use(
    monkeypatch, callback_store,
):
    """Both paths settle the same thing, so finishing by typing must not leave
    a button that still offers to remove an account already removed."""
    calls = _wire_removal(monkeypatch, staff=_OWNER, target=_TARGET)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(123, "/excluir_usuario 7"),
    ))
    token = calls.sent[-1][2]["inline_keyboard"][0][0]["callback_data"]

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(123, "confirmar"),
    ))

    assert calls.deactivated == [{"actor_id": 1, "target_id": 7}]
    assert callback_store.state(token) == "cancelled"


def test_a_press_that_is_not_a_confirmation_opens_the_flow_instead(
    monkeypatch, callback_store, pending_store,
):
    """Nothing mints this any more: it was the main menu's entry, and the menu
    is gone. The branch stays as the safe landing for a press that is not a
    confirmation, including a button left in somebody's chat history from
    before the removal. It opens the same question the bare command does,
    because skipping to the deletion would be a shorter path to an
    irreversible act."""
    calls = _wire_removal(monkeypatch, staff=_OWNER, target=_TARGET)
    token = asyncio.run(callback_store.issue(
        None, chat_id="123", staff_id=1, issued_role="owner",
        action="staff.deactivate", operation="deactivate",
        ttl_seconds=legal_message._CONFIRMATION_TTL, channel="telegram",
    ))

    asyncio.run(legal_message.process_legal_message("connection", _press(token)))

    assert calls.deactivated == []
    assert "ID do usuário" in calls.sent[-1][1]
    assert pending_store.kind(123) == "identifier"


def test_a_removal_button_stops_working_when_the_owner_is_no_longer_one(
    monkeypatch, callback_store,
):
    calls = _wire_removal(monkeypatch, staff=_OWNER, target=_TARGET)
    asyncio.run(legal_message.process_legal_message(
        "connection", _update(123, "/excluir_usuario 7"),
    ))
    token = calls.sent[-1][2]["inline_keyboard"][0][0]["callback_data"]

    demoted = StaffMember(id=1, chat_id="123", name="Paulo", role="admin")
    after = _wire_removal(monkeypatch, staff=demoted, target=_TARGET)
    asyncio.run(legal_message.process_legal_message("connection", _press(token)))

    assert after.deactivated == [], "the rank is re-read on the press"
    assert "permissão" in after.answered[-1]


# ── Withdrawing a request you made yourself ─────────────────────────────────

def test_a_person_waiting_can_withdraw_their_own_request(monkeypatch):
    """Somebody who asked by mistake had no way back: every message they sent
    only refreshed the request they were trying to be rid of."""
    sent = []
    cancelled = []

    async def no_staff(*_args, **_kwargs):
        return None

    async def cancel(_conn, chat_id):
        cancelled.append(str(chat_id))
        return AccessRequest(
            id=3, request_code="NS-A7B2988C", chat_id=str(chat_id), name="Helena",
        )

    async def must_not_refresh(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("withdrawing must not reopen the request")

    async def retire(_conn, **_kwargs):
        return []

    async def send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text))
        return 1

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", no_staff)
    monkeypatch.setattr(legal_message, "cancel_access_request", cancel)
    monkeypatch.setattr(
        legal_message, "create_or_refresh_access_request", must_not_refresh,
    )
    monkeypatch.setattr(legal_message.callback_actions, "cancel_siblings", retire)
    monkeypatch.setattr(legal_message, "_send", send)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(555, "/cancelar"),
    ))

    assert cancelled == ["555"]
    assert "foi retirada" in sent[-1][1]
    # Withdrawal is not a door closing: the next message opens a new request,
    # and saying so is what keeps it from reading as a ban.
    assert "nova" in sent[-1][1].lower()


def test_withdrawing_takes_the_decision_off_every_manager_screen(monkeypatch):
    """A request nobody can decide any more must stop offering the decision."""
    edited = []

    async def no_staff(*_args, **_kwargs):
        return None

    async def cancel(_conn, _chat_id):
        return AccessRequest(
            id=3, request_code="NS-A7B2988C", chat_id="555", name="Helena",
        )

    async def retire(_conn, *, resource, **_kwargs):
        assert resource == "NS-A7B2988C"
        return [("900", "telegram", "42"), ("901", "telegram", "43")]

    async def edit(chat_id, message_id, text, base_url=None, reply_markup=None):
        edited.append((chat_id, message_id, text))
        assert reply_markup is None, "a settled request must not keep its keyboard"
        return True

    async def send(*_args, **_kwargs):
        return 1

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", no_staff)
    monkeypatch.setattr(legal_message, "cancel_access_request", cancel)
    monkeypatch.setattr(legal_message.callback_actions, "cancel_siblings", retire)
    monkeypatch.setattr(channels, "edit_message_text", edit)
    monkeypatch.setattr(legal_message, "_send", send)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(555, "/cancelar"),
    ))

    assert [row[0] for row in edited] == ["900", "901"]
    assert "Retirada pela própria pessoa" in edited[0][2]
    assert "NS-A7B2988C" in edited[0][2]


def test_withdrawing_when_there_is_nothing_to_withdraw_says_so(monkeypatch):
    sent = []

    async def no_staff(*_args, **_kwargs):
        return None

    async def cancel(_conn, _chat_id):
        return None

    async def send(chat_id, text, reply_markup=None):
        sent.append(text)
        return 1

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", no_staff)
    monkeypatch.setattr(legal_message, "cancel_access_request", cancel)
    monkeypatch.setattr(legal_message, "_send", send)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(555, "/cancelar"),
    ))

    assert "nenhuma solicitação" in sent[-1]


def test_the_waiting_message_says_withdrawal_is_possible(monkeypatch):
    """An option nobody is told about is an option nobody has."""
    sent = []

    async def no_staff(*_args, **_kwargs):
        return None

    async def refresh(_conn, **kwargs):
        return AccessRequest(
            id=3, request_code="NS-A7B2988C", chat_id=str(kwargs["chat_id"]),
            name=kwargs["name"],
        ), False

    async def send(chat_id, text, reply_markup=None):
        sent.append(text)
        return 1

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", no_staff)
    monkeypatch.setattr(legal_message, "create_or_refresh_access_request", refresh)
    monkeypatch.setattr(legal_message, "_send", send)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(555, "oi, preciso de acesso"),
    ))

    assert "/cancelar" in sent[-1]


def test_a_manager_deciding_a_withdrawn_request_is_told_it_is_settled(monkeypatch):
    """The repository refuses anything that is no longer pending, so the two
    paths cannot both believe they won."""
    owner = StaffMember(id=1, chat_id="123", name="Paulo", role="owner")
    sent = []

    async def get_staff(*_args, **_kwargs):
        return owner

    async def decide(*_args, **_kwargs):
        return None  # what decide_access_request returns for a settled row

    async def send(chat_id, text, reply_markup=None):
        sent.append(text)
        return 1

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "decide_access_request", decide)
    monkeypatch.setattr(legal_message, "_send", send)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(123, "/autorizar NS-A7B2988C"),
    ))

    assert "pendente" in sent[-1].lower() or "não encontrei" in sent[-1].lower()
