"""The media stage in the worker, and the publication guard, on a real Postgres.

What makes these properties true is SQL: a deadline column the settle query
reads, and two row locks taken in the order erasure takes them. So they are
tested on the real thing, with a second connection playing the erasure and
an event pausing the worker where the race would happen.
"""
import asyncio
import contextlib
import io
from types import SimpleNamespace
from typing import LiteralString
from uuid import UUID

import psycopg
import pytest
from PIL import Image

from core import document_crypto, legal_agent
from core.config import settings
from core.handlers import legal_message
from core.integrations import object_store
from core.models import StaffMember
from core.repositories import panel, panel_uploads, staff
from core.services import (
    authorization,
    budget,
    channels,
    document_storage,
    ingestion,
    media,
    panel_intake,
    work_hours,
)

PAULO = StaffMember(id=1, chat_id="100", name="Paulo", role="owner")
RUI = StaffMember(id=4, chat_id="400", name="Rui", role="advogado")


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 120, 200)).save(out, "PNG")
    return out.getvalue()


async def _rows(conn, statement: LiteralString, params=()):
    async with conn.cursor() as cur:
        await cur.execute(statement, params)
        rows = await cur.fetchall()
    await conn.commit()
    return rows


async def _execute(conn, statement: LiteralString, params=()):
    async with conn.cursor() as cur:
        await cur.execute(statement, params)
    await conn.commit()


async def _waiting_on_a_lock(conn, pid: int) -> bool:
    rows = await _rows(conn, "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                       (pid,))
    return bool(rows) and rows[0][0] == "Lock"


async def _until_waiting(conn, other, task) -> bool:
    for _ in range(250):
        if task.done():
            return False
        if await _waiting_on_a_lock(conn, other.info.backend_pid):
            return True
        await asyncio.sleep(0.02)
    return False


class _CommitHeld:
    """A connection whose commit waits for the test: an erasure that has done
    all its work and still holds the staff row."""

    def __init__(self, conn):
        self._conn = conn
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def commit(self):
        self.reached.set()
        await self.release.wait()
        await self._conn.commit()


@pytest.fixture
def world(db, monkeypatch):
    """Daytime, no rate limit, Telegram spied, the queue in a list, every
    fresh connection on this run's database, the documents' key set, and the
    answer replaced by a stub that records its question and says one line."""
    telegram: list[str] = []
    queued: list[dict] = []
    questions: list[str] = []
    answer = SimpleNamespace(gate=None, reached=None, delay=0.0)

    def spy(name):
        async def fake(*_args, **_kwargs):
            telegram.append(name)
            return 1
        return fake

    for name in ("send_message", "send_document", "send_typing", "edit_message_text",
                 "answer_callback_query"):
        monkeypatch.setattr(channels, name, spy(name))

    async def enqueue(update, *, deferred):
        queued.append(update)
        return True

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

    # The limiter the text path uses, counted and refusable: (allowed, notify).
    limits = SimpleNamespace(answer=(True, False), local=0, shared=0)

    def check(_chat_id):
        limits.local += 1
        return limits.answer

    async def rate_limit_hit(*_args, **_kwargs):
        limits.shared += 1
        return True, False

    async def serve_message(conn, update, msg, chat_id, staff_member, text, *_rest,
                            **_kwargs):
        questions.append(text)
        if answer.reached is not None:
            answer.reached.set()
        if answer.gate is not None:
            await answer.gate.wait()
        if answer.delay:
            await asyncio.sleep(answer.delay)
        await legal_message._send(chat_id, "resposta")

    monkeypatch.setattr(settings, "doc_master_key", document_crypto.new_master_key())
    monkeypatch.setattr(panel_intake.update_queue, "enqueue_update", enqueue)
    monkeypatch.setattr(work_hours, "is_office_hours", lambda: True)
    monkeypatch.setattr(legal_message, "get_pool", get_pool)
    monkeypatch.setattr(legal_agent, "get_pool", get_pool)
    monkeypatch.setattr(legal_message, "_serve_message", serve_message)
    monkeypatch.setattr(legal_message, "_limiter", SimpleNamespace(check=check))
    monkeypatch.setattr(legal_message.cache, "rate_limit_hit", rate_limit_hit)
    return SimpleNamespace(telegram=telegram, queued=queued, questions=questions,
                           answer=answer, limits=limits)


@pytest.fixture
def unsealed(monkeypatch):
    """Every time an upload's seal is opened, recorded."""
    calls: list[bool] = []
    real = document_crypto.open_sealed

    def spy(*args, **kwargs):
        calls.append(True)
        return real(*args, **kwargs)

    monkeypatch.setattr(document_crypto, "open_sealed", spy)
    return calls


@pytest.fixture
def reading(monkeypatch):
    """The model's reading of a photo, recorded."""
    calls: list[str] = []
    behaviour = SimpleNamespace(result="Contrato de locação, R$ 2.000,00.", delay=0.0)

    async def complete_with_image(task, prompt, image_jpeg, *, max_tokens):
        calls.append(prompt)
        if behaviour.delay:
            await asyncio.sleep(behaviour.delay)
        if isinstance(behaviour.result, BaseException):
            raise behaviour.result
        return behaviour.result

    monkeypatch.setattr(media.llm, "complete_with_image", complete_with_image)
    return SimpleNamespace(calls=calls, behaviour=behaviour)


async def _send_with(conn, world, who: StaffMember, text: str, *,
                     kind: panel_uploads.UploadKind = "photo",
                     data: bytes | None = None, media_type="image/png") -> dict:
    upload_id = await panel_uploads.store_upload(
        conn, who.id, kind=kind, media_type=media_type, data=data or _png(), archive=False)
    assert upload_id is not None
    intake = await panel_intake.submit_message(conn, who, text, upload_id=upload_id)
    assert intake.outcome == "queued", intake
    return world.queued[-1]


async def _turns(conn, who: StaffMember):
    return await _rows(conn, "SELECT speaker, text, extras FROM conversation_turns "
                             "WHERE staff_id = %s ORDER BY id", (who.id,))


async def _bot_texts(conn, who: StaffMember) -> list[str]:
    return [t[1] for t in await _turns(conn, who) if t[0] == "bot"]


async def _status(conn, update) -> str | None:
    rows = await _rows(conn, "SELECT status FROM panel_requests WHERE id = %s",
                       (update["panel_request"],))
    return rows[0][0] if rows else None


async def _uploads(conn) -> int:
    return (await _rows(conn, "SELECT COUNT(*) FROM panel_uploads"))[0][0]


# ── the deadline ─────────────────────────────────────────────────────────────


async def _running(conn, who: StaffMember = RUI) -> UUID:
    created = await panel.create_request(conn, staff_id=who.id, chat_id=who.chat_id,
                                         kind="message", text="o que diz?")
    assert created is not None
    assert await panel.start(conn, created[0])
    return created[0]


def test_settle_does_not_fail_a_request_inside_its_media_deadline_plus_the_grace(db):
    """Started long ago, past the 420 s that end a text request, and still
    reading its attachment: not failed, and the person cannot send another."""
    async def body(conn):
        request_id = await _running(conn)
        await panel.begin_media(conn, request_id, 900)
        await _execute(conn, "UPDATE panel_requests SET started_at = NOW() - interval "
                             "'20 minutes', media_deadline_at = NOW() - interval '400 seconds' "
                             "WHERE id = %s", (request_id,))
        inside = await panel.in_flight(conn, RUI.id)
        await _execute(conn, "UPDATE panel_requests SET media_deadline_at = NOW() - interval "
                             "'421 seconds' WHERE id = %s", (request_id,))
        await panel.settle(conn, RUI.id)
        after = await _rows(conn, "SELECT status FROM panel_requests WHERE id = %s",
                            (request_id,))
        return request_id, inside, after

    request_id, inside, after = db.run(body)

    assert inside is not None and inside.id == request_id and inside.status == "running"
    assert after == [("failed",)]


def test_begin_media_sets_the_deadline_from_now(db):
    async def body(conn):
        request_id = await _running(conn)
        await panel.begin_media(conn, request_id, 900)
        return await _rows(conn, "SELECT media_deadline_at - clock_timestamp() "
                                 "FROM panel_requests WHERE id = %s", (request_id,))

    [(left,)] = db.run(body)
    assert 890 < left.total_seconds() <= 900


def test_a_text_request_is_still_settled_from_its_start(db):
    async def body(conn):
        request_id = await _running(conn)
        await _execute(conn, "UPDATE panel_requests SET started_at = NOW() - interval "
                             "'421 seconds' WHERE id = %s", (request_id,))
        await panel.settle(conn, RUI.id)
        return await _rows(conn, "SELECT status FROM panel_requests WHERE id = %s",
                           (request_id,))

    assert db.run(body) == [("failed",)]


# ── the guard ────────────────────────────────────────────────────────────────


def test_the_guard_says_whether_the_request_may_still_be_written(db):
    async def body(conn):
        request_id = await _running(conn)
        results = {}
        async with panel.publishing(conn, request_id) as ok:
            results["live"] = ok
        await _execute(conn, "UPDATE staff SET active = FALSE WHERE id = %s", (RUI.id,))
        async with panel.publishing(conn, request_id) as ok:
            results["inactive"] = ok
        async with panel.publishing(conn, request_id, require_active=False) as ok:
            results["inactive, refusal"] = ok
        await _execute(conn, "DELETE FROM panel_requests WHERE id = %s", (request_id,))
        async with panel.publishing(conn, request_id, require_active=False) as ok:
            results["gone"] = ok
        return results

    assert db.run(body) == {"live": True, "inactive": False, "inactive, refusal": True,
                            "gone": False}


def test_the_guard_waits_for_the_staff_row_before_it_locks_the_request(db):
    """Erasure locks the staff row, then the requests. The guard must take
    them in that same order: while it waits for the staff row it holds
    nothing on the request, or the two would deadlock."""
    async def body(conn):
        request_id = await _running(conn)
        holder, worker, prober = await db.connect(), await db.connect(), await db.connect()
        try:
            async with holder.cursor() as cur:
                await cur.execute("SELECT 1 FROM staff WHERE id = %s FOR UPDATE", (RUI.id,))

            async def publish():
                async with panel.publishing(worker, request_id) as ok:
                    return ok

            guard = asyncio.create_task(publish())
            waiting = await _until_waiting(conn, worker, guard)
            try:
                async with prober.cursor() as cur:
                    await cur.execute("SELECT 1 FROM panel_requests WHERE id = %s "
                                      "FOR UPDATE NOWAIT", (request_id,))
                request_free = True
            except psycopg.errors.LockNotAvailable:
                request_free = False
            await prober.rollback()
            await holder.rollback()
            ok = await asyncio.wait_for(guard, timeout=5)
        finally:
            for c in (holder, worker, prober):
                await c.close()
        return waiting, request_free, ok

    assert db.run(body) == (True, True, True)


def test_while_it_writes_the_guard_holds_the_request_row(db):
    async def body(conn):
        request_id = await _running(conn)
        worker, prober = await db.connect(), await db.connect()
        try:
            async with panel.publishing(worker, request_id) as ok:
                assert ok
                try:
                    async with prober.cursor() as cur:
                        await cur.execute("SELECT 1 FROM panel_requests WHERE id = %s "
                                          "FOR UPDATE NOWAIT", (request_id,))
                    held = False
                except psycopg.errors.LockNotAvailable:
                    held = True
                await prober.rollback()
        finally:
            await worker.close()
            await prober.close()
        return held

    assert db.run(body) is True


def test_an_erasure_between_its_turns_and_its_requests_leaves_no_turn_behind(db):
    """Erasure deletes the person's turns, then their requests, holding the
    staff row throughout. A write that slipped in between, without waiting
    for that row, would land after the turns' delete and survive it."""
    async def body(conn):
        request_id = await _running(conn)
        eraser, worker = await db.connect(), await db.connect()
        try:
            async with eraser.cursor() as cur:
                await cur.execute("SELECT 1 FROM staff WHERE id = %s FOR UPDATE", (RUI.id,))
                await cur.execute("DELETE FROM conversation_turns WHERE staff_id = %s",
                                  (RUI.id,))

            async def publish():
                async with panel.publishing(worker, request_id) as ok:
                    if ok:
                        async with worker.cursor() as cur:
                            await cur.execute(
                                "INSERT INTO conversation_turns (staff_id, chat_id, speaker, "
                                "text, channel, request_id) VALUES (%s, '400', 'bot', "
                                "'resposta', 'panel', %s)", (RUI.id, request_id))
                    return ok

            guard = asyncio.create_task(publish())
            await asyncio.sleep(0.3)

            async def finish_erasure():
                async with eraser.cursor() as cur:
                    await cur.execute("DELETE FROM panel_requests WHERE staff_id = %s",
                                      (RUI.id,))
                    await cur.execute("UPDATE staff SET active = FALSE WHERE id = %s",
                                      (RUI.id,))
                await eraser.commit()

            await asyncio.wait_for(asyncio.gather(guard, finish_erasure()), timeout=10)
            ok = guard.result()
        finally:
            await eraser.close()
            await worker.close()
        return ok, await _rows(conn, "SELECT text FROM conversation_turns "
                                     "WHERE staff_id = %s", (RUI.id,))

    assert db.run(body) == (False, [])


def test_erasure_first_then_a_refusal_to_a_deactivated_person_writes_nothing(db, world):
    """The one write that does not need the person active still needs the
    request: an erasure that took it leaves the refusal nowhere to go."""
    async def body(conn):
        request_id = await _running(conn)
        eraser, worker = await db.connect(), await db.connect()
        try:
            held = _CommitHeld(eraser)
            erasure = asyncio.create_task(
                staff.erase_person(held, actor_id=PAULO.id, target_id=RUI.id))
            await asyncio.wait_for(held.reached.wait(), timeout=5)
            channel = channels.PanelChannel(worker, staff_id=RUI.id, chat_id=RUI.chat_id,
                                            request_id=request_id)
            refusal = asyncio.create_task(channel.refuse(legal_message._PANEL_NO_ACCESS))
            waited = await _until_waiting(conn, worker, refusal)
            held.release.set()
            await asyncio.wait_for(erasure, timeout=5)
            with pytest.raises(panel.RequestGone):
                await asyncio.wait_for(refusal, timeout=5)
        finally:
            await eraser.close()
            await worker.close()
        return waited, await _turns(conn, RUI)

    assert db.run(body) == (True, [])


async def _upload_of(conn, request_id: UUID) -> panel_uploads.Upload:
    upload = await panel_uploads.take_upload(conn, request_id)
    assert upload is not None
    return upload


def test_erasure_first_then_the_reading_publishes_nothing(db, world):
    async def body(conn):
        update = await _send_with(conn, world, RUI, "o que diz?")
        request_id = UUID(update["panel_request"])
        assert await panel.start(conn, request_id)
        upload = await _upload_of(conn, request_id)
        eraser, worker = await db.connect(), await db.connect()
        try:
            held = _CommitHeld(eraser)
            erasure = asyncio.create_task(
                staff.erase_person(held, actor_id=PAULO.id, target_id=RUI.id))
            await asyncio.wait_for(held.reached.wait(), timeout=5)
            publish = asyncio.create_task(legal_message._publish_reading(
                worker, request_id, RUI.chat_id or "", upload, "Leitura da foto:\nx"))
            waited = await _until_waiting(conn, worker, publish)
            held.release.set()
            await asyncio.wait_for(erasure, timeout=5)
            with pytest.raises(panel.RequestGone):
                await asyncio.wait_for(publish, timeout=5)
        finally:
            await eraser.close()
            await worker.close()
        return waited, await _turns(conn, RUI), await _uploads(conn)

    waited, turns, uploads = db.run(body)

    assert waited, "the reading waited for the erasure's staff lock"
    assert turns == [] and uploads == 0


def test_publication_first_is_then_erased_and_an_archived_document_stays(db, world):
    """The guard's transaction is where the archive is written (Task M.4):
    a document written there survives the erasure, as every document a
    person uploaded does; the reading and the upload do not."""
    async def body(conn):
        update = await _send_with(conn, world, RUI, "o que diz?")
        request_id = UUID(update["panel_request"])
        assert await panel.start(conn, request_id)
        upload = await _upload_of(conn, request_id)
        eraser, worker = await db.connect(), await db.connect()
        inside, release = asyncio.Event(), asyncio.Event()

        async def publish():
            async with panel.publishing(worker, request_id) as ok:
                assert ok
                async with worker.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO conversation_turns (staff_id, chat_id, speaker, text, "
                        "channel, request_id) VALUES (%s, %s, 'bot', 'Leitura da foto:\nx', "
                        "'panel', %s)", (RUI.id, RUI.chat_id, request_id))
                    await cur.execute(
                        "INSERT INTO documents (id, title, doc_type, full_text, uploaded_by, "
                        "visibility, content_sha256) VALUES (301, 'Foto', 'foto', 'x', %s, "
                        "'private', 'c301')", (RUI.id,))
                await panel_uploads.delete_upload(worker, upload.id, commit=False)
                inside.set()
                await release.wait()

        try:
            publishing = asyncio.create_task(publish())
            await asyncio.wait_for(inside.wait(), timeout=5)
            erasure = asyncio.create_task(
                staff.erase_person(eraser, actor_id=PAULO.id, target_id=RUI.id))
            waited = await _until_waiting(conn, eraser, erasure)
            release.set()
            await asyncio.wait_for(publishing, timeout=5)
            erased = await asyncio.wait_for(erasure, timeout=5)
        finally:
            await eraser.close()
            await worker.close()
        documents = await _rows(conn, "SELECT id, uploaded_by FROM documents WHERE id = 301")
        return (waited, erased.status, await _turns(conn, RUI), await _uploads(conn),
                await _status(conn, update), documents)

    waited, outcome, turns, uploads, status, documents = db.run(body)

    assert waited, "the erasure waited for the publication"
    assert outcome == "erased"
    assert turns == [] and uploads == 0 and status is None
    assert documents == [(301, RUI.id)]


def test_an_erasure_while_the_answer_is_paused_leaves_no_answer_and_no_deadlock(
        db, world, reading):
    async def body(conn):
        update = await _send_with(conn, world, RUI, "")
        world.answer.reached, world.answer.gate = asyncio.Event(), asyncio.Event()
        eraser = await db.connect()
        try:
            serving = asyncio.create_task(legal_agent._handle_update(update))
            await asyncio.wait_for(world.answer.reached.wait(), timeout=10)
            before = await _bot_texts(conn, RUI)
            erased = await asyncio.wait_for(
                staff.erase_person(eraser, actor_id=PAULO.id, target_id=RUI.id), timeout=5)
            world.answer.gate.set()
            await asyncio.wait_for(serving, timeout=10)
        finally:
            await eraser.close()
        return (before, erased.status, await _turns(conn, RUI), await _uploads(conn),
                await _status(conn, update))

    before, outcome, turns, uploads, status = db.run(body)

    assert before == ["Leitura da foto:\nContrato de locação, R$ 2.000,00."]
    assert outcome == "erased", "the paused worker held no lock the erasure needed"
    assert turns == [] and uploads == 0 and status is None
    assert world.questions == ["o que é isto?"]
    assert world.telegram == []


# ── the stage, end to end ────────────────────────────────────────────────────


def test_a_photo_is_read_published_with_its_upload_deleted_then_answered(db, world,
                                                                         reading):
    async def body(conn):
        update = await _send_with(conn, world, RUI, "qual o valor do aluguel?")
        await legal_agent._handle_update(update)
        deadline = await _rows(conn, "SELECT media_deadline_at IS NOT NULL "
                                     "FROM panel_requests WHERE id = %s",
                               (update["panel_request"],))
        return (await _turns(conn, RUI), await _uploads(conn), await _status(conn, update),
                deadline)

    turns, uploads, status, deadline = db.run(body)

    assert turns == [
        ("person", "qual o valor do aluguel?", {"attachment": {"kind": "photo", "size_bytes": len(_png())}}),
        ("bot", "Leitura da foto:\nContrato de locação, R$ 2.000,00.", {"reading": "photo"}),
        ("bot", "resposta", None),
    ]
    assert uploads == 0 and status == "done" and deadline == [(True,)]
    assert "qual o valor do aluguel?" in reading.calls[0]
    assert world.questions == ["qual o valor do aluguel?"]
    assert world.telegram == []


def test_a_photo_alone_is_asked_what_it_is(db, world, reading):
    async def body(conn):
        await legal_agent._handle_update(await _send_with(conn, world, RUI, ""))

    db.run(body)

    assert world.questions == ["o que é isto?"]
    assert "o que é isto?" in reading.calls[0]


@pytest.fixture
def transcribed(monkeypatch):
    calls: list[dict] = []
    result = SimpleNamespace(text="qual o prazo do recurso", seconds=125.0)

    async def transcribe_audio(data, *, voice, prompt):
        calls.append({"voice": voice, "prompt": prompt})
        return media.Transcript(text=result.text, seconds=result.seconds)

    monkeypatch.setattr(media, "transcribe_audio", transcribe_audio)
    return SimpleNamespace(calls=calls, result=result)


def test_a_voice_note_is_echoed_and_its_transcript_is_the_question(db, world, transcribed):
    async def body(conn):
        update = await _send_with(conn, world, RUI, "", kind="voice", data=b"OggS voice",
                                  media_type="audio/ogg")
        await legal_agent._handle_update(update)
        return await _bot_texts(conn, RUI), await _uploads(conn), await _status(conn, update)

    texts, uploads, status = db.run(body)

    assert texts == ['🎤 Entendi: "qual o prazo do recurso"', "resposta"]
    assert world.questions == ["qual o prazo do recurso"]
    assert transcribed.calls == [{"voice": True, "prompt": legal_message._VOICE_HINT}]
    assert uploads == 0 and status == "done"


def test_a_silent_voice_note_gets_the_telegram_answer(db, world, transcribed):
    transcribed.result.text = None

    async def body(conn):
        update = await _send_with(conn, world, RUI, "", kind="voice", data=b"OggS voice",
                                  media_type="audio/ogg")
        await legal_agent._handle_update(update)
        return await _bot_texts(conn, RUI), await _uploads(conn)

    texts, uploads = db.run(body)

    assert texts == [legal_message._SILENT_VOICE]
    assert world.questions == [] and uploads == 0


def test_an_audio_file_is_transcribed_and_alone_asks_for_a_summary(db, world, transcribed):
    async def body(conn):
        update = await _send_with(conn, world, RUI, "", kind="audio", data=b"OggS audio",
                                  media_type="audio/ogg")
        await legal_agent._handle_update(update)
        return await _bot_texts(conn, RUI)

    texts = db.run(body)

    assert texts == ["Transcrição (3 min):\nqual o prazo do recurso", "resposta"]
    assert world.questions == ["Resuma a transcrição."]
    assert transcribed.calls[0]["voice"] is False


@pytest.mark.parametrize("failure, said", [
    (media.MediaError(media.PHOTO_TOO_LARGE), media.PHOTO_TOO_LARGE),
    (RuntimeError("provider exploded"), media.UNREADABLE),
    (budget.BudgetExceeded("day", "amanhã"), None),
])
def test_a_reading_that_fails_says_so_deletes_the_upload_and_answers_nothing(
        db, world, reading, failure, said):
    reading.behaviour.result = failure

    async def body(conn):
        update = await _send_with(conn, world, RUI, "o que diz?")
        await legal_agent._handle_update(update)
        return await _bot_texts(conn, RUI), await _uploads(conn), await _status(conn, update)

    texts, uploads, status = db.run(body)

    if said is None:
        said = legal_message.budget_reached(budget.BudgetExceeded("day", "amanhã"))
        assert said.startswith("O orçamento do piloto")
    assert texts == [said]
    assert uploads == 0 and status == "done"
    assert world.questions == []


def test_a_reading_past_its_stage_deadline_is_cut_and_said(db, world, reading, monkeypatch):
    monkeypatch.setattr(settings, "panel_media_photo_seconds", 0.3)
    reading.behaviour.delay = 30

    async def body(conn):
        update = await _send_with(conn, world, RUI, "o que diz?")
        await asyncio.wait_for(legal_agent._handle_update(update), timeout=10)
        return await _bot_texts(conn, RUI), await _uploads(conn), await _status(conn, update)

    assert db.run(body) == ([media.UNREADABLE], 0, "done")
    assert world.questions == []


def test_the_stage_has_its_own_time_and_the_answer_its_usual_time_after_it(
        db, world, reading, monkeypatch):
    """The update's limit (0.4 s here for 300 s) does not cut a stage given
    1.5 s, and the answer then gets the limit again from the stage's end."""
    monkeypatch.setattr(legal_agent, "_UPDATE_TIMEOUT", 0.4)
    monkeypatch.setattr(settings, "panel_media_photo_seconds", 1.5)
    reading.behaviour.delay = 0.7
    world.answer.delay = 0.25

    async def body(conn):
        update = await _send_with(conn, world, RUI, "o que diz?")
        await legal_agent._handle_update(update)
        return await _bot_texts(conn, RUI), await _status(conn, update)

    texts, status = db.run(body)

    assert texts == ["Leitura da foto:\nContrato de locação, R$ 2.000,00.", "resposta"]
    assert status == "done"


def test_the_answer_gets_its_usual_time_from_the_stage_end_not_from_its_deadline(
        db, world, reading, monkeypatch):
    """A reading done early leaves no spare time to the answer: the answer
    is cut its usual time (0.4 s here) after the stage ended, well before
    the stage's own deadline (3 s) would have allowed."""
    monkeypatch.setattr(legal_agent, "_UPDATE_TIMEOUT", 0.4)
    monkeypatch.setattr(settings, "panel_media_photo_seconds", 3.0)
    world.answer.delay = 1.2

    async def body(conn):
        update = await _send_with(conn, world, RUI, "o que diz?")
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(legal_agent._handle_update(update), timeout=10)
        return await _status(conn, update), loop.time() - started

    status, took = db.run(body)
    assert status == "failed"
    assert took < 1.2, f"cut after {took:.2f} s"


def test_the_reading_and_the_upload_deletion_are_one_transaction(db, world, monkeypatch):
    async def broken(*_args, **_kwargs):
        raise RuntimeError("the delete failed")

    async def body(conn):
        update = await _send_with(conn, world, RUI, "o que diz?")
        request_id = UUID(update["panel_request"])
        upload = await _upload_of(conn, request_id)
        monkeypatch.setattr(legal_message.panel_uploads, "delete_upload", broken)
        with pytest.raises(RuntimeError):
            await legal_message._publish_reading(conn, request_id, RUI.chat_id or "",
                                                 upload, "Leitura da foto:\nx")
        return await _bot_texts(conn, RUI), await _uploads(conn)

    assert db.run(body) == ([], 1), "no reading without the deletion, and the upload kept"


def test_a_deactivated_person_with_an_attachment_is_refused_and_nothing_is_read(
        db, world, reading):
    async def body(conn):
        update = await _send_with(conn, world, RUI, "o que diz?")
        await _execute(conn, "UPDATE staff SET active = FALSE WHERE id = %s", (RUI.id,))
        await legal_agent._handle_update(update)
        return await _bot_texts(conn, RUI), await _status(conn, update)

    assert db.run(body) == ([legal_message._PANEL_NO_ACCESS], "done")
    assert reading.calls == []


def test_an_answer_to_a_request_erased_meanwhile_ends_quietly(db, world):
    """No attachment at all: every later answer goes through the guard."""
    async def body(conn):
        intake = await panel_intake.submit_message(conn, RUI, "bom dia")
        update = world.queued[-1]
        assert intake.outcome == "queued"
        world.answer.reached, world.answer.gate = asyncio.Event(), asyncio.Event()
        eraser = await db.connect()
        try:
            serving = asyncio.create_task(legal_agent._handle_update(update))
            await asyncio.wait_for(world.answer.reached.wait(), timeout=10)
            await staff.erase_person(eraser, actor_id=PAULO.id, target_id=RUI.id)
            world.answer.gate.set()
            await asyncio.wait_for(serving, timeout=10)
        finally:
            await eraser.close()
        return await _turns(conn, RUI)

    assert db.run(body) == []


def test_an_export_offered_to_a_request_erased_meanwhile_is_not_stored(db, world):
    async def body(conn):
        request_id = await _running(conn)
        await staff.erase_person(conn, actor_id=PAULO.id, target_id=RUI.id)
        channel = channels.PanelChannel(conn, staff_id=RUI.id, chat_id=RUI.chat_id,
                                        request_id=request_id)
        with pytest.raises(panel.RequestGone):
            await channel.send_file(RUI.chat_id, "resposta.pdf", b"%PDF", None,
                                    kind="export", document_id=None)
        return await _rows(conn, "SELECT COUNT(*) FROM panel_files")

    assert db.run(body) == [(0,)]


@pytest.mark.parametrize("kind", ["pending", "callback", "delivery"])
def test_an_erasure_while_the_answer_is_made_leaves_no_step_button_or_delivery(
        db, world, monkeypatch, kind):
    """Spec revision 13, beyond the turns: what the answer writes on its way
    (an open step, a button's token, the answer kept for "manda em pdf") goes
    through the same guard. Served end to end, erased while it is paused:
    none of them is left behind."""
    from core.repositories import callback_actions, pending_actions
    from core.services.legal_assistant.reply import AgentReply

    async def serve_message(conn, update, msg, chat_id, staff_member, text, *_rest,
                            **_kwargs):
        world.answer.reached.set()
        await world.answer.gate.wait()
        if kind == "pending":
            await pending_actions.remember(conn, chat_id, kind="export", ttl_seconds=60,
                                           channel="panel")
        elif kind == "callback":
            await callback_actions.issue(conn, chat_id=chat_id, staff_id=staff_member.id,
                                         issued_role=staff_member.role, action="answer.rate",
                                         ttl_seconds=60, channel="panel")
        else:
            await legal_message._deliver(chat_id, AgentReply(text="resposta"), text,
                                         conn=conn, staff=staff_member)

    monkeypatch.setattr(legal_message, "_serve_message", serve_message)
    legal_message._last_deliveries.pop(str(RUI.chat_id), None)

    async def body(conn):
        intake = await panel_intake.submit_message(conn, RUI, "bom dia")
        assert intake.outcome == "queued"
        update = world.queued[-1]
        world.answer.reached, world.answer.gate = asyncio.Event(), asyncio.Event()
        eraser = await db.connect()
        try:
            serving = asyncio.create_task(legal_agent._handle_update(update))
            await asyncio.wait_for(world.answer.reached.wait(), timeout=10)
            await staff.erase_person(eraser, actor_id=PAULO.id, target_id=RUI.id)
            world.answer.gate.set()
            await asyncio.wait_for(serving, timeout=10)
        finally:
            await eraser.close()
        return (await _turns(conn, RUI),
                await _rows(conn, "SELECT (SELECT count(*) FROM pending_actions "
                                  "WHERE chat_id = %s), (SELECT count(*) FROM "
                                  "callback_actions WHERE chat_id = %s)",
                            (RUI.chat_id, RUI.chat_id)))

    try:
        assert db.run(body) == ([], [(0, 0)])
        assert str(RUI.chat_id) not in legal_message._last_deliveries
    finally:
        legal_message._last_deliveries.pop(str(RUI.chat_id), None)


# ── a document, archived as on Telegram (Task M.6) ──────────────────────────

ANA = StaffMember(id=5, chat_id="500", name="Ana", role="estagiario")
CONTRACT = "Contrato de locação residencial.\nAluguel de R$ 2.000,00, reajuste pelo IGP-M."
_RANK_REFUSAL = authorization.decide(ANA, "document.ingest").reason


@pytest.fixture
def archive(monkeypatch):
    """Originals go to a bucket kept in a dict; embeddings are counted and can
    be paused or made to do something while they run."""
    monkeypatch.setattr(object_store, "configured", lambda: True)
    objects: dict[str, bytes] = {}
    embedding = SimpleNamespace(calls=0, reached=None, gate=None, during=None, result=True)
    invalidated: list[bool] = []

    async def put(key, data):
        objects[key] = data

    async def get(key):
        return objects[key]

    async def remove(key):
        objects.pop(key, None)

    async def embeddings(texts):
        embedding.calls += 1
        if embedding.during is not None:
            await embedding.during()
        if embedding.reached is not None:
            embedding.reached.set()
        if embedding.gate is not None:
            await embedding.gate.wait()
        return [[0.1] * settings.embed_dim for _ in texts] if embedding.result else None

    async def invalidate_answers():
        invalidated.append(True)
        return 0

    monkeypatch.setattr(object_store, "put_object", put)
    monkeypatch.setattr(object_store, "get_object", get)
    monkeypatch.setattr(object_store, "delete_object", remove)
    monkeypatch.setattr(ingestion, "embed_texts", embeddings)
    monkeypatch.setattr(legal_message.cache, "invalidate_answers", invalidate_answers)
    monkeypatch.setattr(ingestion.cache, "invalidate_answers", invalidate_answers)
    return SimpleNamespace(objects=objects, embedding=embedding, invalidated=invalidated)


async def _send_document(conn, world, who: StaffMember, caption: str = "", *,
                         name: str = "contrato.txt", data: bytes | None = None) -> dict:
    """A document as the upload route leaves it once every check passed."""
    upload_id = await panel_uploads.store_upload(
        conn, who.id, kind="document", media_type="text/plain",
        data=CONTRACT.encode() if data is None else data, archive=False, filename=name)
    assert upload_id is not None
    intake = await panel_intake.submit_message(conn, who, caption, upload_id=upload_id)
    assert intake.outcome == "queued", intake
    return world.queued[-1]


async def _documents(conn):
    return await _rows(conn, "SELECT id, uploaded_by, visibility, title, doc_type, matter, "
                             "full_text, source_filename FROM documents ORDER BY id")


async def _demote(db, who: StaffMember = RUI) -> None:
    other = await db.connect()
    try:
        await _execute(other, "UPDATE staff SET role = 'estagiario' WHERE id = %s", (who.id,))
    finally:
        await other.close()


def test_a_lawyers_document_is_archived_as_on_telegram(db, world, archive):
    async def body(conn):
        update = await _send_document(conn, world, RUI, "tipo: contrato; caso: Acme")
        await legal_agent._handle_update(update)
        stored = await _rows(conn, "SELECT source_storage_key, source_wrapped_key, "
                                   "source_sha256 FROM documents")
        originals = [await document_storage.read(key, wrapped, raw_sha256=digest)
                     for key, wrapped, digest in stored]
        audit = await _rows(conn, "SELECT action, staff_id FROM document_access_log")
        return (await _turns(conn, RUI), await _documents(conn), originals, audit,
                await _uploads(conn), await _status(conn, update))

    turns, documents, originals, audit, uploads, status = db.run(body)

    assert turns == [
        ("person", "tipo: contrato; caso: Acme",
         {"attachment": {"kind": "document", "size_bytes": len(CONTRACT.encode()),
                         "name": "contrato.txt"}}),
        ("bot", "📄 Recebi 'contrato.txt'. Iniciando o processamento.", None),
        ("bot", "Concluído! O documento 'contrato.txt' foi processado e integrado à base de "
                "conhecimento. Você já pode fazer perguntas sobre ele.", None),
    ]
    [(_id, owner, visibility, title, doc_type, matter, text, source_name)] = documents
    assert (owner, visibility, title, doc_type, matter, text, source_name) == (
        RUI.id, "private", "contrato.txt", "contrato", "Acme", CONTRACT, "contrato.txt")
    assert originals == [CONTRACT.encode()] and audit == [("ingest", RUI.id)]
    assert uploads == 0 and status == "done"
    assert world.questions == [], "a document is archived, not answered"
    assert archive.invalidated == [True] and world.telegram == []


def test_a_document_already_in_the_archive_is_said_and_not_kept_twice(db, world, archive):
    async def body(conn):
        first = await ingestion.ingest_bytes(conn, CONTRACT.encode(), filename="antigo.txt",
                                             uploaded_by=PAULO.id)
        await legal_agent._handle_update(await _send_document(conn, world, RUI))
        return first, await _bot_texts(conn, RUI), await _documents(conn), await _uploads(conn)

    (first_id, _chunks), texts, documents, uploads = db.run(body)

    assert texts[-1] == "O documento 'contrato.txt' já fazia parte da base de conhecimento."
    assert [row[0] for row in documents] == [first_id] and uploads == 0
    assert len(archive.objects) == 1, "the second copy's original was taken back"


def test_a_rank_lost_before_the_worker_ingests_nothing_and_deletes_the_upload(
        db, world, archive, unsealed):
    async def body(conn):
        update = await _send_document(conn, world, RUI)
        await _demote(db)
        await legal_agent._handle_update(update)
        return await _bot_texts(conn, RUI), await _documents(conn), await _uploads(conn)

    texts, documents, uploads = db.run(body)

    assert texts == [_RANK_REFUSAL], "Telegram's refusal, and nothing else"
    assert documents == [] and uploads == 0
    assert archive.embedding.calls == 0 and archive.objects == {}, "nothing paid for"
    assert unsealed == [], "nothing decrypted for a caller who may not archive"


def test_a_rank_lost_while_the_document_is_prepared_is_refused_under_the_guard(
        db, world, archive):
    async def demote():
        await _demote(db)

    archive.embedding.during = demote

    async def body(conn):
        await legal_agent._handle_update(await _send_document(conn, world, RUI))
        return await _bot_texts(conn, RUI), await _documents(conn), await _uploads(conn)

    texts, documents, uploads = db.run(body)

    assert archive.embedding.calls == 1, "the preparation ran"
    assert texts == ["📄 Recebi 'contrato.txt'. Iniciando o processamento.", _RANK_REFUSAL]
    assert documents == [] and uploads == 0
    assert archive.objects == {}, "the stored original was taken back"


def test_an_erasure_while_the_document_is_prepared_does_not_wait_and_ingests_nothing(
        db, world, archive):
    archive.embedding.reached, archive.embedding.gate = asyncio.Event(), asyncio.Event()

    async def body(conn):
        update = await _send_document(conn, world, RUI)
        eraser = await db.connect()
        try:
            serving = asyncio.create_task(legal_agent._handle_update(update))
            await asyncio.wait_for(archive.embedding.reached.wait(), timeout=10)
            erased = await asyncio.wait_for(
                staff.erase_person(eraser, actor_id=PAULO.id, target_id=RUI.id), timeout=5)
            archive.embedding.gate.set()
            await asyncio.wait_for(serving, timeout=10)
        finally:
            archive.embedding.gate.set()
            await eraser.close()
        return (erased.status, await _turns(conn, RUI), await _documents(conn),
                await _uploads(conn))

    outcome, turns, documents, uploads = db.run(body)

    assert outcome == "erased", "the erasure did not wait for the embeddings"
    assert turns == [] and documents == [] and uploads == 0 and archive.objects == {}


def test_a_document_that_cannot_be_read_is_said_in_telegrams_words(db, world, archive):
    async def body(conn):
        await legal_agent._handle_update(
            await _send_document(conn, world, RUI, name="vazio.txt", data=b"  \n  "))
        return await _bot_texts(conn, RUI), await _documents(conn), await _uploads(conn)

    texts, documents, uploads = db.run(body)

    assert texts == ["📄 Recebi 'vazio.txt'. Iniciando o processamento.",
                     "Não consegui processar 'vazio.txt'. Verifique o formato e os limites "
                     "do arquivo."]
    assert documents == [] and uploads == 0 and archive.objects == {}


def test_a_document_past_its_deadline_is_said_and_leaves_nothing(
        db, world, archive, monkeypatch):
    monkeypatch.setattr(settings, "panel_media_document_seconds", 1.0)
    archive.embedding.gate = asyncio.Event()  # never opened

    async def body(conn):
        update = await _send_document(conn, world, RUI)
        started = asyncio.get_running_loop().time()
        await asyncio.wait_for(legal_agent._handle_update(update), timeout=15)
        took = asyncio.get_running_loop().time() - started
        return (took, await _bot_texts(conn, RUI), await _documents(conn),
                await _uploads(conn), await _status(conn, update))

    took, texts, documents, uploads, status = db.run(body)

    assert took < 5, "the preparation was not cut at the stage's deadline"
    assert texts == ["📄 Recebi 'contrato.txt'. Iniciando o processamento.", media.UNREADABLE]
    assert documents == [] and uploads == 0 and archive.objects == {} and status == "done"


def test_an_erasure_during_a_preparation_that_fails_leaves_no_word_behind(
        db, world, archive):
    """The failure is said through the guard too: the person erased while the
    embeddings ran, and the embeddings then failing, nothing is written."""
    archive.embedding.reached, archive.embedding.gate = asyncio.Event(), asyncio.Event()
    archive.embedding.result = False

    async def body(conn):
        update = await _send_document(conn, world, RUI)
        eraser = await db.connect()
        try:
            serving = asyncio.create_task(legal_agent._handle_update(update))
            await asyncio.wait_for(archive.embedding.reached.wait(), timeout=10)
            erased = await asyncio.wait_for(
                staff.erase_person(eraser, actor_id=PAULO.id, target_id=RUI.id), timeout=5)
            archive.embedding.gate.set()
            await asyncio.wait_for(serving, timeout=10)
        finally:
            archive.embedding.gate.set()
            await eraser.close()
        return erased.status, await _turns(conn, RUI), await _uploads(conn)

    assert db.run(body) == ("erased", [], 0)


def test_a_document_upload_without_its_name_is_refused_by_the_table(db):
    """The table itself holds the rule (`panel_uploads_document_named`), on a
    database built by init.sql and by the startup path alike."""
    async def body(conn):
        with pytest.raises(psycopg.errors.CheckViolation):
            await _execute(conn, "INSERT INTO panel_uploads (id, staff_id, kind, media_type, "
                                 "size_bytes, sealed, wrapped_key, content_sha, expires_at) "
                                 "VALUES (gen_random_uuid(), 4, 'document', 'text/plain', 1, "
                                 "'x', 'x', 'x', NOW() + interval '1 hour')")
        await conn.rollback()
        with pytest.raises(psycopg.errors.CheckViolation):
            await _execute(conn, "INSERT INTO panel_uploads (id, staff_id, kind, media_type, "
                                 "size_bytes, sealed, wrapped_key, content_sha, filename, "
                                 "expires_at) VALUES (gen_random_uuid(), 4, 'photo', "
                                 "'image/png', 1, 'x', 'x', 'x', 'foto.png', "
                                 "NOW() + interval '1 hour')")
        await conn.rollback()

    db.run(body)


def test_a_commit_failing_after_the_preparation_takes_the_original_back(
        db, world, archive, monkeypatch):
    """The original is stored before the database decides; a failure between
    the two (here, the commit right after the preparation) must not leave it
    with no document to own it."""
    prepare = ingestion.prepare_document
    armed = []

    async def prepared_then_fail(*args, **kwargs):
        result = await prepare(*args, **kwargs)
        armed.append(True)
        return result

    commit = psycopg.AsyncConnection.commit

    async def failing_commit(self):
        if armed:
            armed.clear()
            raise psycopg.OperationalError("the connection dropped")
        return await commit(self)

    monkeypatch.setattr(ingestion, "prepare_document", prepared_then_fail)
    monkeypatch.setattr(psycopg.AsyncConnection, "commit", failing_commit)

    async def body(conn):
        await legal_agent._handle_update(await _send_document(conn, world, RUI))
        return await _bot_texts(conn, RUI), await _documents(conn), await _uploads(conn)

    texts, documents, uploads = db.run(body)

    assert archive.embedding.calls == 1, "the preparation ran and stored the original"
    assert texts[-1] == media.UNREADABLE
    assert documents == [] and uploads == 0
    assert archive.objects == {}, "the stored original was taken back"


# ── the limit, and an attachment that is gone ────────────────────────────────

_RATE_LIMITED = "⚠️ Muitas solicitações em sequência. Aguarde um instante."
_NO_UPLOAD = "O anexo não está mais disponível. Anexe de novo e envie."


async def _send_kind(conn, world, kind: str, text: str) -> dict:
    if kind == "document":
        return await _send_document(conn, world, RUI, text)
    if kind == "photo":
        return await _send_with(conn, world, RUI, text)
    return await _send_with(conn, world, RUI, text, kind=kind,  # type: ignore[arg-type]
                            data=b"OggS " + kind.encode(), media_type="audio/ogg")


@pytest.mark.parametrize("kind", ["photo", "voice", "audio", "document"])
def test_a_rate_limited_attachment_is_refused_before_it_is_unsealed_or_paid_for(
        db, world, reading, transcribed, archive, unsealed, kind):
    world.limits.answer = (False, True)

    async def body(conn):
        update = await _send_kind(conn, world, kind, "tipo: contrato; o que diz?")
        await legal_agent._handle_update(update)
        return (await _bot_texts(conn, RUI), await _uploads(conn),
                await _status(conn, update), await _documents(conn))

    texts, uploads, status, documents = db.run(body)

    assert texts == [_RATE_LIMITED], "the text path's message, and nothing else"
    assert uploads == 0 and status == "done" and documents == []
    assert unsealed == [], "refused before the upload was decrypted"
    assert reading.calls == [] and transcribed.calls == []
    assert archive.embedding.calls == 0 and archive.objects == {}, "nothing paid for"
    assert world.questions == [] and world.limits.local == 1


@pytest.mark.parametrize("kind", ["photo", "document"])
def test_every_rate_limited_panel_message_is_told_not_only_the_first(
        db, world, archive, unsealed, kind):
    """Telegram says it once per window; the panel waits on each request, so a
    later refusal said nothing would leave a message with no reply."""
    world.limits.answer = (False, False)

    async def body(conn):
        update = await _send_kind(conn, world, kind, "o que diz?")
        await legal_agent._handle_update(update)
        return await _bot_texts(conn, RUI), await _uploads(conn), await _status(conn, update)

    texts, uploads, status = db.run(body)

    assert texts == [_RATE_LIMITED]
    assert uploads == 0 and status == "done" and unsealed == []


@pytest.mark.parametrize("kind", ["photo", "document"])
def test_an_attachment_is_counted_once_by_the_limit(db, world, reading, archive, kind):
    async def body(conn):
        await legal_agent._handle_update(await _send_kind(conn, world, kind, "o que diz?"))
        return await _status(conn, world.queued[-1])

    assert db.run(body) == "done"
    assert (world.limits.local, world.limits.shared) == (1, 1)


async def _make_unavailable(conn, update: dict, how: str) -> None:
    request_id = update["panel_request"]
    if how == "purged":
        await _execute(conn, "DELETE FROM panel_uploads WHERE request_id = %s", (request_id,))
    elif how == "past its day":
        await _execute(conn, "UPDATE panel_uploads SET created_at = NOW() - interval '25 hours' "
                             "WHERE request_id = %s", (request_id,))
    else:
        await _execute(conn, "UPDATE panel_uploads SET content_sha = repeat('0', 64) "
                             "WHERE request_id = %s", (request_id,))


@pytest.mark.parametrize("how", ["purged", "past its day", "seal does not open"])
@pytest.mark.parametrize("kind", ["photo", "document"])
def test_an_attachment_no_longer_there_is_asked_for_again_never_answered_as_text(
        db, world, reading, archive, kind, how):
    async def body(conn):
        # An earlier photo, read and answered: nothing of it may answer the
        # message whose own attachment is gone.
        await legal_agent._handle_update(
            await _send_with(conn, world, RUI, "qual o valor do aluguel?"))
        update = await _send_kind(conn, world, kind, "tipo: contrato; e o vencimento?")
        await _make_unavailable(conn, update, how)
        await legal_agent._handle_update(update)
        return (await _bot_texts(conn, RUI), await _uploads(conn),
                await _status(conn, update), await _documents(conn))

    texts, uploads, status, documents = db.run(body)

    assert texts[-1] == _NO_UPLOAD and texts[-2] == "resposta"
    assert world.questions == ["qual o valor do aluguel?"], "no answer without its attachment"
    assert len(reading.calls) == 1 and archive.embedding.calls == 0 and documents == []
    assert uploads == 0 and status == "done"
