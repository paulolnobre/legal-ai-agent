"""A failed notice to the app never costs the delivery it follows.

Each test runs a real sending point (the panel worker ending a request, the
publication watch, the access request told to the managers) with the push
switched on and the notice's own work failing inside `notify`. What must
still happen is the delivery that came first: the reply committed and the
request ended `done`, every Telegram message sent and recorded, every
manager told. Each also checks that the notice comes after its delivery,
never before, since a notice for something not yet delivered points at
nothing.
"""
import asyncio
import contextlib
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from core.config import settings
from core.handlers import legal_message
from core.integrations import djen
from core.models import AccessRequest, StaffMember
from core.repositories import callback_actions, conversation, panel
from core.repositories.publications import Watch
from core.services import channels, panel_intake, publication_watch, push, work_hours

PAULO = StaffMember(id=1, chat_id="100", name="Paulo", role="owner")
RUI = StaffMember(id=4, chat_id="400", name="Rui", role="advogado")
_REQUEST = AccessRequest(id=3, request_code="NS-A7B2988C", chat_id="987", name="Helena")


@pytest.fixture
def switched_on(monkeypatch):
    """Push ready and the app switched on for everyone, so `notify` gets past
    its gates; its own work then fails, after saying when it ran."""
    monkeypatch.setattr(settings, "web_enabled", True)
    monkeypatch.setattr(settings, "web_app", "on")
    monkeypatch.setattr(settings, "web_push_vapid_public_key", "BPub")
    monkeypatch.setattr(settings, "web_push_vapid_private_key", SecretStr("private"))
    monkeypatch.setattr(settings, "web_push_subject", "mailto:contato@example.com")
    events: list[tuple] = []
    probe: list = []

    async def failing(staff_id, kind):
        events.append(("push", staff_id, kind))
        for look in probe:
            events.append(("seen", await look()))
        raise RuntimeError("the push service is down")

    monkeypatch.setattr(push, "_notify", failing)
    return SimpleNamespace(events=events, probe=probe)


# ── answer ──────────────────────────────────────────────────────────────────


@pytest.fixture
def panel_world(db, monkeypatch):
    """The panel worker on this run's database: the queue in a list, Telegram
    recorded (the person served is never written to there), no limits."""
    queued: list[dict] = []
    telegram: list[str] = []

    async def enqueue(update, *, deferred):
        queued.append(update)
        return True

    async def allow(*_args, **_kwargs):
        return True, False

    def spy(name):
        async def fake(*args, **_kwargs):
            telegram.append(str(args[0]) if args else "")
            return 1
        return fake

    class _Pool:
        def connection(self, timeout=None):
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

    for name in ("send_message", "send_document", "send_typing", "edit_message_text",
                 "answer_callback_query"):
        monkeypatch.setattr(channels, name, spy(name))
    monkeypatch.setattr(panel_intake.update_queue, "enqueue_update", enqueue)
    monkeypatch.setattr(work_hours, "is_office_hours", lambda: True)
    monkeypatch.setattr(legal_message, "_limiter", SimpleNamespace(check=lambda _c: (True, False)))
    monkeypatch.setattr(legal_message.cache, "rate_limit_hit", allow)
    monkeypatch.setattr(legal_message.settings, "conversational_actions", False)
    monkeypatch.setattr(legal_message, "get_pool", get_pool)

    async def committed(staff_id: int):
        """What another connection sees: only what was committed."""
        other = await db.connect()
        try:
            async with other.cursor() as cur:
                await cur.execute("SELECT status FROM panel_requests WHERE id = %s",
                                  (queued[-1]["panel_request"],))
                status = (await cur.fetchone() or [None])[0]
                await cur.execute("SELECT text FROM conversation_turns WHERE staff_id = %s "
                                  "AND speaker = 'bot' AND channel = 'panel' ORDER BY id",
                                  (staff_id,))
                turns = [row[0] for row in await cur.fetchall()]
            return status, turns
        finally:
            await other.close()

    return SimpleNamespace(queued=queued, telegram=telegram, committed=committed)


def test_answer_push_failure_still_ends_done_with_the_reply_committed(
        db, switched_on, panel_world):
    switched_on.probe.append(lambda: panel_world.committed(RUI.id))

    async def body(conn):
        intake = await panel_intake.submit_message(conn, RUI, "/id")
        assert intake.outcome == "queued", intake
        await legal_message.process_legal_message(conn, panel_world.queued[-1])
        await conn.commit()
        return await panel_world.committed(RUI.id)

    status, turns = db.run(body)

    assert status == "done"
    assert turns == ["Seu identificador: 400"]
    assert panel_world.telegram == [], "the panel's reply never goes to Telegram"
    assert switched_on.events == [
        ("push", RUI.id, "answer"),
        ("seen", ("done", ["Seu identificador: 400"])),
    ], "the notice goes to the writer, after the reply and the ending are committed"


def test_a_message_request_ending_done_notifies_once(db, switched_on, panel_world):
    async def body(conn):
        await panel_intake.submit_message(conn, RUI, "/id")
        await legal_message.process_legal_message(conn, panel_world.queued[-1])
        # A redelivery finds it ended and runs nothing, the notice included.
        await legal_message.process_legal_message(conn, panel_world.queued[-1])
        return await panel_world.committed(RUI.id)

    assert db.run(body)[0] == "done"
    assert switched_on.events == [("push", RUI.id, "answer")]


def test_a_person_deactivated_while_answered_gets_no_notice(db, switched_on, panel_world,
                                                            monkeypatch):
    """Deactivated mid-answer: the answer is not written (the publication
    guard refuses it) and the request still exists, so only the stop itself
    keeps a notice from pointing at an answer that is not there."""
    from core.services.legal_assistant.reply import AgentReply

    async def answer_question(conn, question, **_kwargs):
        other = await db.connect()
        try:
            async with other.cursor() as cur:
                await cur.execute("UPDATE staff SET active = FALSE WHERE id = %s", (RUI.id,))
            await other.commit()
        finally:
            await other.close()
        return AgentReply(text="resposta que não chega")

    monkeypatch.setattr(legal_message, "answer_question", answer_question)

    async def body(conn):
        intake = await panel_intake.submit_message(conn, RUI, "o que é usucapião?")
        assert intake.outcome == "queued", intake
        await legal_message.process_legal_message(conn, panel_world.queued[-1])
        await conn.commit()
        return await panel_world.committed(RUI.id)

    status, turns = db.run(body)

    assert status == "failed"
    assert turns == []
    assert switched_on.events == []


async def _offer_access_buttons(conn, request_code: str) -> tuple[int, str]:
    """An access-request notice in Paulo's panel, with its approve button."""
    token = await callback_actions.issue(
        conn, chat_id="100", staff_id=PAULO.id, issued_role="owner",
        action="access.review", operation="approve", resource=request_code,
        ttl_seconds=600, channel="panel")
    turn_id = await conversation.record_turn(
        conn, staff_id=PAULO.id, chat_id="100", speaker="bot",
        text="🔔 Nova solicitação de acesso: Helena", channel="panel",
        extras=panel.buttons_extras([[("Autorizar", token)]]))
    await callback_actions.attach_reference(conn, [token], reference=str(turn_id))
    await conn.commit()
    return turn_id, token


def test_a_press_request_ending_done_notifies_nothing(db, switched_on, panel_world,
                                                      monkeypatch):
    """A press (a rating, a confirmation, a decision) also ends `done`, but
    it is not a new answer: the person is looking at the turn they pressed."""
    decided: list[bool] = []

    async def decide(_conn, code, *, approved, decided_by):
        decided.append(approved)
        return _REQUEST

    monkeypatch.setattr(legal_message, "decide_access_request", decide)

    async def body(conn):
        turn_id, approve = await _offer_access_buttons(conn, _REQUEST.request_code)
        press = await panel_intake.submit_press(conn, PAULO, token=approve, turn_id=turn_id)
        assert press.outcome == "queued", press
        await legal_message.process_legal_message(conn, panel_world.queued[-1])
        return await panel_world.committed(PAULO.id)

    status, turns = db.run(body)

    assert decided == [True], "the press really ran"
    assert status == "done"
    assert "Autorizado." in turns
    assert switched_on.events == []


# ── djen ────────────────────────────────────────────────────────────────────


def _comm(pub_id: int, day: date) -> djen.Communication:
    return djen.Communication(
        process_number="0825046-87.2025.8.14.0301", court="TJPA", kind="Intimação",
        document_type="Decisão", body="1ª Vara Cível", case_class="Procedimento Comum",
        made_available=day, text="DECISÃO Defiro.", link=None, publication_id=pub_id,
    )


def _watch(staff_id: int, key: str) -> Watch:
    return Watch(staff_id=staff_id, chat_id=str(staff_id * 100), kind="oab", key=key,
                 since=datetime(2026, 9, 1, 12, tzinfo=timezone.utc))


def test_djen_push_failure_still_sends_and_records_telegram(switched_on, monkeypatch):
    events = switched_on.events
    by_oab = {"12345/PA": [_comm(1, date(2026, 9, 21))],
              "777/PA": [_comm(2, date(2026, 9, 21))]}

    async def for_lawyer(number, uf, *, since, until):
        return by_oab.get(f"{number}/{uf}", [])

    async def for_process(number, *, limit=None):
        return []

    class _Pool:
        def connection(self):
            return self

        async def __aenter__(self):
            return "conn"

        async def __aexit__(self, *exc):
            return False

    async def pool():
        return _Pool()

    async def watches(_conn):
        return [_watch(4, "12345/PA"), _watch(6, "777/PA")]

    async def noticed(_conn, ids):
        return set()

    async def record(_conn, staff_id, ids):
        events.append(("recorded", staff_id, sorted(ids)))

    async def purge(_conn, *, keep_days):
        return 0

    async def send(chat_id, text, parse_mode="Markdown", **_kwargs):
        events.append(("telegram", chat_id))
        return 1

    monkeypatch.setattr(publication_watch.djen, "communications_for_lawyer", for_lawyer)
    monkeypatch.setattr(publication_watch.djen, "communications", for_process)
    monkeypatch.setattr(publication_watch, "get_pool", pool)
    monkeypatch.setattr(publication_watch, "list_active_watches", watches)
    monkeypatch.setattr(publication_watch, "noticed", noticed)
    monkeypatch.setattr(publication_watch, "record_notices", record)
    monkeypatch.setattr(publication_watch, "purge_old_notices", purge)
    monkeypatch.setattr(publication_watch.telegram, "send_message", send)
    monkeypatch.setattr(publication_watch, "local_now",
                        lambda now=None: datetime(2026, 9, 21, 15, tzinfo=timezone.utc))

    delivered = asyncio.run(publication_watch.check_once())

    assert delivered == 2
    per_person = sorted([events[i:i + 3] for i in range(0, len(events), 3)])
    assert per_person == [
        [("telegram", "400"), ("recorded", 4, [1]), ("push", 4, "djen")],
        [("telegram", "600"), ("recorded", 6, [2]), ("push", 6, "djen")],
    ], events


def test_djen_sends_no_notice_to_a_person_whose_message_did_not_go_out(
        switched_on, monkeypatch):
    events = switched_on.events

    async def for_lawyer(number, uf, *, since, until):
        return [_comm(1, date(2026, 9, 21))]

    async def nothing(*_args, **_kwargs):
        return []

    class _Pool:
        def connection(self):
            return self

        async def __aenter__(self):
            return "conn"

        async def __aexit__(self, *exc):
            return False

    async def pool():
        return _Pool()

    async def watches(_conn):
        return [_watch(4, "12345/PA")]

    async def noticed(_conn, ids):
        return set()

    async def record(_conn, staff_id, ids):
        events.append(("recorded", staff_id))

    async def purge(_conn, *, keep_days):
        return 0

    async def undelivered(chat_id, text, parse_mode="Markdown", **_kwargs):
        events.append(("telegram", chat_id))
        return None

    monkeypatch.setattr(publication_watch.djen, "communications_for_lawyer", for_lawyer)
    monkeypatch.setattr(publication_watch.djen, "communications", nothing)
    monkeypatch.setattr(publication_watch, "get_pool", pool)
    monkeypatch.setattr(publication_watch, "list_active_watches", watches)
    monkeypatch.setattr(publication_watch, "noticed", noticed)
    monkeypatch.setattr(publication_watch, "record_notices", record)
    monkeypatch.setattr(publication_watch, "purge_old_notices", purge)
    monkeypatch.setattr(publication_watch.telegram, "send_message", undelivered)
    monkeypatch.setattr(publication_watch, "local_now",
                        lambda now=None: datetime(2026, 9, 21, 15, tzinfo=timezone.utc))

    assert asyncio.run(publication_watch.check_once()) == 0
    assert events == [("telegram", "400")]


# ── access ──────────────────────────────────────────────────────────────────


_IVO = StaffMember(id=9, chat_id="900", name="Ivo", role="admin")
_BIA = StaffMember(id=10, chat_id="1000", name="Bia", role="owner")


@pytest.mark.usefixtures("isolate_message_rate_limits")
def test_access_push_failure_still_tells_every_manager(switched_on, monkeypatch,
                                                       callback_store):
    events = switched_on.events
    ids = iter([501, 502])

    async def managers(*_args, **_kwargs):
        return [_IVO, _BIA]

    async def send(chat_id, text, reply_markup=None):
        events.append(("telegram", chat_id, bool(reply_markup)))
        return next(ids)

    monkeypatch.setattr(legal_message, "list_staff", managers)
    monkeypatch.setattr(legal_message, "_send", send)

    asyncio.run(legal_message._notify_managers("connection", _REQUEST))

    assert events == [
        ("telegram", "900", True), ("push", 9, "access"),
        ("telegram", "1000", True), ("push", 10, "access"),
    ]
    assert {row["chat_id"] for row in callback_store.rows.values()} == {"900", "1000"}
