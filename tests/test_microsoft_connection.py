"""A lawyer connects their own Microsoft account, and never types a password here.

The device code flow is the point: the agent shows a code, the person signs in
at microsoft.com/devicelogin, and what reaches this server is a token that
Microsoft issued and that the person can revoke. Nothing in this file should
ever be able to see a password, because there is no path that carries one.

Three failures worth pinning: a refresh token written where the backup can
read it, a sign-in that blocks the single update worker for fifteen minutes,
and a "connected" message sent for a sign-in that never completed.
"""
import asyncio
from datetime import datetime, timezone

import httpx
import pytest
from cryptography.fernet import Fernet

from core.config import settings
from core.handlers import legal_message
from core.integrations import microsoft
from core.models import StaffMember
from core.repositories import microsoft_accounts
from core.services.authorization import decide

pytestmark = pytest.mark.usefixtures(
    "isolate_message_rate_limits", "pending_store", "callback_store",
)

_LAWYER = StaffMember(id=6, chat_id="600", name="Lia", role="advogado")
_INTERN = StaffMember(id=4, chat_id="400", name="Rita", role="estagiario")


@pytest.fixture
def sealed(monkeypatch):
    """A real Fernet key, so the sealing is exercised and not stubbed."""
    monkeypatch.setattr(settings, "ms_token_key", Fernet.generate_key().decode())


# ── the token at rest ───────────────────────────────────────────────────────


def test_a_refresh_token_is_encrypted_before_it_is_stored(sealed):
    """The backup leaves this host every night; MS_TOKEN_KEY does not."""
    token = "0.AXoA-a-real-looking-refresh-token"

    box = microsoft_accounts.seal(token)

    assert token.encode() not in box
    assert microsoft_accounts.unseal(box) == token


def test_a_token_sealed_with_another_key_is_refused_not_guessed(sealed, monkeypatch):
    box = microsoft_accounts.seal("token")
    monkeypatch.setattr(settings, "ms_token_key", Fernet.generate_key().decode())

    with pytest.raises(microsoft_accounts.TokenSealError, match="connect again"):
        microsoft_accounts.unseal(box)


def test_without_a_key_nothing_is_stored_and_the_operator_is_told_how_to_fix_it(monkeypatch):
    """Failing closed is the floor: an empty key would be refused by Fernet
    anyway. What the guard adds is an error somebody can act on, which is the
    difference between a five-minute fix and a support thread."""
    monkeypatch.setattr(settings, "ms_token_key", None)

    with pytest.raises(microsoft_accounts.TokenSealError) as refused:
        microsoft_accounts.seal("token")

    assert "MS_TOKEN_KEY" in str(refused.value)
    assert "Fernet.generate_key" in str(refused.value), "say how to make one"


def test_the_stored_row_carries_the_sealed_bytes_and_one_account_per_person(sealed):
    conn = _Conn()

    asyncio.run(microsoft_accounts.save_account(
        conn, 6, account_name="lia@escritorio.adv.br", refresh_token="secret-token",
    ))

    sql, params = conn.statements[0]
    assert "ON CONFLICT (staff_id) DO UPDATE" in sql
    assert params[1] == "lia@escritorio.adv.br"
    assert isinstance(params[2], bytes) and b"secret-token" not in params[2]


# ── the sign-in flow ────────────────────────────────────────────────────────


def _token_response(status, payload):
    return httpx.Response(status, json=payload)


def _stub_microsoft(monkeypatch, handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(microsoft, "_http", lambda: client)
    monkeypatch.setattr(settings, "ms_client_id", "app-id")


def test_the_code_to_show_comes_from_microsoft(monkeypatch):
    _stub_microsoft(monkeypatch, lambda request: _token_response(200, {
        "user_code": "H7QK2M", "verification_uri": "https://microsoft.com/devicelogin",
        "device_code": "dev-code", "interval": 5, "expires_in": 900,
    }))

    flow = asyncio.run(microsoft.start_device_flow())

    assert flow.user_code == "H7QK2M"
    assert flow.device_code == "dev-code"
    assert flow.expires_in == 900


def test_only_read_scopes_are_ever_requested(monkeypatch):
    """A write scope not requested cannot be misused by a bug. Creating or
    changing a file in somebody's drive is behind an approval flow that does
    not exist yet."""
    asked = {}

    def handler(request):
        asked.update(dict(httpx.QueryParams(request.content.decode())))
        return _token_response(200, {"user_code": "X", "verification_uri": "u",
                                     "device_code": "d", "interval": 5, "expires_in": 900})

    _stub_microsoft(monkeypatch, handler)
    asyncio.run(microsoft.start_device_flow())

    assert asked["scope"] == "Files.Read offline_access User.Read"
    assert "Write" not in asked["scope"] and "ReadWrite" not in asked["scope"]


def test_a_pending_sign_in_is_a_state_not_an_error(monkeypatch):
    _stub_microsoft(monkeypatch, lambda request: _token_response(
        400, {"error": "authorization_pending"}))

    with pytest.raises(microsoft.SignInPending):
        asyncio.run(microsoft.poll_once("dev-code"))


def test_a_refusal_or_an_expired_code_is_told_apart_from_an_outage(monkeypatch):
    for error in ("authorization_declined", "expired_token"):
        _stub_microsoft(monkeypatch, lambda request, e=error: _token_response(400, {"error": e}))
        with pytest.raises(microsoft.SignInDeclined):
            asyncio.run(microsoft.poll_once("dev-code"))


def test_a_sign_in_without_a_refresh_token_is_refused(monkeypatch):
    """Without offline_access the connection would silently last one hour."""
    _stub_microsoft(monkeypatch, lambda request: _token_response(
        200, {"access_token": "at"}))

    with pytest.raises(microsoft.MicrosoftError, match="refresh token"):
        asyncio.run(microsoft.poll_once("dev-code"))


def test_slow_down_widens_the_interval_rather_than_being_ignored(monkeypatch):
    """A client that polls faster than it was told is the one that gets
    throttled."""
    calls = {"n": 0}
    slept: list[float] = []

    async def poll(_device_code):
        calls["n"] += 1
        if calls["n"] == 1:
            raise microsoft.SignInPending("slow_down")
        return microsoft.Tokens(access_token="at", refresh_token="rt")

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(microsoft, "poll_once", poll)
    monkeypatch.setattr(microsoft.asyncio, "sleep", sleep)

    flow = microsoft.DeviceFlow("C", "u", "d", interval=5, expires_in=900)
    asyncio.run(microsoft.wait_for_sign_in(flow))

    assert slept == [5, 10], "the second wait is longer, as Microsoft asked"


def test_a_refreshed_token_replaces_the_stored_one_when_microsoft_rotates_it(monkeypatch):
    _stub_microsoft(monkeypatch, lambda request: _token_response(
        200, {"access_token": "new-at", "refresh_token": "rotated-rt"}))

    tokens = asyncio.run(microsoft.access_token_from("old-rt"))

    assert tokens.refresh_token == "rotated-rt"


def test_a_refresh_that_microsoft_rejects_means_the_person_reconnects(monkeypatch):
    _stub_microsoft(monkeypatch, lambda request: _token_response(
        400, {"error": "invalid_grant", "error_description": "revoked"}))

    with pytest.raises(microsoft.SignInDeclined):
        asyncio.run(microsoft.access_token_from("revoked-rt"))


# ── the command ─────────────────────────────────────────────────────────────


class _Cursor:
    def __init__(self, state):
        self.state = state
        self.rowcount = 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self.state.statements.append((" ".join(sql.split()), list(params or [])))

    async def fetchone(self):
        return self.state.row


class _Conn:
    def __init__(self, row=None):
        self.statements = []
        self.row = row
        self.commits = 0

    def cursor(self):
        return _Cursor(self)

    async def commit(self):
        self.commits += 1


@pytest.fixture(autouse=True)
def no_sign_in_left_over():
    """Per test, not per call: a test about a second /microsoft needs the
    first one's state to still be there."""
    legal_message._MS_SIGNING_IN.clear()
    yield
    legal_message._MS_SIGNING_IN.clear()


def _command(monkeypatch, staff, text, *, account=None, flow=None, forgotten=True):
    sent: list[str] = []
    spawned: list[str] = []

    async def get_staff(_conn, chat_id):
        return staff if str(chat_id) == str(staff.chat_id) else None

    async def send(_chat_id, message, reply_markup=None):
        sent.append(message)
        return 1

    async def get_account(_conn, _staff_id):
        return account

    async def forget(_conn, _staff_id):
        return forgotten

    async def start_flow():
        if flow is None:
            raise microsoft.MicrosoftError("nope")
        return flow

    class _FakeTask:
        """Hashable, because the handler keeps its tasks in a set."""

        def add_done_callback(self, _callback):
            return None

        def cancel(self):
            return None

    def create_task(coro, name=None):
        spawned.append(name or "")
        coro.close()  # the waiting is tested separately, not here
        return _FakeTask()

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(legal_message, "get_account", get_account)
    monkeypatch.setattr(legal_message, "forget_account", forget)
    monkeypatch.setattr(legal_message.microsoft, "start_device_flow", start_flow)
    monkeypatch.setattr(legal_message.microsoft, "configured", lambda: True)
    monkeypatch.setattr(legal_message.asyncio, "create_task", create_task)
    update = {"update_id": 1, "message": {
        "chat": {"id": int(staff.chat_id), "type": "private"},
        "from": {"id": int(staff.chat_id)}, "text": text,
    }}
    asyncio.run(legal_message.process_legal_message("connection", update))
    return sent, spawned


def test_every_rank_may_connect_their_own_account():
    assert decide(_INTERN, "microsoft.connect").allowed
    assert decide(_LAWYER, "microsoft.connect").allowed


def test_connecting_shows_the_code_and_says_the_password_stays_with_microsoft(monkeypatch):
    flow = microsoft.DeviceFlow("H7QK2M", "https://microsoft.com/devicelogin", "d", 5, 900)

    sent, spawned = _command(monkeypatch, _LAWYER, "/microsoft", flow=flow)

    assert "H7QK2M" in sent[-1]
    assert "microsoft.com/devicelogin" in sent[-1]
    assert "senha não passa por mim" in sent[-1]
    assert "15 minutos" in sent[-1]


def test_the_wait_happens_off_the_worker(monkeypatch):
    """The update worker serves one message at a time. Waiting inline would
    stop the assistant answering anybody for as long as the code is valid."""
    flow = microsoft.DeviceFlow("H7QK2M", "u", "d", 5, 900)

    _sent, spawned = _command(monkeypatch, _LAWYER, "/microsoft", flow=flow)

    assert spawned == ["ms-signin-6"], "the sign-in runs as its own task"


def test_a_second_request_while_one_is_pending_does_not_start_another(monkeypatch):
    flow = microsoft.DeviceFlow("H7QK2M", "u", "d", 5, 900)
    _command(monkeypatch, _LAWYER, "/microsoft", flow=flow)

    sent, spawned = _command(monkeypatch, _LAWYER, "/microsoft", flow=flow)

    assert spawned == [], "a code nobody is polling is worse than no code"
    assert "ainda estou esperando" in sent[-1]


def test_a_connected_account_shows_who_and_since_when(monkeypatch):
    account = microsoft_accounts.ConnectedAccount(
        staff_id=6, account_name="lia@escritorio.adv.br",
        connected_at=datetime(2026, 9, 20, 12, tzinfo=timezone.utc), last_used_at=None,
    )

    sent, spawned = _command(monkeypatch, _LAWYER, "/microsoft", account=account)

    assert spawned == []
    assert "lia@escritorio.adv.br" in sent[-1]
    assert "20/09/2026" in sent[-1]
    assert "/microsoft desconectar" in sent[-1]


def test_disconnecting_says_what_it_did_and_did_not_do(monkeypatch):
    """Dropping the token here is not revoking it at Microsoft, and saying so
    is the difference between a person who is covered and one who thinks so."""
    sent, _spawned = _command(monkeypatch, _LAWYER, "/microsoft desconectar")

    assert "desconectei" in sent[-1].casefold()
    assert "Microsoft" in sent[-1] and "segurança" in sent[-1]


def test_disconnecting_what_was_never_connected_says_so(monkeypatch):
    sent, _spawned = _command(monkeypatch, _LAWYER, "/microsoft desconectar", forgotten=False)

    assert "não tem nenhuma conta" in sent[-1]


def test_the_menu_offers_it_in_the_firms_words():
    menu = {entry["command"]: entry["description"] for entry in legal_message.command_menu()}
    assert menu["microsoft"] == "conecta a sua conta Microsoft, para o agente ler os seus arquivos"
