"""An attachment sent in the panel's chat, sealed until read, on a real Postgres.

Who may hold one, how much, for how long, and that erasure takes every one of
them are properties of the SQL (a row lock, a live sum, a delete), so they are
tested on the real thing. So is the seam with the intake: the upload is bound
to its request before the queue is handed the request, never after.
"""
import asyncio
import contextlib
from datetime import datetime
from typing import LiteralString
from uuid import UUID, uuid4

import pytest

from core import document_crypto
from core.config import settings
from core.models import StaffMember
from core.repositories import panel, panel_uploads, staff
from core.services import maintenance, panel_intake, work_hours

PAULO, RUI, ANA = 1, 4, 5
RUI_STAFF = StaffMember(id=4, chat_id="400", name="Rui", role="advogado")
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60


@pytest.fixture(autouse=True)
def master_key(monkeypatch):
    monkeypatch.setattr(settings, "doc_master_key", document_crypto.new_master_key())


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


async def _store(conn, who: int = RUI, data: bytes = JPEG, *,
                 kind: panel_uploads.UploadKind = "photo", archive=False) -> UUID:
    upload_id = await panel_uploads.store_upload(
        conn, who, kind=kind, media_type="image/jpeg", data=data, archive=archive)
    assert upload_id is not None
    return upload_id


async def _request(conn, who: int = RUI, chat: str = "400") -> UUID:
    created = await panel.create_request(conn, staff_id=who, chat_id=chat, kind="message",
                                         text="o que diz?")
    assert created is not None
    return created[0]


async def _ids(conn) -> set[UUID]:
    return {row[0] for row in await _rows(conn, "SELECT id FROM panel_uploads")}


# ── storing ─────────────────────────────────────────────────────────────────


def test_an_upload_is_sealed_at_rest_and_read_back_whole(db):
    data = JPEG + b"segredo do cliente"

    async def body(conn):
        upload_id = await _store(conn, data=data, archive=True)
        stored = await _rows(conn, "SELECT staff_id, kind, media_type, size_bytes, archive, "
                                   "request_id, sealed, content_sha, "
                                   "expires_at - created_at FROM panel_uploads")
        request_id = await _request(conn)
        assert await panel_uploads.attach_upload(conn, upload_id, staff_id=RUI,
                                                 request_id=request_id)
        taken = await panel_uploads.take_upload(conn, request_id)
        return upload_id, stored, taken

    upload_id, stored, taken = db.run(body)

    (who, kind, media_type, size, archive, request_id, sealed, sha, lifetime), = stored
    assert (who, kind, media_type, size, archive, request_id) == (
        RUI, "photo", "image/jpeg", len(data), True, None)
    assert b"segredo" not in bytes(sealed) and len(sha) == 64
    assert lifetime.total_seconds() == 15 * 60
    assert taken is not None
    assert (taken.id, taken.kind, taken.media_type, taken.archive, taken.data) == (
        upload_id, "photo", "image/jpeg", True, data)


def test_the_live_cap_is_per_person(db, monkeypatch):
    monkeypatch.setattr(settings, "panel_uploads_max_live_bytes", 2 * len(JPEG))

    async def body(conn):
        first = await panel_uploads.store_upload(conn, RUI, kind="photo",
                                                 media_type="image/jpeg", data=JPEG,
                                                 archive=False)
        second = await panel_uploads.store_upload(conn, RUI, kind="photo",
                                                  media_type="image/jpeg", data=JPEG,
                                                  archive=False)
        over = await panel_uploads.store_upload(conn, RUI, kind="photo",
                                                media_type="image/jpeg", data=JPEG,
                                                archive=False)
        someone_else = await panel_uploads.store_upload(conn, ANA, kind="photo",
                                                        media_type="image/jpeg", data=JPEG,
                                                        archive=False)
        # An expired one no longer counts against the person.
        await _execute(conn, "UPDATE panel_uploads SET expires_at = NOW() - interval '1 s' "
                             "WHERE id = %s", (first,))
        after_expiry = await panel_uploads.store_upload(conn, RUI, kind="photo",
                                                        media_type="image/jpeg", data=JPEG,
                                                        archive=False)
        return first, second, over, someone_else, after_expiry

    first, second, over, someone_else, after_expiry = db.run(body)

    assert first and second and someone_else and after_expiry
    assert over is None


def test_concurrent_uploads_cannot_both_fit_under_the_cap(db, monkeypatch):
    monkeypatch.setattr(settings, "panel_uploads_max_live_bytes", len(JPEG))

    async def body(conn):
        others = [await db.connect() for _ in range(4)]
        try:
            results = await asyncio.gather(*(
                panel_uploads.store_upload(other, RUI, kind="photo", media_type="image/jpeg",
                                           data=JPEG, archive=False)
                for other in others))
        finally:
            for other in others:
                await other.close()
        return results

    results = db.run(body)

    assert sum(1 for r in results if r is not None) == 1


def test_an_inactive_person_cannot_store(db):
    async def body(conn):
        await _execute(conn, "UPDATE staff SET active = FALSE WHERE id = %s", (RUI,))
        refused = await panel_uploads.store_upload(conn, RUI, kind="photo",
                                                   media_type="image/jpeg", data=JPEG,
                                                   archive=False)
        return refused, await _ids(conn)

    assert db.run(body) == (None, set())


class _CommitHeld:
    """A connection whose commit waits for the test: erasure has done all its
    work and holds the staff row until `release` is set."""

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


async def _waiting_on_a_lock(conn, pid: int) -> bool:
    rows = await _rows(conn, "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                       (pid,))
    return bool(rows) and rows[0][0] == "Lock"


def test_an_upload_finishing_during_an_erasure_is_refused(db):
    """The upload waits on the staff row the erasure holds, then finds the
    person gone. Without the lock it would insert under the erasure's
    delete and outlive it."""
    async def body(conn):
        eraser, uploader = await db.connect(), await db.connect()
        try:
            held = _CommitHeld(eraser)
            erasure = asyncio.create_task(
                staff.erase_person(held, actor_id=PAULO, target_id=RUI))
            await asyncio.wait_for(held.reached.wait(), timeout=5)
            upload = asyncio.create_task(panel_uploads.store_upload(
                uploader, RUI, kind="photo", media_type="image/jpeg", data=JPEG,
                archive=False))
            blocked = False
            for _ in range(100):
                if upload.done():
                    break
                if await _waiting_on_a_lock(conn, uploader.info.backend_pid):
                    blocked = True
                    break
                await asyncio.sleep(0.02)
            held.release.set()
            await erasure
            stored = await upload
        finally:
            await eraser.close()
            await uploader.close()
        return blocked, stored, await _ids(conn)

    blocked, stored, left = db.run(body)

    assert blocked, "the upload did not wait for the erasure's lock on the staff row"
    assert stored is None and left == set()


def test_erasure_deletes_every_upload_of_the_person(db):
    async def body(conn):
        unattached = await _store(conn)
        attached = await _store(conn)
        request_id = await _request(conn)
        assert await panel_uploads.attach_upload(conn, attached, staff_id=RUI,
                                                 request_id=request_id)
        kept = await _store(conn, ANA)
        await staff.erase_person(conn, actor_id=PAULO, target_id=RUI)
        return unattached, attached, kept, await _ids(conn)

    _unattached, _attached, kept, left = db.run(body)

    assert left == {kept}


# ── attaching ───────────────────────────────────────────────────────────────


def test_only_its_owner_attaches_an_unattached_unexpired_upload(db):
    async def body(conn):
        mine = await _store(conn)
        expired = await _store(conn)
        await _execute(conn, "UPDATE panel_uploads SET expires_at = NOW() - interval '1 s' "
                             "WHERE id = %s", (expired,))
        first = await _request(conn)
        await panel.finish(conn, first, "done")
        second = await _request(conn)
        anas = await _request(conn, ANA, "500")
        return {
            "someone else": await panel_uploads.attach_upload(
                conn, mine, staff_id=ANA, request_id=anas),
            "expired": await panel_uploads.attach_upload(
                conn, expired, staff_id=RUI, request_id=second),
            "unknown": await panel_uploads.attach_upload(
                conn, uuid4(), staff_id=RUI, request_id=second),
            "owner": await panel_uploads.attach_upload(
                conn, mine, staff_id=RUI, request_id=first),
            "again": await panel_uploads.attach_upload(
                conn, mine, staff_id=RUI, request_id=second),
            "lifetime": await _rows(conn, "SELECT expires_at - created_at FROM panel_uploads "
                                          "WHERE id = %s", (mine,)),
        }

    found = db.run(body)

    assert found["someone else"] is False and found["expired"] is False
    assert found["unknown"] is False and found["again"] is False
    assert found["owner"] is True
    assert found["lifetime"][0][0].total_seconds() == 24 * 3600


# ── purging ─────────────────────────────────────────────────────────────────


def test_purge_follows_the_upload_and_its_request(db):
    async def body(conn):
        fresh = await _store(conn)
        stale = await _store(conn)
        await _execute(conn, "UPDATE panel_uploads SET created_at = NOW() - interval '16 min', "
                             "expires_at = NOW() - interval '1 min' WHERE id = %s", (stale,))

        ended = await _store(conn, ANA)
        done = await _request(conn, ANA, "500")
        assert await panel_uploads.attach_upload(conn, ended, staff_id=ANA, request_id=done)
        await panel.finish(conn, done, "done")

        # A message deferred to the morning keeps its attachment past the
        # unattached 15 minutes, up to 24 hours.
        deferred = await _store(conn, PAULO)
        night = await _request(conn, PAULO, "100")
        assert await panel_uploads.attach_upload(conn, deferred, staff_id=PAULO,
                                                 request_id=night)
        await panel.mark_handed(conn, night, deferred=True)
        await _execute(conn, "UPDATE panel_uploads SET created_at = NOW() - interval '10 h' "
                             "WHERE id = %s", (deferred,))
        purged = await panel_uploads.purge_uploads(conn)
        after_night = await _ids(conn)

        await _execute(conn, "UPDATE panel_uploads SET created_at = NOW() - interval '25 h' "
                             "WHERE id = %s", (deferred,))
        late = await panel_uploads.purge_uploads(conn)
        return fresh, deferred, purged, after_night, late, await _ids(conn)

    fresh, deferred, purged, after_night, late, left = db.run(body)

    assert purged == 2 and after_night == {fresh, deferred}
    assert late == 1 and left == {fresh}


def test_an_attached_upload_older_than_a_day_is_not_taken(db):
    async def body(conn):
        upload_id = await _store(conn)
        request_id = await _request(conn)
        assert await panel_uploads.attach_upload(conn, upload_id, staff_id=RUI,
                                                 request_id=request_id)
        await _execute(conn, "UPDATE panel_uploads SET created_at = NOW() - interval '25 h'")
        return await panel_uploads.take_upload(conn, request_id)

    assert db.run(body) is None


def test_delete_upload_removes_it_and_a_withdrawn_request_takes_its_own(db):
    async def body(conn):
        upload_id = await _store(conn)
        await panel_uploads.delete_upload(conn, upload_id)
        gone = await _ids(conn)
        attached = await _store(conn)
        request_id = await _request(conn)
        assert await panel_uploads.attach_upload(conn, attached, staff_id=RUI,
                                                 request_id=request_id)
        await panel.mark_handed(conn, request_id, deferred=True)
        assert await panel.withdraw(conn, request_id, staff_id=RUI)
        purged = await panel_uploads.purge_uploads(conn)
        return gone, purged, await _ids(conn)

    assert db.run(body) == (set(), 1, set())


# ── the intake binds it before the hand-over ────────────────────────────────


@pytest.fixture
def daytime(monkeypatch):
    monkeypatch.setattr(work_hours, "is_office_hours", lambda: True)


def test_the_upload_is_bound_before_the_queue_sees_the_request(db, monkeypatch, daytime):
    seen: list = []

    async def body(conn):
        upload_id = await _store(conn)

        async def enqueue(update, *, deferred):
            # What the worker would find the moment it took the request.
            other = await db.connect()
            try:
                taken = await panel_uploads.take_upload(other, UUID(update["panel_request"]))
            finally:
                await other.close()
            seen.append(taken)
            return True

        monkeypatch.setattr(panel_intake.update_queue, "enqueue_update", enqueue)
        intake = await panel_intake.submit_message(conn, RUI_STAFF, "", upload_id=upload_id)
        return upload_id, intake

    upload_id, intake = db.run(body)

    assert intake.outcome == "queued"
    assert seen and seen[0] is not None and seen[0].id == upload_id


@pytest.mark.parametrize("whose", ["someone else", "expired", "attached", "unknown"])
def test_a_message_with_an_unusable_upload_writes_nothing(db, monkeypatch, daytime, whose):
    queued: list = []

    async def enqueue(update, *, deferred):
        queued.append(update)
        return True

    monkeypatch.setattr(panel_intake.update_queue, "enqueue_update", enqueue)

    async def body(conn):
        upload_id = await _store(conn, ANA if whose == "someone else" else RUI)
        if whose == "expired":
            await _execute(conn, "UPDATE panel_uploads SET expires_at = NOW() - "
                                 "interval '1 s'")
        if whose == "attached":
            earlier = await _request(conn)
            assert await panel_uploads.attach_upload(conn, upload_id, staff_id=RUI,
                                                     request_id=earlier)
            await panel.finish(conn, earlier, "done")
        if whose == "unknown":
            upload_id = uuid4()
        before = await _rows(conn, "SELECT count(*) FROM panel_requests")
        intake = await panel_intake.submit_message(conn, RUI_STAFF, "o que é?",
                                                   upload_id=upload_id)
        after = await _rows(conn, "SELECT count(*) FROM panel_requests")
        turns = await _rows(conn, "SELECT count(*) FROM conversation_turns "
                                  "WHERE text = 'o que é?'")
        return intake, before, after, turns

    intake, before, after, turns = db.run(body)

    assert (intake.outcome, intake.reason) == ("refused", "no_upload")
    assert before == after and turns == [(0,)] and queued == []


def test_empty_text_is_taken_only_with_an_upload(db, monkeypatch, daytime):
    monkeypatch.setattr(panel_intake.update_queue, "enqueue_update",
                        lambda update, *, deferred: _true())

    async def body(conn):
        alone = await panel_intake.submit_message(conn, RUI_STAFF, "  ")
        upload_id = await _store(conn)
        with_upload = await panel_intake.submit_message(conn, RUI_STAFF, "",
                                                        upload_id=upload_id)
        return alone, with_upload

    alone, with_upload = db.run(body)

    assert (alone.outcome, alone.reason) == ("refused", "empty")
    assert with_upload.outcome == "queued"


async def _true():
    return True


# ── the sweep ───────────────────────────────────────────────────────────────


def test_the_maintenance_worker_sweeps_uploads_on_every_check(monkeypatch):
    """Every quarter-hour wake, outside the daily pass's hour too, and a
    failed sweep does not stop the next one."""
    calls: list = []
    stop = asyncio.Event()

    async def purge(conn):
        calls.append(conn)
        if len(calls) == 1:
            raise RuntimeError("database away")
        if len(calls) == 3:
            stop.set()
        return 1

    class _Pool:
        def connection(self):
            @contextlib.asynccontextmanager
            async def opened():
                yield "conn"
            return opened()

    async def pool():
        return _Pool()

    async def no_daily_pass():
        raise AssertionError("the daily pass ran outside its hour")

    monkeypatch.setattr(maintenance, "get_pool", pool)
    monkeypatch.setattr(maintenance, "purge_uploads", purge)
    monkeypatch.setattr(maintenance, "run_retention_pass", no_daily_pass)
    monkeypatch.setattr(maintenance, "_CHECK_INTERVAL", 0.01)
    monkeypatch.setattr(maintenance, "local_now",
                        lambda: datetime(2026, 10, 27, (settings.retention_hour + 6) % 24))

    asyncio.run(asyncio.wait_for(maintenance.run_maintenance_worker(stop), timeout=5))

    assert calls == ["conn", "conn", "conn"]
