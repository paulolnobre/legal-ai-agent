"""A message or a press from the panel, from the page to the queue.

Real Postgres for what is stored (and what is not), a fake queue for what is
handed over. The hour is decided by `work_hours`, the webhook's own rule.
"""
import asyncio
import contextlib
from uuid import UUID

import pytest

from core.handlers import legal_message
from core.models import StaffMember
from core.repositories import panel
from core.schedule import OUT_OF_HOURS_MESSAGE
from core.services import panel_intake, work_hours

PAULO = StaffMember(id=1, chat_id="100", name="Paulo", role="owner")
RUI = StaffMember(id=4, chat_id="400", name="Rui", role="advogado")
ANA = StaffMember(id=5, chat_id="500", name="Ana", role="estagiario")
NO_CHAT = StaffMember(id=6, chat_id=None, name="Sem chat", role="advogado")


class _Queue:
    def __init__(self, *, fails: bool = False):
        self.items: list[tuple[dict, bool]] = []
        self.fails = fails

    async def enqueue(self, update, *, deferred):
        if self.fails:
            raise RuntimeError("Update queue unavailable")
        self.items.append((update, deferred))
        return True


@pytest.fixture
def queue(monkeypatch):
    fake = _Queue()
    monkeypatch.setattr(panel_intake.update_queue, "enqueue_update", fake.enqueue)
    return fake


@pytest.fixture
def daytime(monkeypatch):
    monkeypatch.setattr(work_hours, "is_office_hours", lambda: True)


@pytest.fixture
def night(monkeypatch, db):
    """Out of hours, with the owner's exemption read from this run's database."""
    monkeypatch.setattr(work_hours, "is_office_hours", lambda: False)

    class _Pool:
        def connection(self):
            @contextlib.asynccontextmanager
            async def opened():
                conn = await db.connect()
                try:
                    yield conn
                finally:
                    await conn.close()
            return opened()

    async def get_pool():
        return _Pool()

    monkeypatch.setattr(work_hours, "get_pool", get_pool)


async def _rows(conn, statement, params=()):
    async with conn.cursor() as cur:
        await cur.execute(statement, params)
        return await cur.fetchall()


async def _everything(conn):
    """Every panel request and every turn, to say what was (not) stored."""
    return (await _rows(conn, "SELECT staff_id, kind, status FROM panel_requests ORDER BY "
                              "created_at"),
            await _rows(conn, "SELECT staff_id, speaker, text, channel FROM conversation_turns "
                              "ORDER BY id"))


def test_a_message_is_stored_queued_and_handed_over(db, queue, daytime):
    async def body(conn):
        intake = await panel_intake.submit_message(conn, RUI, "Qual o prazo do recurso?")
        return intake, await _everything(conn)

    intake, (requests, turns) = db.run(body)

    assert intake.outcome == "queued" and isinstance(intake.request_id, UUID)
    assert requests == [(4, "message", "queued")]
    assert turns == [(4, "person", "Qual o prazo do recurso?", "panel")]
    update, deferred = queue.items[0]
    assert deferred is False
    assert update == {
        "update_id": f"panel:{intake.request_id}",
        "channel": "panel",
        "panel_request": str(intake.request_id),
        "message": {"chat": {"id": 400, "type": "private"}, "from": {"id": 400},
                    "text": "Qual o prazo do recurso?", "recorded": True},
    }


def test_out_of_hours_it_is_deferred_with_the_notice_recorded_for_the_panel(
        db, queue, night):
    async def body(conn):
        intake = await panel_intake.submit_message(conn, RUI, "Qual o prazo?")
        return intake, await _everything(conn)

    intake, (requests, turns) = db.run(body)

    assert intake.outcome == "deferred"
    assert queue.items[0][1] is True
    assert requests == [(4, "message", "deferred")]
    assert turns == [(4, "person", "Qual o prazo?", "panel"),
                     (4, "bot", OUT_OF_HOURS_MESSAGE, "panel")]


def test_the_owner_is_not_deferred(db, queue, night):
    """The webhook's rule, one implementation: the owner works at night."""
    async def body(conn):
        return await panel_intake.submit_message(conn, PAULO, "Situação do sistema")

    assert db.run(body).outcome == "queued"
    assert queue.items[0][1] is False


def test_a_panel_button_press_out_of_hours_is_not_deferred(db, queue, night):
    async def body(conn):
        turn_id = await _bot_turn_with_buttons(conn, RUI, ["tok-1", "tok-2"])
        intake = await panel_intake.submit_press(conn, RUI, token="tok-2", turn_id=turn_id)
        return intake, turn_id, await _everything(conn)

    intake, turn_id, (requests, turns) = db.run(body)

    assert intake.outcome == "queued"
    update, deferred = queue.items[0]
    assert deferred is False
    assert update == {
        "update_id": f"panel:{intake.request_id}",
        "channel": "panel",
        "panel_request": str(intake.request_id),
        "callback_query": {
            "id": f"panel:{intake.request_id}",
            "from": {"id": 400},
            "data": "tok-2",
            "message": {"message_id": turn_id, "chat": {"id": 400, "type": "private"},
                        "text": "Apagar?"},
        },
    }
    assert requests == [(4, "button", "queued")]
    assert [t[1] for t in turns] == ["bot"], "a press says nothing of its own"


def test_an_access_link_pasted_in_a_message_is_stored_redacted(db, queue, daytime):
    async def body(conn):
        await panel_intake.submit_message(
            conn, RUI, "abri https://agente.demo-firm.example/web/login/Zx9kq e deu erro")
        return await _rows(conn, "SELECT text FROM conversation_turns")

    assert db.run(body) == [("abri [link de acesso] e deu erro",)]


def test_simultaneous_sends_from_two_tabs_queue_one_and_refuse_the_other(
        db, queue, daytime):
    async def body(conn):
        other = await db.connect()
        try:
            results = await asyncio.gather(
                panel_intake.submit_message(conn, RUI, "primeira aba"),
                panel_intake.submit_message(other, RUI, "segunda aba"),
            )
        finally:
            await other.close()
        return results, await _everything(conn)

    results, (requests, turns) = db.run(body)

    assert sorted(r.outcome for r in results) == ["busy", "queued"]
    assert len(queue.items) == 1 and len(requests) == 1 and len(turns) == 1


@pytest.mark.parametrize("text", [
    "/web", "/acesso", "/mfa", "/autenticador",
    "/mfa refazer ABCD-EFGH", "  /MFA   refazer ABCD-EFGH", "/Web encerrar",
])
def test_sign_in_commands_are_refused_and_nothing_is_stored(db, queue, daytime, text):
    async def body(conn):
        intake = await panel_intake.submit_message(conn, RUI, text)
        dump = await _rows(conn, "SELECT row_to_json(t)::text FROM conversation_turns t "
                                 "UNION ALL SELECT row_to_json(r)::text FROM panel_requests r")
        return intake, dump

    intake, dump = db.run(body)

    assert intake.outcome == "refused"
    assert intake.message.startswith("Isso é feito pelo Telegram: envie /")
    assert dump == [] and queue.items == []


@pytest.mark.parametrize("text", ["/microsoft", "/ms", "/MS desconectar"])
def test_microsoft_points_to_the_integrations_page_and_nothing_is_stored(
        db, queue, daytime, text):
    async def body(conn):
        intake = await panel_intake.submit_message(conn, RUI, text)
        dump = await _rows(conn, "SELECT row_to_json(t)::text FROM conversation_turns t "
                                 "UNION ALL SELECT row_to_json(r)::text FROM panel_requests r")
        return intake, dump

    intake, dump = db.run(body)

    assert (intake.outcome, intake.reason) == ("refused", "microsoft_page")
    assert intake.message == "Conecte a Microsoft na aba Integrações."
    assert dump == [] and queue.items == []


def test_a_second_message_while_one_is_in_flight_is_refused_and_not_stored(
        db, queue, daytime):
    """Review Focus 4: the second tab is told to wait, and nothing of it stays."""
    async def body(conn):
        first = await panel_intake.submit_message(conn, RUI, "primeira")
        second = await panel_intake.submit_message(conn, RUI, "segunda")
        other_person = await panel_intake.submit_message(conn, ANA, "de outra pessoa")
        return first, second, other_person, await _everything(conn)

    first, second, other_person, (requests, turns) = db.run(body)

    assert first.outcome == "queued" and other_person.outcome == "queued"
    assert second.outcome == "busy" and second.message == "Aguarde a resposta anterior."
    assert [t[2] for t in turns] == ["primeira", "de outra pessoa"]
    assert len(requests) == 2


def test_a_queue_failure_marks_the_request_failed(db, monkeypatch, daytime):
    fake = _Queue(fails=True)
    monkeypatch.setattr(panel_intake.update_queue, "enqueue_update", fake.enqueue)

    async def body(conn):
        intake = await panel_intake.submit_message(conn, RUI, "oi")
        after = await panel.in_flight(conn, RUI.id)
        return intake, after, await _everything(conn)

    intake, after, (requests, _turns) = db.run(body)

    assert intake.outcome == "failed"
    assert requests == [(4, "message", "failed")]
    assert after is None, "the person may send again"


def test_a_staff_row_without_chat_cannot_use_the_panel_chat(db, queue, daytime):
    async def body(conn):
        return (await panel_intake.submit_message(conn, NO_CHAT, "oi"),
                await _everything(conn))

    intake, (requests, turns) = db.run(body)

    assert intake.outcome == "no_chat"
    assert requests == [] and turns == [] and queue.items == []


async def _bot_turn_with_buttons(conn, who: StaffMember, tokens: list[str]) -> int:
    from core.repositories import conversation

    return await conversation.record_turn(
        conn, staff_id=who.id, chat_id=who.chat_id, speaker="bot", text="Apagar?",
        channel="panel",
        extras=panel.buttons_extras([[(f"b{i}", t) for i, t in enumerate(tokens)]]),
    )


def test_a_press_on_someone_elses_turn_or_a_token_not_on_the_turn_is_refused(
        db, queue, daytime):
    async def body(conn):
        from core.repositories import conversation

        anas = await _bot_turn_with_buttons(conn, ANA, ["tok-ana"])
        ruis = await _bot_turn_with_buttons(conn, RUI, ["tok-rui"])
        telegram_turn = await conversation.record_turn(
            conn, staff_id=RUI.id, chat_id=RUI.chat_id, speaker="bot", text="Apagar?",
            channel="telegram", extras=panel.buttons_extras([[("b", "tok-tg")]]))
        outcomes = [
            await panel_intake.submit_press(conn, RUI, token="tok-ana", turn_id=anas),
            await panel_intake.submit_press(conn, RUI, token="tok-ana", turn_id=ruis),
            await panel_intake.submit_press(conn, RUI, token="tok-tg", turn_id=telegram_turn),
            await panel_intake.submit_press(conn, RUI, token="", turn_id=ruis),
        ]
        return outcomes, await _everything(conn)

    outcomes, (requests, _turns) = db.run(body)

    assert [o.outcome for o in outcomes] == ["refused"] * 4
    assert requests == [] and queue.items == []


def test_a_withdrawn_deferred_request_never_runs(db, queue, night):
    """Review Focus 5: withdrawn, it frees the person and the worker skips it."""
    async def body(conn):
        intake = await panel_intake.submit_message(conn, RUI, "para amanhã")
        assert intake.request_id
        by_other = await panel_intake.withdraw(conn, ANA, intake.request_id)
        withdrawn = await panel_intake.withdraw(conn, RUI, intake.request_id)
        started = await panel.start(conn, intake.request_id)
        again = await panel_intake.submit_message(conn, RUI, "outra coisa")
        return by_other, withdrawn, started, again

    by_other, withdrawn, started, again = db.run(body)

    assert (by_other, withdrawn, started) == (False, True, False)
    assert again.outcome == "deferred"


def test_an_empty_message_is_refused(db, queue, daytime):
    async def body(conn):
        return (await panel_intake.submit_message(conn, RUI, "  \n "),
                await _everything(conn))

    intake, (requests, turns) = db.run(body)

    assert intake.outcome == "refused" and requests == [] and turns == []


def test_a_message_over_the_bots_limit_is_refused(db, queue, daytime, monkeypatch):
    monkeypatch.setattr(panel_intake.settings, "max_question_chars", 10)

    async def body(conn):
        return (await panel_intake.submit_message(conn, RUI, "x" * 11),
                await _everything(conn))

    intake, (requests, turns) = db.run(body)

    assert intake.outcome == "refused" and requests == [] and turns == []


def test_the_refused_commands_cover_what_the_handler_keeps_out_of_the_record():
    """`/pje` is the one exception: the panel answers it with a page and no
    token, and takes nothing after it but its own words (below)."""
    assert legal_message._UNRECORDED - {"/pje"} <= panel_intake.REFUSED_COMMANDS
    assert {"/microsoft", "/ms"} <= panel_intake.REFUSED_COMMANDS
    assert "/pje" not in panel_intake.REFUSED_COMMANDS


@pytest.mark.parametrize("text", [
    "/pje 12345678901 minha-senha", "/pje minha-senha", "/PJE cadastrar minha-senha",
    "/pje remover agora", "/Pje\u00a0link",
])
def test_pje_with_anything_but_its_words_is_refused_and_nothing_is_stored(
        db, queue, daytime, text):
    async def body(conn):
        intake = await panel_intake.submit_message(conn, RUI, text)
        dump = await _rows(conn, "SELECT row_to_json(t)::text FROM conversation_turns t "
                                 "UNION ALL SELECT row_to_json(r)::text FROM panel_requests r")
        return intake, dump

    intake, dump = db.run(body)

    assert (intake.outcome, intake.reason) == ("refused", "pje_words")
    assert "minha-senha" not in intake.message
    assert dump == [] and queue.items == []


@pytest.mark.parametrize("text", ["/pje", "/PJE", "/pje cadastrar", "/pje Trocar",
                                  "/pje recadastrar", "/pje remover", "  /pje   sair "])
def test_pje_alone_or_with_its_words_is_taken(db, queue, daytime, text):
    async def body(conn):
        return await panel_intake.submit_message(conn, RUI, text)

    assert db.run(body).outcome == "queued"
    assert len(queue.items) == 1


def test_the_pje_words_are_the_ones_the_handler_reads():
    from core.services import pje

    assert pje.REMOVE_WORDS == {"remover", "remove", "esquecer", "sair"}
    assert pje.ENROL_WORDS == {"cadastrar", "trocar", "recadastrar"}


def test_the_command_word_is_read_as_the_handler_reads_it():
    for text in ("/MFA refazer X", "\t/web x", "/pje", ""):
        handler = text.casefold().split(maxsplit=1)
        assert panel_intake.command_word(text) == (handler[0] if handler else "")


def test_a_worker_starting_fails_the_requests_left_running(monkeypatch):
    from core import legal_agent

    calls: list[str] = []

    async def recover():
        calls.append("recover")
        return 0

    async def fail_abandoned():
        calls.append("panel")

    monkeypatch.setattr(legal_agent.update_queue, "recover_processing", recover)
    monkeypatch.setattr(legal_agent, "_fail_abandoned_panel_requests", fail_abandoned)
    stop = asyncio.Event()
    stop.set()

    asyncio.run(legal_agent._run_worker_session(stop))

    assert calls == ["recover", "panel"]


def test_a_request_that_never_reached_the_queue_does_not_block_the_next(
        db, queue, daytime, monkeypatch):
    """The process died between writing it and queueing it: past its grace
    it is given up on, and the person sends again."""
    monkeypatch.setattr(panel_intake.settings, "panel_sending_grace_seconds", 60)

    async def body(conn):
        stuck = await panel.create_request(conn, staff_id=RUI.id, chat_id=RUI.chat_id,
                                           kind="message", text="perdida")
        assert stuck
        async with conn.cursor() as cur:
            await cur.execute("UPDATE panel_requests SET updated_at = NOW() - interval "
                              "'61 seconds' WHERE id = %s", (stuck[0],))
        await conn.commit()
        return await panel_intake.submit_message(conn, RUI, "de novo")

    assert db.run(body).outcome == "queued"


def test_a_label_reaches_the_turn_and_the_command_the_queue(db, queue, daytime):
    async def body(conn):
        intake = await panel_intake.submit_message(conn, RUI, "/feriados", label="Feriados")
        return intake, await _rows(conn, "SELECT text, extras FROM conversation_turns")

    intake, turns = db.run(body)

    assert intake.outcome == "queued"
    assert turns == [("/feriados", {"action": "Feriados"})]
    update, _deferred = queue.items[0]
    assert update["message"]["text"] == "/feriados"
