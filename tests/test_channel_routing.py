"""The handler answers the person being served through the bound channel.

Telegram is the only channel until the panel's arrives, so these bind a
recording channel of their own and check where each kind of reply lands:
text, the typing indicator, a file, a button press. A notice to somebody
else must still go by Telegram whatever channel serves the person.
"""
import asyncio
import re
from pathlib import Path

import pytest

from core.handlers import legal_message
from core.services import channels

pytestmark = pytest.mark.usefixtures("isolate_message_rate_limits")


class _Recording:
    name = "recording"
    records_itself = False
    max_text = None

    def __init__(self):
        self.calls: list[tuple] = []

    async def send_text(self, chat_id, text, buttons, *, preview):
        self.calls.append(("text", str(chat_id), text, buttons))
        return "r1"

    async def send_file(self, chat_id, filename, data, caption, *, kind, document_id):
        self.calls.append(("file", str(chat_id), filename, kind))
        return "r2"

    async def typing(self, chat_id):
        self.calls.append(("typing", str(chat_id)))

    async def edit_text(self, chat_id, reference, text):
        self.calls.append(("edit", str(chat_id), reference, text))

    async def ack_button(self, callback_id, text):
        self.calls.append(("ack", callback_id, text))


def _telegram_spy(monkeypatch) -> list[tuple]:
    """Every Telegram call, by function and chat."""
    calls: list[tuple] = []

    def spy(name):
        async def fake(*args, **kwargs):
            calls.append((name, str(args[0]) if args else None))
            return 1 if name == "send_message" else True
        return fake

    for name in ("send_message", "send_document", "send_typing", "edit_message_text",
                 "answer_callback_query"):
        monkeypatch.setattr(channels, name, spy(name))
    return calls


def test_every_reply_to_the_served_person_goes_through_the_current_channel(monkeypatch):
    telegram = _telegram_spy(monkeypatch)
    fake = _Recording()
    legal_message._last_deliveries["700"] = ("Resposta", "texto da resposta", 1e18)

    async def build(fmt, title, text, listing):
        return b"PK", "resposta.docx"

    monkeypatch.setattr(legal_message, "build_async", build)

    async def run():
        with channels.serving(fake, 700):  # type: ignore[arg-type]
            await legal_message._send(700, "Olá")
            await legal_message._export_last_answer("700", "docx")

    try:
        asyncio.run(run())
    finally:
        legal_message._last_deliveries.pop("700", None)

    kinds = [call[0] for call in fake.calls]
    assert kinds == ["text", "typing", "file"]
    assert fake.calls[2] == ("file", "700", "resposta.docx", "export")
    assert telegram == []


def test_a_notice_to_another_person_goes_by_telegram_even_while_another_channel_serves(
        monkeypatch):
    telegram = _telegram_spy(monkeypatch)
    fake = _Recording()

    async def run():
        with channels.serving(fake, 700):  # type: ignore[arg-type]
            await legal_message._send(900, "🔔 Nova solicitação de acesso")

    asyncio.run(run())

    assert fake.calls == []
    assert telegram == [("send_message", "900")]


def test_telegram_splits_a_long_reply_and_keys_the_last_part(monkeypatch):
    sent: list[tuple[str, object]] = []

    async def send(chat_id, text, base_url=None, parse_mode=None, reply_markup=None,
                   preview=True):
        sent.append((text, reply_markup))
        return 1

    monkeypatch.setattr(channels, "send_message", send)
    keyboard = {"inline_keyboard": [[{"text": "Ajudou", "callback_data": "t1"}]]}

    async def run():
        with channels.serving(channels.TELEGRAM, 700):
            await legal_message._send(700, "x" * 9000, reply_markup=keyboard)

    asyncio.run(run())

    assert [len(text) for text, _ in sent] == [4000, 4000, 1000]
    assert [markup for _, markup in sent] == [None, None, keyboard]


def test_a_reply_is_one_message_where_the_channel_has_no_limit(monkeypatch):
    _telegram_spy(monkeypatch)
    fake = _Recording()

    async def run():
        with channels.serving(fake, 700):  # type: ignore[arg-type]
            await legal_message._send(700, "x" * 9000)

    asyncio.run(run())

    assert [(kind, len(text)) for kind, _chat, text, _b in fake.calls] == [("text", 9000)]


def test_buttons_reach_the_channel_as_buttons(monkeypatch):
    _telegram_spy(monkeypatch)
    fake = _Recording()
    keyboard = {"inline_keyboard": [[{"text": "Apagar", "callback_data": "t9"},
                                     {"text": "Cancelar", "callback_data": "t10"}]]}

    async def run():
        with channels.serving(fake, 700):  # type: ignore[arg-type]
            await legal_message._send(700, "Apagar?", reply_markup=keyboard)

    asyncio.run(run())

    assert fake.calls == [("text", "700", "Apagar?", [[channels.Button("Apagar", "t9"),
                                                       channels.Button("Cancelar", "t10")]])]


def test_a_button_press_is_served_inside_the_conversation(monkeypatch, callback_store):
    """The press binds the person like a message does, so what it says is
    recorded as their conversation (revision 9)."""
    from core.models import AccessRequest, StaffMember

    admin = StaffMember(id=9, chat_id="900", name="Ivo", role="admin")
    request = AccessRequest(id=3, request_code="NS-A7B2988C", chat_id="987", name="Helena")
    recorded: list[tuple[str, str]] = []
    _telegram_spy(monkeypatch)

    async def get_staff(_conn, chat_id):
        return admin if str(chat_id) == "900" else None

    async def decide(_conn, code, *, approved, decided_by):
        return request

    async def record(_conn, *, staff_id, chat_id, speaker, text, **_kwargs):
        recorded.append((speaker, text))

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "decide_access_request", decide)
    monkeypatch.setattr(legal_message.conversation, "record_turn", record)
    token = asyncio.run(callback_store.issue(
        None, chat_id="900", staff_id=9, issued_role="admin", action="access.review",
        operation="approve", resource=request.request_code,
        ttl_seconds=legal_message._CALLBACK_TTL, channel="telegram"))

    asyncio.run(legal_message.process_legal_message("connection", {
        "update_id": 5,
        "callback_query": {"id": "cbq", "data": token, "from": {"id": 900},
                           "message": {"message_id": 42, "chat": {"id": 900,
                                                                  "type": "private"}}},
    }))

    assert any(speaker == "bot" for speaker, _ in recorded), recorded


def test_no_direct_telegram_call_is_left_in_the_handler():
    source = Path("core/handlers/legal_message.py").read_text(encoding="utf-8")
    calls = re.findall(r"(?<![\w.])(send_typing|send_document|edit_message_text|"
                       r"answer_callback_query|send_message)\(", source)

    assert calls == []


def test_the_microsoft_task_replies_by_telegram_with_no_inherited_conversation(monkeypatch):
    """The sign-in finishes minutes after the update that started it: it must
    not reach for that update's connection or channel (revision 10)."""
    telegram = _telegram_spy(monkeypatch)
    fake = _Recording()
    recorded: list[str] = []

    async def wait(_flow):
        return type("Tokens", (), {"access_token": "a", "refresh_token": "r"})()

    async def whoami(_token):
        return "lia@escritorio.com.br"

    async def save(*_args, **_kwargs):
        return None

    class _Pool:
        def connection(self):
            import contextlib

            @contextlib.asynccontextmanager
            async def opened():
                yield "fresh-connection"
            return opened()

    async def get_pool():
        return _Pool()

    async def record(_conn, *_args, **_kwargs):
        recorded.append("recorded")

    monkeypatch.setattr(legal_message.microsoft, "wait_for_sign_in", wait)
    monkeypatch.setattr(legal_message.microsoft, "whoami", whoami)
    monkeypatch.setattr(legal_message, "save_account", save)
    monkeypatch.setattr(legal_message, "get_pool", get_pool)
    monkeypatch.setattr(legal_message, "_record", record)

    async def run():
        token = legal_message._conversation.set(("closed-connection", 6, "700"))
        try:
            with channels.serving(fake, 700):  # type: ignore[arg-type]
                task = asyncio.create_task(
                    legal_message._await_microsoft_sign_in(object(), "700", 6))
            await task
        finally:
            legal_message._conversation.reset(token)

    asyncio.run(run())

    assert fake.calls == []
    assert ("send_message", "700") in telegram
    assert recorded == []


def test_retiring_buttons_does_not_rewrite_the_served_persons_telegram_copy_while_another_channel_serves():  # noqa: E501
    fake = _Recording()
    fake.name = "panel"

    with channels.serving(fake, 700):  # type: ignore[arg-type]
        assert legal_message._channel_for_copy("700", "telegram") is None
        assert legal_message._channel_for_copy("900", "telegram") is channels.TELEGRAM
        assert legal_message._channel_for_copy("700", "panel") is fake
        assert legal_message._channel_for_copy("900", "panel") is None
    with channels.serving(channels.TELEGRAM, 700):
        assert legal_message._channel_for_copy("700", "telegram") is channels.TELEGRAM


def test_a_managers_notification_button_issued_while_the_panel_serves_is_a_telegram_button(
        monkeypatch, callback_store):
    from core.models import AccessRequest, StaffMember

    _telegram_spy(monkeypatch)
    fake = _Recording()
    fake.name = "panel"
    manager = StaffMember(id=9, chat_id="900", name="Ivo", role="admin")
    request = AccessRequest(id=3, request_code="NS-A7B2988C", chat_id="987", name="Helena")

    async def managers(_conn, roles):
        return [manager]

    monkeypatch.setattr(legal_message, "list_staff", managers)

    async def run():
        with channels.serving(fake, 555):  # type: ignore[arg-type]
            await legal_message._notify_managers("connection", request)

    asyncio.run(run())

    assert {row["channel"] for row in callback_store.rows.values()} == {"telegram"}
    assert {row["reference"] for row in callback_store.rows.values()} == {"1"}


class _Conn:
    """Enough of a connection for a press: it counts rollbacks."""

    def __init__(self):
        self.rollbacks = 0

    async def rollback(self):
        self.rollbacks += 1


def test_a_button_whose_action_failed_says_so_when_pressed_again(monkeypatch, callback_store):
    from core.models import StaffMember

    admin = StaffMember(id=9, chat_id="900", name="Ivo", role="admin")
    answered: list[str | None] = []
    _telegram_spy(monkeypatch)

    async def answer(_query_id, text=None, base_url=None, show_alert=False):
        answered.append(text)
        return True

    async def get_staff(_conn, chat_id):
        return admin if str(chat_id) == "900" else None

    async def broken(*_args, **_kwargs):
        raise RuntimeError("database went away mid-decision")

    monkeypatch.setattr(channels, "answer_callback_query", answer)
    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "decide_access_request", broken)
    token = asyncio.run(callback_store.issue(
        None, chat_id="900", staff_id=9, issued_role="admin", action="access.review",
        operation="approve", resource="NS-A7B2988C",
        ttl_seconds=legal_message._CALLBACK_TTL, channel="telegram"))
    press = {"update_id": 5, "callback_query": {
        "id": "cbq", "data": token, "from": {"id": 900},
        "message": {"message_id": 42, "chat": {"id": 900, "type": "private"}}}}

    conn = _Conn()
    with pytest.raises(RuntimeError):
        asyncio.run(legal_message.process_legal_message(conn, press))
    asyncio.run(legal_message.process_legal_message(conn, press))

    assert answered[-1] == "Não consegui concluir. Tente pelo comando."
    assert conn.rollbacks == 1, "the failed action's writes are rolled back first"


def test_a_press_cut_off_by_the_worker_timeout_records_that_it_failed(
        monkeypatch, callback_store):
    """The worker cancels an update at its time limit; the spent press must
    still say it did not finish."""
    from core.models import StaffMember

    admin = StaffMember(id=9, chat_id="900", name="Ivo", role="admin")
    _telegram_spy(monkeypatch)

    async def slow(*_args, **_kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(legal_message, "_decide_access", slow)
    token = asyncio.run(callback_store.issue(
        None, chat_id="900", staff_id=9, issued_role="admin", action="access.review",
        operation="approve", resource="NS-A7B2988C",
        ttl_seconds=legal_message._CALLBACK_TTL, channel="telegram"))

    async def answered(_text=None):
        return None

    async def run():
        await asyncio.wait_for(legal_message._run_callback(
            _Conn(), {"text": "Solicitação"}, "900", 42, admin, token, answered), 0.05)

    with pytest.raises(TimeoutError):
        asyncio.run(run())

    assert callback_store.rows[token]["result"] == legal_message._PRESS_FAILED


def test_copies_with_the_same_reference_on_two_channels_are_both_considered(
        monkeypatch, callback_store):
    """A reference means something only on its own channel: a panel row "42"
    must not stand in for the Telegram message 42 in the same chat."""
    from core.models import AccessRequest, StaffMember

    admin = StaffMember(id=9, chat_id="900", name="Ivo", role="admin")
    request = AccessRequest(id=3, request_code="NS-A7B2988C", chat_id="987", name="Helena")
    telegram = _telegram_spy(monkeypatch)

    async def get_staff(_conn, chat_id):
        return admin if str(chat_id) == "900" else None

    async def decide(_conn, code, *, approved, decided_by):
        return request

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "decide_access_request", decide)

    async def offer(chat_id, staff_id, channel, reference):
        token = await callback_store.issue(
            None, chat_id=chat_id, staff_id=staff_id, issued_role="admin",
            action="access.review", operation="approve", resource=request.request_code,
            ttl_seconds=legal_message._CALLBACK_TTL, channel=channel)
        await callback_store.attach_reference(None, [token], reference=reference)
        return token

    async def setup():
        pressed = await offer("900", 9, "telegram", "41")
        await offer("901", 10, "panel", "42")
        await offer("901", 10, "telegram", "42")
        return pressed

    token = asyncio.run(setup())
    asyncio.run(legal_message.process_legal_message(_Conn(), {
        "update_id": 6,
        "callback_query": {"id": "cbq", "data": token, "from": {"id": 900},
                           "message": {"message_id": 41, "text": "Solicitação",
                                       "chat": {"id": 900, "type": "private"}}},
    }))

    assert ("edit_message_text", "901") in telegram


def test_a_press_cut_off_after_its_decision_keeps_the_decisions_result(
        monkeypatch, callback_store):
    from core.models import AccessRequest, StaffMember

    admin = StaffMember(id=9, chat_id="900", name="Ivo", role="admin")
    request = AccessRequest(id=3, request_code="NS-A7B2988C", chat_id="987", name="Helena")
    _telegram_spy(monkeypatch)

    async def decide(_conn, code, *, approved, decided_by):
        return request

    async def welcome(_conn, _request):
        return None

    monkeypatch.setattr(legal_message, "decide_access_request", decide)
    monkeypatch.setattr(legal_message, "_welcome", welcome)
    token = asyncio.run(callback_store.issue(
        None, chat_id="900", staff_id=9, issued_role="admin", action="access.review",
        operation="approve", resource=request.request_code,
        ttl_seconds=legal_message._CALLBACK_TTL, channel="telegram"))

    async def answered(_text=None):
        await asyncio.sleep(30)  # the final acknowledgement hangs

    async def run():
        with channels.serving(channels.TELEGRAM, 900):
            await asyncio.wait_for(legal_message._run_callback(
                _Conn(), {"text": "Solicitação"}, "900", 42, admin, token, answered), 0.05)

    with pytest.raises(TimeoutError):
        asyncio.run(run())

    assert callback_store.rows[token]["result"] == "Autorizado."


def _listing_world(monkeypatch):
    from core.models import StaffMember

    sent: list[str] = []

    async def send(_chat_id, text, **_kwargs):
        sent.append(text)

    async def typing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(channels.TELEGRAM, "typing", typing)
    return StaffMember(id=1, chat_id="100", name="Paulo", role="owner"), sent


def test_the_last_onedrive_listing_is_one_per_person_whichever_way_the_chat_id_arrived(
        monkeypatch):
    """Telegram's chat id is a number, the panel's text: "trazer 1" means the
    line on the screen the person looked at last, from either."""
    from types import SimpleNamespace

    paulo, _sent = _listing_world(monkeypatch)
    listings = iter([[SimpleNamespace(name="A.pdf", size=1, modified="")],
                     [SimpleNamespace(name="B.pdf", size=1, modified="")]])
    imported: list[str] = []

    async def list_files(_conn, _staff_id, query=""):
        return next(listings)

    async def import_file(_conn, _staff_id, item):
        imported.append(item.name)
        return SimpleNamespace(chunk_count=1, document_id=9)

    monkeypatch.setattr(legal_message.microsoft_files, "list_files", list_files)
    monkeypatch.setattr(legal_message.microsoft_files, "import_file", import_file)

    try:
        asyncio.run(legal_message._handle_onedrive("c", 100, paulo, []))
        asyncio.run(legal_message._handle_onedrive("c", "100", paulo, []))
        asyncio.run(legal_message._handle_onedrive("c", 100, paulo, ["trazer", "1"]))
    finally:
        legal_message._LAST_DRIVE_LISTING.clear()

    assert imported == ["B.pdf"]


def test_the_last_process_listing_is_one_per_person_whichever_way_the_chat_id_arrived(
        monkeypatch):
    from types import SimpleNamespace

    paulo, _sent = _listing_world(monkeypatch)
    found = iter([
        SimpleNamespace(number="A", court="", matter="", movements=[],
                        documents=[SimpleNamespace(described=lambda: "doc A")]),
        SimpleNamespace(number="B", court="", matter="", movements=[],
                        documents=[SimpleNamespace(described=lambda: "doc B")]),
    ])
    imported: list[str] = []

    async def process(_conn, _staff_id, _number):
        return next(found)

    async def import_document(_conn, _staff_id, *, number, document):
        imported.append(number)
        return SimpleNamespace(document_id=9, chunk_count=1)

    monkeypatch.setattr(legal_message.pje_service, "process", process)
    monkeypatch.setattr(legal_message.pje_service, "import_document", import_document)

    try:
        asyncio.run(legal_message._handle_autos("c", 100, paulo, ["1" * 20]))
        asyncio.run(legal_message._handle_autos("c", "100", paulo, ["2" * 20]))
        asyncio.run(legal_message._handle_autos("c", 100, paulo, ["trazer", "1"]))
    finally:
        legal_message._LAST_PROCESS_LISTING.clear()

    assert imported == ["B"]


def test_the_rate_limit_counts_one_person_whichever_way_the_chat_id_arrived(monkeypatch):
    keys: list[object] = []

    class _Limiter:
        def check(self, key):
            keys.append(key)
            return False, False

    monkeypatch.setattr(legal_message, "_limiter", _Limiter())
    from core.models import StaffMember

    paulo = StaffMember(id=1, chat_id="100", name="Paulo", role="owner")
    for chat in (100, "100"):
        asyncio.run(legal_message._serve_message("c", {}, {}, chat, paulo, "/id", None, None,
                                                 ""))

    assert keys == ["100", "100"]
