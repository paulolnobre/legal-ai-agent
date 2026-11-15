"""The panel's channel: what the handler says becomes rows, never Telegram.

Real Postgres throughout, because the row is the delivery. Telegram is
spied on in every test, and none of these may reach it.
"""
import pytest

from core import document_crypto
from core.config import settings
from core.handlers import legal_message
from core.models import StaffMember
from core.repositories import panel
from core.services import channels

RUI = StaffMember(id=4, chat_id="400", name="Rui", role="advogado")
ANA = StaffMember(id=5, chat_id="500", name="Ana", role="estagiario")

_DOCUMENTS = """
INSERT INTO documents (id, title, doc_type, full_text, uploaded_by, visibility,
                       content_sha256, source_filename, source_storage_key, source_sha256,
                       source_size_bytes, source_wrapped_key) VALUES
    (201, 'Contrato Acme', 'contrato', '', 4, 'firm', 'c201', 'contrato.pdf', 'k/201',
     's201', 10, '\\x01'::bytea),
    (202, 'Parecer da Ana', 'parecer', '', 5, 'private', 'c202', 'parecer.pdf', 'k/202',
     's202', 10, '\\x01'::bytea);
"""


@pytest.fixture
def telegram(monkeypatch):
    calls: list[str] = []

    def spy(name):
        async def fake(*_args, **_kwargs):
            calls.append(name)
            return 1
        return fake

    for name in ("send_message", "send_document", "send_typing", "edit_message_text",
                 "answer_callback_query"):
        monkeypatch.setattr(channels, name, spy(name))
    return calls


@pytest.fixture
def master_key(monkeypatch):
    monkeypatch.setattr(settings, "doc_master_key", document_crypto.new_master_key())


async def _rows(conn, statement, params=()):
    async with conn.cursor() as cur:
        await cur.execute(statement, params)
        return await cur.fetchall()


async def _request(conn, who: StaffMember):
    created = await panel.create_request(conn, staff_id=who.id, chat_id=who.chat_id,
                                         kind="message", text="pergunta")
    assert created
    return created[0]


def _channel(conn, who: StaffMember, request_id) -> channels.PanelChannel:
    return channels.PanelChannel(conn, staff_id=who.id, chat_id=who.chat_id,
                                 request_id=request_id)


async def _bot_turns(conn):
    return await _rows(conn, "SELECT id, text, extras, request_id, channel FROM "
                             "conversation_turns WHERE speaker = 'bot' ORDER BY id")


def test_a_long_answer_is_one_panel_turn(db, telegram):
    """Review Focus 1: no 4000-character split on the panel."""
    async def body(conn):
        request_id = await _request(conn, RUI)
        with channels.serving(_channel(conn, RUI, request_id), RUI.chat_id):
            await legal_message._send(RUI.chat_id, "x" * 9000)
        return request_id, await _bot_turns(conn)

    request_id, turns = db.run(body)

    assert [(len(t[1]), t[3], t[4]) for t in turns] == [(9000, request_id, "panel")]
    assert telegram == []


def test_buttons_are_stored_with_the_turn(db, telegram):
    keyboard = {"inline_keyboard": [[{"text": "Apagar", "callback_data": "t9"},
                                     {"text": "Cancelar", "callback_data": "t10"}]]}

    async def body(conn):
        request_id = await _request(conn, RUI)
        with channels.serving(_channel(conn, RUI, request_id), RUI.chat_id):
            reference = await legal_message._send(RUI.chat_id, "Apagar?",
                                                  reply_markup=keyboard)
        return reference, await _bot_turns(conn)

    reference, turns = db.run(body)

    turn_id, text, extras, _request_id, _channel_name = turns[0]
    assert reference == str(turn_id) and text == "Apagar?"
    assert extras == {"buttons": [[{"label": "Apagar", "token": "t9"},
                                   {"label": "Cancelar", "token": "t10"}]]}
    assert telegram == []


def test_bot_turns_and_edits_are_redacted_of_access_links(db, telegram):
    link = "https://agente.demo-firm.example/web/login/Zx9kq"

    async def body(conn):
        request_id = await _request(conn, RUI)
        channel = _channel(conn, RUI, request_id)
        sent = await channel.send_text(RUI.chat_id, f"Entre por {link}", None, preview=False)
        assert sent
        first = (await _bot_turns(conn))[0][1]
        await channel.edit_text(RUI.chat_id, sent, f"Use {link} de novo")
        return first, (await _bot_turns(conn))[0][1]

    first, edited = db.run(body)

    assert first == "Entre por [link de acesso]"
    assert edited == "Use [link de acesso] de novo"


def test_an_original_asked_from_the_panel_is_linked_and_never_read_or_logged(
        db, telegram, monkeypatch):
    async def never(*_args, **_kwargs):
        raise AssertionError("the original must not be read for the panel")

    monkeypatch.setattr(legal_message.document_storage, "read", never)

    async def body(conn):
        async with conn.cursor() as cur:
            await cur.execute(_DOCUMENTS)
        await conn.commit()
        request_id = await _request(conn, RUI)
        async with legal_message._serving(conn, RUI):
            with channels.serving(_channel(conn, RUI, request_id), RUI.chat_id):
                await legal_message._send_original_document(conn, RUI.chat_id, 201, RUI.id)
                await legal_message._send_original_document(conn, RUI.chat_id, 202, RUI.id)
        logged = await _rows(conn, "SELECT action FROM document_access_log")
        files = await _rows(conn, "SELECT token FROM panel_files")
        return await _bot_turns(conn), logged, files

    turns, logged, files = db.run(body)

    linked, refused = turns
    assert linked[1] == "Arquivo original do documento [201]"
    assert linked[2] == {"file": {"href": "/web/arquivos/201/original",
                                  "name": "contrato.pdf"}}
    assert "não está disponível" in refused[1] and refused[2] is None, \
        "someone else's private document is not even linked"
    assert logged == [] and files == []
    assert telegram == []


def test_an_export_is_sealed_and_linked(db, telegram, master_key, monkeypatch):
    legal_message._last_deliveries["400"] = ("Resposta", "texto da resposta", 1e18)

    async def build(fmt, title, text, listing):
        return b"%PDF-1.7 resposta", "resposta.pdf"

    monkeypatch.setattr(legal_message, "build_async", build)

    async def body(conn):
        request_id = await _request(conn, RUI)
        with channels.serving(_channel(conn, RUI, request_id), RUI.chat_id):
            await legal_message._export_last_answer(RUI.chat_id, "pdf")
        turns = await _bot_turns(conn)
        token = turns[0][2]["file"]["href"].rsplit("/", 1)[1]
        return turns, await panel.read_file(conn, token, staff_id=RUI.id)

    try:
        turns, opened = db.run(body)
    finally:
        legal_message._last_deliveries.pop("400", None)

    (_id, text, extras, _request_id, _channel_name), = turns
    assert text == "Resposta (PDF)\nDisponível por 24 h."
    assert extras["file"]["name"] == "resposta.pdf" and extras["file"]["hours"] == 24
    assert extras["file"]["href"].startswith("/web/assistente/arquivo/")
    assert opened == ("resposta.pdf", "application/pdf", b"%PDF-1.7 resposta")
    assert telegram == []


def test_an_oversized_export_says_so(db, telegram, master_key, monkeypatch):
    monkeypatch.setattr(settings, "panel_file_max_bytes", 4)

    async def body(conn):
        request_id = await _request(conn, RUI)
        channel = _channel(conn, RUI, request_id)
        sent = await channel.send_file(RUI.chat_id, "grande.pdf", b"12345", "Grande",
                                       kind="export", document_id=None)
        return sent, await _bot_turns(conn), await _rows(conn, "SELECT 1 FROM panel_files")

    sent, turns, files = db.run(body)

    assert sent and [t[1] for t in turns] == ["Esse arquivo é grande demais para o painel."]
    assert files == []


def test_editing_a_turn_clears_its_buttons_and_marks_it_updated(db, telegram):
    async def body(conn):
        request_id = await _request(conn, RUI)
        channel = _channel(conn, RUI, request_id)
        buttons = [[channels.Button("Apagar", "t9")]]
        mine = await channel.send_text(RUI.chat_id, "Apagar?", buttons, preview=False)
        assert mine
        anas = await _channel(conn, ANA, request_id).send_text(
            ANA.chat_id, "Apagar?", buttons, preview=False)
        assert anas
        before = await _rows(conn, "SELECT updated_at FROM conversation_turns WHERE id = %s",
                             (int(mine),))
        await channel.edit_text(RUI.chat_id, mine, "Apagar?\n\nApagado.")
        await channel.edit_text(RUI.chat_id, anas, "tomado")
        await channel.edit_text(RUI.chat_id, "not-a-turn", "nada")
        rows = await _rows(conn, "SELECT id, text, extras, updated_at FROM conversation_turns "
                                 "WHERE speaker = 'bot' ORDER BY id")
        return int(mine), before[0][0], rows

    mine, before, rows = db.run(body)

    edited, untouched = rows
    assert edited[0] == mine and edited[1] == "Apagar?\n\nApagado."
    assert edited[2] == {} and edited[3] > before
    assert untouched[1] == "Apagar?" and "buttons" in untouched[2], \
        "another person's turn is never edited"


def test_a_telegram_turn_is_never_edited_as_a_panel_turn(db, telegram):
    async def body(conn):
        from core.repositories import conversation

        turn_id = await conversation.record_turn(
            conn, staff_id=RUI.id, chat_id=RUI.chat_id, speaker="bot", text="Apagar?",
            channel="telegram", extras={"buttons": [[{"label": "a", "token": "t"}]]})
        request_id = await _request(conn, RUI)
        await _channel(conn, RUI, request_id).edit_text(RUI.chat_id, str(turn_id), "x")
        return await _rows(conn, "SELECT text FROM conversation_turns WHERE id = %s",
                           (turn_id,))

    assert db.run(body) == [("Apagar?",)]


def test_a_button_acknowledgement_is_a_short_turn(db, telegram):
    async def body(conn):
        request_id = await _request(conn, RUI)
        channel = _channel(conn, RUI, request_id)
        await channel.ack_button(f"panel:{request_id}", "Autorizado.")
        await channel.ack_button(f"panel:{request_id}", None)
        await channel.ack_button(f"panel:{request_id}", "")
        await channel.typing(RUI.chat_id)
        return [t[1] for t in await _bot_turns(conn)]

    assert db.run(body) == ["Autorizado."]


def test_a_recording_failure_in_the_panel_raises(db, telegram):
    """Revision 8: on the panel the row is the reply, so losing it is a
    failure of the request, not a log line."""
    async def body(conn):
        request_id = await _request(conn, RUI)
        async with conn.cursor() as cur:
            await cur.execute("ALTER TABLE conversation_turns RENAME TO turns_gone")
        await conn.commit()
        with channels.serving(_channel(conn, RUI, request_id), RUI.chat_id):
            with pytest.raises(Exception):
                await legal_message._send(RUI.chat_id, "resposta")
        return True

    assert db.run(body)
    assert telegram == []


def test_without_the_documents_key_an_export_is_not_sent(db, telegram, monkeypatch):
    monkeypatch.setattr(settings, "doc_master_key", None)

    async def body(conn):
        request_id = await _request(conn, RUI)
        return await _channel(conn, RUI, request_id).send_file(
            RUI.chat_id, "a.pdf", b"x", "A", kind="export", document_id=None)

    assert db.run(body) is None


def test_the_buttons_extras_shape_is_what_the_press_check_reads(db):
    async def body(conn):
        from core.repositories import conversation

        turn_id = await conversation.record_turn(
            conn, staff_id=RUI.id, chat_id=RUI.chat_id, speaker="bot", text="?",
            channel="panel", extras=panel.buttons_extras([[("a", "t1")], [("b", "t2")]]))
        found = [await panel.button_turn_text(conn, staff_id=RUI.id, turn_id=turn_id,
                                              token=t)
                 for t in ("t1", "t2", "t3")]
        return found

    assert db.run(body) == ["?", "?", None]

