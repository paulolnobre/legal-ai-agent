"""The worker answering the panel: from the queued update to the rows.

Each test goes through the real intake (`panel_intake`) and the real handler
(`process_legal_message`) on a real Postgres. Telegram is spied on: a reply
to the person being served must never reach it, while a notice to somebody
else (the requester told they were let in) still does (revision 14).
"""
import asyncio
import contextlib
from types import SimpleNamespace

import pytest

from core import legal_agent
from core.handlers import legal_message
from core.models import AccessRequest, StaffMember
from core.repositories import callback_actions, conversation, panel, pending_actions
from core.services import channels, panel_intake, work_hours

PAULO = StaffMember(id=1, chat_id="100", name="Paulo", role="owner")
RUI = StaffMember(id=4, chat_id="400", name="Rui", role="advogado")

_DOCUMENTS = """
INSERT INTO documents (id, title, doc_type, full_text, uploaded_by, visibility,
                       content_sha256, source_filename, source_storage_key, source_sha256,
                       source_size_bytes, source_wrapped_key) VALUES
    (201, 'Contrato Acme', 'contrato', '', 1, 'firm', 'c201', 'contrato.pdf', 'k/201',
     's201', 10, '\\x01'::bytea);
"""


class _World(SimpleNamespace):
    telegram: list[tuple[str, str]]
    texts: list[tuple[str, str]]
    queued: list[dict]


@pytest.fixture
def world(db, monkeypatch):
    """Daytime, no rate limit, no model, Telegram spied, the queue in a list,
    and every fresh connection opened on this run's database."""
    telegram: list[tuple[str, str]] = []
    texts: list[tuple[str, str]] = []
    queued: list[dict] = []

    def spy(name):
        async def fake(*args, **_kwargs):
            telegram.append((name, str(args[0]) if args else ""))
            if name == "send_message" and len(args) > 1:
                texts.append((str(args[0]), str(args[1])))
            return 1
        return fake

    for name in ("send_message", "send_document", "send_typing", "edit_message_text",
                 "answer_callback_query"):
        monkeypatch.setattr(channels, name, spy(name))

    async def enqueue(update, *, deferred):
        queued.append(update)
        return True

    async def allow(*_args, **_kwargs):
        return True, False

    class _Pool:
        def connection(self):
            @contextlib.asynccontextmanager
            async def opened():
                conn = await db.connect()
                try:
                    yield conn
                    await conn.commit()
                finally:
                    await conn.close()
            return opened()

    async def get_pool():
        return _Pool()

    monkeypatch.setattr(panel_intake.update_queue, "enqueue_update", enqueue)
    monkeypatch.setattr(work_hours, "is_office_hours", lambda: True)
    monkeypatch.setattr(legal_message, "_limiter", SimpleNamespace(check=lambda _c: (True, False)))
    monkeypatch.setattr(legal_message.cache, "rate_limit_hit", allow)
    monkeypatch.setattr(legal_message.settings, "conversational_actions", False)
    monkeypatch.setattr(legal_message, "get_pool", get_pool)
    monkeypatch.setattr(legal_agent, "get_pool", get_pool)
    return _World(telegram=telegram, texts=texts, queued=queued)


async def _rows(conn, statement, params=()):
    async with conn.cursor() as cur:
        await cur.execute(statement, params)
        return await cur.fetchall()


async def _say(conn, world, who: StaffMember, text: str) -> dict:
    intake = await panel_intake.submit_message(conn, who, text)
    assert intake.outcome == "queued", intake
    return world.queued[-1]


async def _status(conn, update) -> str:
    rows = await _rows(conn, "SELECT status FROM panel_requests WHERE id = %s",
                       (update["panel_request"],))
    return rows[0][0]


async def _panel_bot_turns(conn, who: StaffMember):
    return await _rows(conn, "SELECT id, text, extras FROM conversation_turns "
                             "WHERE staff_id = %s AND speaker = 'bot' AND channel = 'panel' "
                             "ORDER BY id", (who.id,))


def _telegram_update(who: StaffMember, text: str, update_id: int = 7) -> dict:
    chat = int(who.chat_id or 0)
    return {"update_id": update_id, "message": {"chat": {"id": chat, "type": "private"},
                                                "from": {"id": chat}, "text": text}}


def test_a_panel_question_is_answered_as_a_panel_turn_and_telegram_is_not_used_for_the_person(
        db, world, monkeypatch):
    class _Reply:
        sources: list = []

        def __str__(self):
            return "O prazo é de 15 dias úteis. " + "Fundamento. " * 500

    async def answer_question(_conn, _text, **_kwargs):
        return _Reply()

    monkeypatch.setattr(legal_message, "answer_question", answer_question)

    async def body(conn):
        update = await _say(conn, world, RUI, "Qual o prazo para apelação no CPC?")
        await legal_message.process_legal_message(conn, update)
        commands = await _say(conn, world, RUI, "/comandos")
        await legal_message.process_legal_message(conn, commands)
        turns = await _panel_bot_turns(conn, RUI)
        person = await _rows(conn, "SELECT count(*) FROM conversation_turns "
                                   "WHERE speaker = 'person'")
        buttons = await _rows(conn, "SELECT DISTINCT channel FROM callback_actions")
        return turns, person, await _status(conn, update), buttons

    turns, person, status, buttons = db.run(body)

    assert turns[0][1].startswith("O prazo é de 15 dias úteis.")
    assert len(turns[0][1]) > 4000, "one turn, not split"
    assert "buttons" in (turns[0][2] or {}), "the rating buttons ride along"
    assert buttons == [("panel",)], "and they are panel buttons"
    assert person == [(2,)], "the person's turns were written once, by the intake"
    assert status == "done"
    assert all(chat != "400" for _name, chat in world.telegram), world.telegram


def test_an_access_request_decided_in_the_panel_still_tells_the_requester_by_telegram(
        db, world, monkeypatch):
    request = AccessRequest(id=3, request_code="NS-A7B2988C", chat_id="987", name="Helena")

    async def decide(_conn, code, *, approved, decided_by):
        return request

    monkeypatch.setattr(legal_message, "decide_access_request", decide)

    async def body(conn):
        update = await _say(conn, world, PAULO, "/autorizar NS-A7B2988C")
        await legal_message.process_legal_message(conn, update)
        return [t[1] for t in await _panel_bot_turns(conn, PAULO)]

    turns = db.run(body)

    assert any("Helena" in t for t in turns)
    assert ("send_message", "987") in world.telegram
    assert all(chat != "100" for _name, chat in world.telegram)


def test_a_redelivered_panel_update_does_not_run_twice(db, world):
    async def body(conn):
        update = await _say(conn, world, RUI, "/id")
        await legal_message.process_legal_message(conn, update)
        await legal_message.process_legal_message(conn, update)
        return [t[1] for t in await _panel_bot_turns(conn, RUI)]

    assert db.run(body) == ["Seu identificador: 400"]


def test_a_failing_panel_update_ends_failed_and_does_not_run_again(db, world, monkeypatch):
    calls: list[str] = []

    async def broken(*_args, **_kwargs):
        calls.append("ran")
        raise RuntimeError("the model went away")

    monkeypatch.setattr(legal_message, "_send_commands", broken)

    async def body(conn):
        update = await _say(conn, world, RUI, "/comandos")
        with pytest.raises(RuntimeError):
            await legal_message.process_legal_message(conn, update)
        await conn.rollback()
        await legal_message.process_legal_message(conn, update)
        return await _status(conn, update), await panel.in_flight(conn, RUI.id)

    status, in_flight = db.run(body)

    assert status == "failed" and in_flight is None
    assert calls == ["ran"], "the queue's retry finds it not runnable"


def test_a_panel_update_that_times_out_in_the_worker_ends_failed(db, world, monkeypatch):
    async def slow(*_args, **_kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(legal_message, "_send_commands", slow)
    monkeypatch.setattr(legal_agent, "_UPDATE_TIMEOUT", 0.2)

    async def body(conn):
        update = await _say(conn, world, RUI, "/comandos")
        with pytest.raises(TimeoutError):
            await legal_agent._handle_update(update)
        return await _status(conn, update)

    assert db.run(body) == "failed"


def test_a_deactivated_person_gets_the_refusal_and_the_request_ends_done(db, world):
    """Review Focus 3: never `running` forever, and nothing else runs."""
    async def body(conn):
        update = await _say(conn, world, RUI, "/id")
        async with conn.cursor() as cur:
            await cur.execute("UPDATE staff SET active = FALSE WHERE id = %s", (RUI.id,))
        await conn.commit()
        await legal_message.process_legal_message(conn, update)
        return [t[1] for t in await _panel_bot_turns(conn, RUI)], await _status(conn, update)

    turns, status = db.run(body)

    assert turns == [legal_message._PANEL_NO_ACCESS]
    assert status == "done"
    assert world.telegram == []


def test_an_older_telegram_update_does_not_read_a_queued_panel_message(db, world):
    async def body(conn):
        await conversation.record_turn(conn, staff_id=RUI.id, chat_id="400",
                                       speaker="person", text="dito no Telegram")
        update = await _say(conn, world, RUI, "dito no painel, ainda na fila")
        before = [t.text for t in await conversation.recent_turns(conn, RUI.id)]
        await panel.start(conn, update["panel_request"])
        after = [t.text for t in await conversation.recent_turns(conn, RUI.id)]
        return before, after

    before, after = db.run(body)

    assert before == ["dito no Telegram"]
    assert after == ["dito no Telegram", "dito no painel, ainda na fila"]


def test_withdrawn_and_never_started_messages_are_not_context(db, world, monkeypatch):
    monkeypatch.setattr(work_hours, "is_office_hours", lambda: False)

    async def body(conn):
        withdrawn = await panel_intake.submit_message(conn, RUI, "retirada")
        assert withdrawn.request_id
        await panel_intake.withdraw(conn, RUI, withdrawn.request_id)
        failed = await panel.create_request(conn, staff_id=RUI.id, chat_id="400",
                                            kind="message", text="falhou antes de rodar")
        assert failed
        await panel.finish(conn, failed[0], "failed")
        done = await panel.create_request(conn, staff_id=RUI.id, chat_id="400",
                                          kind="message", text="respondida")
        assert done
        await panel.start(conn, done[0])
        await panel.finish(conn, done[0], "done")
        return [t.text for t in await conversation.recent_turns(conn, RUI.id)]

    assert db.run(body) == ["respondida"]


_CONFIRMATIONS = {
    "confirm_document_deletion": ("confirmar", "_apply_document_removal"),
    "confirm_deactivation": ("confirmar", "deactivate_staff"),
    "confirm_erase": ("APAGAR", "erase_person"),
}


@pytest.mark.parametrize("asked_on", ["telegram", "panel"])
@pytest.mark.parametrize("kind", sorted(_CONFIRMATIONS))
def test_destructive_confirmations_are_refused_from_the_other_channel(
        db, world, monkeypatch, kind, asked_on):
    word, destroyer = _CONFIRMATIONS[kind]

    async def must_not(*_args, **_kwargs):
        raise AssertionError(f"{kind} answered from the other channel must not run")

    monkeypatch.setattr(legal_message, destroyer, must_not)

    async def body(conn):
        asking = channels.TELEGRAM if asked_on == "telegram" else channels.PanelChannel(
            conn, staff_id=PAULO.id, chat_id="100", request_id=None)  # type: ignore[arg-type]
        with channels.serving(asking, "100"):
            await legal_message._remember_pending_identifier(conn, "100", kind, target_id=4)
        await conn.commit()
        if asked_on == "telegram":
            update = await _say(conn, world, PAULO, word)
            await legal_message.process_legal_message(conn, update)
            reply = (await _panel_bot_turns(conn, PAULO))[-1][1]
        else:
            await legal_message.process_legal_message(conn, _telegram_update(PAULO, word))
            reply = next(text for chat, text in reversed(world.texts) if chat == "100")
        return reply, await pending_actions.peek(conn, "100")

    reply, still = db.run(body)

    where = "Telegram" if asked_on == "telegram" else "painel"
    assert reply == f"Essa confirmação foi pedida no {where}: confirme lá ou envie /cancelar."
    assert still is not None and still.action == kind and still.channel == asked_on


def test_a_confirmation_answered_in_its_own_window_still_runs(db, world, monkeypatch):
    removed: list[int] = []

    async def remove(_conn, _chat_id, _staff, document_id):
        removed.append(document_id)

    monkeypatch.setattr(legal_message, "_apply_document_removal", remove)

    async def body(conn):
        channel = channels.PanelChannel(conn, staff_id=PAULO.id, chat_id="100",
                                        request_id=None)  # type: ignore[arg-type]
        with channels.serving(channel, "100"):
            await legal_message._remember_pending_identifier(
                conn, "100", "confirm_document_deletion", target_id=201)
        await conn.commit()
        update = await _say(conn, world, PAULO, "confirmar")
        await legal_message.process_legal_message(conn, update)

    db.run(body)

    assert removed == [201]


def test_an_ordinary_follow_up_crosses_channels(db, world):
    """Asked on Telegram for an id, answered in the panel: one conversation."""
    async def body(conn):
        async with conn.cursor() as cur:
            await cur.execute(_DOCUMENTS)
        await conn.commit()
        with channels.serving(channels.TELEGRAM, "100"):
            await legal_message._remember_pending_identifier(conn, "100", "original")
        await conn.commit()
        update = await _say(conn, world, PAULO, "201")
        await legal_message.process_legal_message(conn, update)
        return await _panel_bot_turns(conn, PAULO)

    turns = db.run(body)

    assert turns[-1][2] == {"file": {"href": "/web/arquivos/201/original",
                                     "name": "contrato.pdf"}}
    assert world.telegram == []


def test_sign_in_commands_built_inside_the_handler_are_refused_on_the_panel(db, world):
    """The intake refuses them typed; a step resumed into one is refused too."""
    async def body(conn):
        # Every write of the panel's channel is for a request that exists.
        created = await panel.create_request(conn, staff_id=RUI.id, chat_id="400",
                                             kind="message", text="")
        assert created is not None
        channel = channels.PanelChannel(conn, staff_id=RUI.id, chat_id="400",
                                        request_id=created[0])
        with channels.serving(channel, "400"):
            async with legal_message._serving(conn, RUI):
                await legal_message._serve_message(
                    conn, {}, {}, "400", RUI, "/mfa refazer ABCD-EFGH", None, None, "",
                    from_conversation=True)
        return [t[1] for t in await _panel_bot_turns(conn, RUI)]

    assert db.run(body) == ["Isso é feito pelo Telegram: envie /mfa ao bot."]
    assert world.telegram == []


async def _offer_access_buttons(conn, request_code: str) -> tuple[int, str, str]:
    """An access-request notice in Paulo's panel, with its two panel buttons."""
    tokens = []
    for operation in ("approve", "reject"):
        tokens.append(await callback_actions.issue(
            conn, chat_id="100", staff_id=PAULO.id, issued_role="owner",
            action="access.review", operation=operation, resource=request_code,
            ttl_seconds=600, channel="panel"))
    turn_id = await conversation.record_turn(
        conn, staff_id=PAULO.id, chat_id="100", speaker="bot",
        text="🔔 Nova solicitação de acesso: Helena", channel="panel",
        extras=panel.buttons_extras([[("Autorizar", tokens[0]), ("Recusar", tokens[1])]]))
    await callback_actions.attach_reference(conn, tokens, reference=str(turn_id))
    await conn.commit()
    return turn_id, tokens[0], tokens[1]


def test_a_press_from_the_panel_decides_once_and_edits_the_turn(db, world, monkeypatch):
    request = AccessRequest(id=3, request_code="NS-A7B2988C", chat_id="987", name="Helena")
    decided: list[bool] = []

    async def decide(_conn, code, *, approved, decided_by):
        decided.append(approved)
        return request

    monkeypatch.setattr(legal_message, "decide_access_request", decide)

    async def body(conn):
        turn_id, approve, reject = await _offer_access_buttons(conn, request.request_code)
        first = await panel_intake.submit_press(conn, PAULO, token=approve, turn_id=turn_id)
        assert first.outcome == "queued"
        await legal_message.process_legal_message(conn, world.queued[-1])
        again = await panel_intake.submit_press(conn, PAULO, token=reject, turn_id=turn_id)
        edited = await _rows(conn, "SELECT text, extras FROM conversation_turns WHERE id = %s",
                             (turn_id,))
        return again, edited, [t[1] for t in await _panel_bot_turns(conn, PAULO)]

    again, edited, turns = db.run(body)

    assert decided == [True]
    assert again.outcome == "refused", "the turn no longer carries buttons"
    (text, extras), = edited
    assert text == "🔔 Nova solicitação de acesso: Helena\n\nAutorizado."
    assert "buttons" not in (extras or {})
    assert "Autorizado." in turns, "the toast became a short turn"
    assert ("send_message", "987") in world.telegram
    assert all(chat != "100" for _name, chat in world.telegram)


def test_a_telegram_button_cannot_be_pressed_through_the_panel(db, world, monkeypatch):
    """Even placed on a panel turn, a token issued for Telegram is refused by
    the claim's channel check and stays unspent."""
    async def decide(*_args, **_kwargs):
        raise AssertionError("must not decide")

    monkeypatch.setattr(legal_message, "decide_access_request", decide)

    async def body(conn):
        token = await callback_actions.issue(
            conn, chat_id="100", staff_id=PAULO.id, issued_role="owner",
            action="access.review", operation="approve", resource="NS-A7B2988C",
            ttl_seconds=600, channel="telegram")
        turn_id = await conversation.record_turn(
            conn, staff_id=PAULO.id, chat_id="100", speaker="bot", text="?",
            channel="panel", extras=panel.buttons_extras([[("Autorizar", token)]]))
        await conn.commit()
        await panel_intake.submit_press(conn, PAULO, token=token, turn_id=turn_id)
        await legal_message.process_legal_message(conn, world.queued[-1])
        return await _rows(conn, "SELECT state FROM callback_actions WHERE token = %s",
                           (token,))

    assert db.run(body) == [("pending",)]


def test_a_deletion_previewed_on_telegram_and_confirmed_in_the_panel_leaves_the_telegram_copy_untouched(  # noqa: E501
        db, world, monkeypatch):
    """Revision 5: the Telegram buttons die, and the Telegram message is not
    rewritten, since the rewrite would carry the decision to Telegram."""
    async def remove(_conn, _chat_id, _staff, _document_id):
        return None

    monkeypatch.setattr(legal_message, "_apply_document_removal", remove)

    async def body(conn):
        async with conn.cursor() as cur:
            await cur.execute(_DOCUMENTS)
        await conn.commit()
        await legal_message.process_legal_message(
            conn, _telegram_update(PAULO, "/excluir_documento 201"))
        world.telegram.clear()
        update = await _say(conn, world, PAULO, "/excluir_documento 201 confirmar")
        await legal_message.process_legal_message(conn, update)
        return await _rows(conn, "SELECT channel, state FROM callback_actions "
                                 "WHERE resource = %s",
                           (legal_message._document_resource(201),))

    states = db.run(body)

    assert states and all(state != "pending" for _channel, state in states), states
    assert not any(name == "edit_message_text" for name, _chat in world.telegram)
    assert world.telegram == []


def test_an_answer_delivered_on_telegram_is_exported_from_the_panel(db, world, monkeypatch):
    """Telegram's chat id arrives as a number, the panel's as text: the last
    answer is found either way."""
    from core import document_crypto

    monkeypatch.setattr(legal_message.settings, "doc_master_key",
                        document_crypto.new_master_key())

    async def build(fmt, title, text, listing):
        return b"%PDF-1.7", "resposta.pdf"

    monkeypatch.setattr(legal_message, "build_async", build)
    legal_message._remember_delivery(100, "Resposta", "texto")

    async def body(conn):
        update = await _say(conn, world, PAULO, "/exportar pdf")
        await legal_message.process_legal_message(conn, update)
        return await _rows(conn, "SELECT filename FROM panel_files")

    try:
        files = db.run(body)
    finally:
        legal_message._last_deliveries.clear()

    assert files == [("resposta.pdf",)]


def test_a_request_cut_off_while_being_marked_done_ends_failed(db, world, monkeypatch):
    """The last step can fail too: cut off by the time limit while holding
    the row's lock, the request still ends failed rather than running."""
    finish = panel.finish

    async def hanging(conn, request_id, status):
        if status != "done":
            return await finish(conn, request_id, status)
        async with conn.cursor() as cur:
            await cur.execute("UPDATE panel_requests SET updated_at = NOW() WHERE id = %s",
                              (request_id,))
        await asyncio.sleep(30)

    monkeypatch.setattr(legal_message.panel, "finish", hanging)
    monkeypatch.setattr(legal_agent, "_UPDATE_TIMEOUT", 0.5)

    async def body(conn):
        update = await _say(conn, world, RUI, "/id")
        with pytest.raises(TimeoutError):
            await legal_agent._handle_update(update)
        return await _status(conn, update)

    assert db.run(body) == "failed"


def test_a_request_whose_done_mark_fails_ends_failed(db, world, monkeypatch):
    finish = panel.finish

    async def broken(conn, request_id, status):
        if status == "done":
            raise RuntimeError("database went away")
        return await finish(conn, request_id, status)

    monkeypatch.setattr(legal_message.panel, "finish", broken)

    async def body(conn):
        update = await _say(conn, world, RUI, "/id")
        with pytest.raises(RuntimeError):
            await legal_message.process_legal_message(conn, update)
        return await _status(conn, update)

    assert db.run(body) == "failed"


# ── /pje in the panel ───────────────────────────────────────────────────────


@pytest.fixture
def pje_on(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setattr(legal_message.settings, "pje_enabled", True)
    monkeypatch.setattr(legal_message.settings, "pje_cred_key", Fernet.generate_key().decode())
    monkeypatch.setattr(legal_message.settings, "public_base_url", "https://agente.exemplo.br")
    monkeypatch.setattr(legal_message.settings, "web_base_url", None)


async def _everything_said(conn) -> list[str]:
    """Every row a panel request leaves behind, as text."""
    rows = await _rows(conn, "SELECT row_to_json(t)::text FROM conversation_turns t "
                             "UNION ALL SELECT row_to_json(r)::text FROM panel_requests r")
    return [row[0] for row in rows]


async def _enrolled(conn, who: StaffMember) -> None:
    from core.repositories import pje_credentials

    await pje_credentials.save_credential(conn, who.id, cpf="12345678901",
                                          password="senha-do-pje", court="TJPA")


@pytest.mark.parametrize("typed, enrolled", [
    ("/pje", False), ("/PJE", False), ("/pje cadastrar", False),
    ("/pje cadastrar", True), ("/pje trocar", True),
])
def test_pje_in_the_panel_answers_with_the_page_and_never_a_token(
        db, world, pje_on, typed, enrolled):
    async def body(conn):
        if enrolled:
            await _enrolled(conn, RUI)
        update = await _say(conn, world, RUI, typed)
        await legal_message.process_legal_message(conn, update)
        enrolments = await _rows(conn, "SELECT count(*) FROM pje_enrolments")
        return (await _panel_bot_turns(conn, RUI), enrolments[0][0],
                await _everything_said(conn), await _status(conn, update))

    turns, enrolments, said, status = db.run(body)

    assert [t[1] for t in turns] == [
        "Para cadastrar o seu acesso ao TJPA, abra a página de cadastro no painel:\n\n"
        "https://agente.exemplo.br/web/pje/\n\nDigite a senha só na página, nunca aqui "
        "no chat."]
    assert enrolments == 0, "the page opens the enrolment, not the panel's turn"
    assert not any("/pje/" in row.replace("/web/pje/", "") for row in said)
    assert status == "done"
    assert world.telegram == []


def test_a_built_pje_command_in_the_panel_never_opens_an_enrolment(db, world, pje_on):
    """Past the intake (a resumed step, a sentence read as the command), the
    handler still answers with the page: the token decision is its own."""
    async def body(conn):
        created = await panel.create_request(conn, staff_id=RUI.id, chat_id="400",
                                             kind="message", text="")
        assert created is not None
        channel = channels.PanelChannel(conn, staff_id=RUI.id, chat_id="400",
                                        request_id=created[0])
        with channels.serving(channel, "400"):
            async with legal_message._serving(conn, RUI):
                await legal_message._serve_message(
                    conn, {}, {}, "400", RUI, "/pje cadastrar", None, None, "",
                    from_conversation=True)
        enrolments = await _rows(conn, "SELECT count(*) FROM pje_enrolments")
        return [t[1] for t in await _panel_bot_turns(conn, RUI)], enrolments[0][0]

    turns, enrolments = db.run(body)

    assert enrolments == 0
    assert len(turns) == 1 and "https://agente.exemplo.br/web/pje/\n" in turns[0]
    assert world.telegram == []


def test_pje_on_telegram_still_sends_its_own_link(db, world, pje_on):
    """Only the bot's chat carries a token, and it is never recorded."""
    async def body(conn):
        await legal_message.process_legal_message(conn, _telegram_update(RUI, "/pje"))
        enrolments = await _rows(conn, "SELECT count(*) FROM pje_enrolments")
        return enrolments[0][0], await _everything_said(conn)

    enrolments, said = db.run(body)

    assert enrolments == 1
    assert len(world.texts) == 1 and "https://agente.exemplo.br/pje/" in world.texts[0][1]
    assert not any("agente.exemplo.br/pje/" in row for row in said)


def test_pje_remover_in_the_panel_forgets_the_credential(db, world, pje_on):
    async def body(conn):
        await _enrolled(conn, RUI)
        update = await _say(conn, world, RUI, "/pje remover")
        await legal_message.process_legal_message(conn, update)
        left = await _rows(conn, "SELECT count(*) FROM pje_credentials WHERE staff_id = %s",
                           (RUI.id,))
        return [t[1] for t in await _panel_bot_turns(conn, RUI)], left[0][0]

    turns, left = db.run(body)

    assert left == 0
    assert turns[-1].startswith("Acesso ao PJe removido daqui.")
    assert world.telegram == []


def test_pje_status_in_the_panel_masks_the_cpf(db, world, pje_on):
    async def body(conn):
        await _enrolled(conn, RUI)
        update = await _say(conn, world, RUI, "/pje")
        await legal_message.process_legal_message(conn, update)
        return [t[1] for t in await _panel_bot_turns(conn, RUI)], await _everything_said(conn)

    turns, said = db.run(body)

    # `plain_text` (both channels) reads the mask's "**" as emphasis; the
    # point here is that the middle digits never reach the turn.
    assert turns[-1].startswith("⚖️ Acesso ao TJPA cadastrado (CPF 123.")
    assert "-01).\nCadastrado em:" in turns[-1]
    assert not any("12345678901" in row or "senha-do-pje" in row for row in said)


def test_an_intern_without_pje_connect_is_refused_in_the_panel(db, world, pje_on, monkeypatch):
    from core.repositories.staff import ROLE_LAWYER
    from core.services import authorization

    monkeypatch.setitem(authorization._REQUIRED_RANK, "pje.connect", ROLE_LAWYER)
    ana = StaffMember(id=5, chat_id="500", name="Ana", role="estagiario")

    async def body(conn):
        update = await _say(conn, world, ana, "/pje")
        await legal_message.process_legal_message(conn, update)
        return [t[1] for t in await _panel_bot_turns(conn, ana)]

    turns = db.run(body)

    assert len(turns) == 1 and turns[0].startswith("🔒 Este recurso é para advogados.")
