"""The panel's requests and files, against a real Postgres.

A panel message is a row before it is a queue entry: it runs at most once,
one at a time per person, and the page polls it. A file the panel offers is
sealed with the documents' key, read only by its owner, and gone when it
expires, in the query itself. Each of these is a property of the SQL, so it
is tested on the real thing.
"""
import asyncio
from typing import LiteralString

import pytest
from psycopg import sql

from core import document_crypto
from core.config import settings
from core.repositories import conversation, panel, pending_actions, staff

PAULO, RUI, ANA = 1, 4, 5


async def _rows(conn, statement: LiteralString, params=()):
    async with conn.cursor() as cur:
        await cur.execute(statement, params)
        return await cur.fetchall()


async def _status(conn, request_id) -> str:
    rows = await _rows(conn, "SELECT status FROM panel_requests WHERE id = %s", (request_id,))
    return rows[0][0]


async def _age(conn, request_id, *, column: LiteralString, seconds: int) -> None:
    async with conn.cursor() as cur:
        await cur.execute(sql.SQL(
            "UPDATE panel_requests SET {column} = NOW() - make_interval(secs => %s) "
            "WHERE id = %s").format(column=sql.Identifier(column)), (seconds, request_id))
    await conn.commit()


@pytest.fixture
def master_key(monkeypatch):
    monkeypatch.setattr(settings, "doc_master_key", document_crypto.new_master_key())


def test_a_request_and_its_turn_are_written_together(db):
    async def body(conn):
        created = await panel.create_request(
            conn, staff_id=RUI, chat_id="400", kind="message",
            text="Prazo? veja https://x.com.br/web/login/abc123")
        assert created is not None
        request_id, turn_id = created
        turns = await _rows(conn, "SELECT id, speaker, text, channel, request_id "
                                  "FROM conversation_turns")
        requests = await _rows(conn, "SELECT staff_id, chat_id, kind, status, person_turn_id "
                                     "FROM panel_requests")
        pressed = await panel.create_request(conn, staff_id=ANA, chat_id="500",
                                             kind="button", text=None)
        return request_id, turn_id, turns, requests, pressed

    request_id, turn_id, turns, requests, pressed = db.run(body)

    assert turns == [(turn_id, "person", "Prazo? veja [link de acesso]", "panel", request_id)]
    assert requests == [(RUI, "400", "message", "sending", turn_id)]
    assert pressed is not None and pressed[1] == 0


def test_handing_over_only_moves_a_sending_request(db):
    async def body(conn):
        first = await panel.create_request(conn, staff_id=RUI, chat_id="400",
                                           kind="message", text="a")
        assert first
        handed = await panel.mark_handed(conn, first[0], deferred=True)
        again = await panel.mark_handed(conn, first[0], deferred=False)
        status_after_handover = await _status(conn, first[0])
        second = await panel.create_request(conn, staff_id=ANA, chat_id="500",
                                            kind="message", text="b")
        assert second
        assert await panel.start(conn, second[0])
        late = await panel.mark_handed(conn, second[0], deferred=False)
        return handed, again, status_after_handover, late, await _status(conn, second[0])

    handed, again, status_after_handover, late, running = db.run(body)

    assert (handed, again, status_after_handover) == (True, False, "deferred")
    assert late is False and running == "running", "the endpoint never overwrites the worker"


def test_a_request_starts_at_most_once(db):
    async def body(conn):
        outcomes = []
        for ending in ("done", "failed", "withdrawn"):
            created = await panel.create_request(conn, staff_id=RUI, chat_id="400",
                                                 kind="message", text=ending)
            assert created
            request_id = created[0]
            await panel.mark_handed(conn, request_id, deferred=True)
            if ending == "withdrawn":
                assert await panel.withdraw(conn, request_id, staff_id=RUI)
                outcomes.append(await panel.start(conn, request_id))
                continue
            first = await panel.start(conn, request_id)
            second = await panel.start(conn, request_id)
            await panel.finish(conn, request_id, ending)
            outcomes.append((first, second, await panel.start(conn, request_id)))
        return outcomes

    done, failed, withdrawn = db.run(body)

    assert done == (True, False, False)
    assert failed == (True, False, False)
    assert withdrawn is False


def test_the_worker_can_start_a_request_still_marked_sending(db):
    """Redis can hand the payload over before the endpoint writes queued."""
    async def body(conn):
        created = await panel.create_request(conn, staff_id=RUI, chat_id="400",
                                             kind="message", text="a")
        assert created
        started = await panel.start(conn, created[0])
        rows = await _rows(conn, "SELECT status, started_at IS NOT NULL FROM panel_requests")
        return started, rows

    started, rows = db.run(body)

    assert started and rows == [("running", True)]


def test_finishing_never_revives_a_request_that_ended(db):
    async def body(conn):
        created = await panel.create_request(conn, staff_id=RUI, chat_id="400",
                                             kind="message", text="a")
        assert created
        await panel.start(conn, created[0])
        await panel.finish(conn, created[0], "failed")
        await panel.finish(conn, created[0], "done")
        return await _status(conn, created[0])

    assert db.run(body) == "failed"


def test_stale_sending_and_running_requests_settle_as_failed(db, monkeypatch):
    monkeypatch.setattr(settings, "panel_sending_grace_seconds", 60)
    monkeypatch.setattr(settings, "panel_running_grace_seconds", 420)

    async def body(conn):
        statuses = {}
        for name, staff_id, chat in (("sending", RUI, "400"), ("running", ANA, "500")):
            created = await panel.create_request(conn, staff_id=staff_id, chat_id=chat,
                                                 kind="message", text=name)
            assert created
            if name == "running":
                await panel.start(conn, created[0])
                await _age(conn, created[0], column="started_at", seconds=421)
            else:
                await _age(conn, created[0], column="updated_at", seconds=61)
            statuses[name] = created[0]
        fresh = await panel.create_request(conn, staff_id=PAULO, chat_id="100",
                                           kind="message", text="fresh")
        assert fresh
        await panel.start(conn, fresh[0])
        await _age(conn, fresh[0], column="started_at", seconds=400)
        flights = [await panel.in_flight(conn, who) for who in (RUI, ANA, PAULO)]
        return ([await _status(conn, rid) for rid in statuses.values()],
                flights, await _status(conn, fresh[0]))

    settled, flights, fresh = db.run(body)

    assert settled == ["failed", "failed"]
    assert flights[0] is None and flights[1] is None
    assert flights[2] is not None and flights[2].status == "running"
    assert fresh == "running", "a request inside the worker's own time limit is left alone"


def test_queued_and_deferred_requests_are_never_settled_as_failed(db, monkeypatch):
    monkeypatch.setattr(settings, "panel_sending_grace_seconds", 60)

    async def body(conn):
        ids = []
        for staff_id, chat, deferred in ((RUI, "400", False), (ANA, "500", True)):
            created = await panel.create_request(conn, staff_id=staff_id, chat_id=chat,
                                                 kind="message", text="x")
            assert created
            await panel.mark_handed(conn, created[0], deferred=deferred)
            await _age(conn, created[0], column="updated_at", seconds=36000)
            await _age(conn, created[0], column="created_at", seconds=36000)
            ids.append(created[0])
        flights = [await panel.in_flight(conn, who) for who in (RUI, ANA)]
        return [f.status if f else None for f in flights]

    assert db.run(body) == ["queued", "deferred"]


def test_only_the_owner_withdraws_and_only_before_it_runs(db):
    async def body(conn):
        created = await panel.create_request(conn, staff_id=RUI, chat_id="400",
                                             kind="message", text="a")
        assert created
        await panel.mark_handed(conn, created[0], deferred=True)
        by_other = await panel.withdraw(conn, created[0], staff_id=ANA)
        by_owner = await panel.withdraw(conn, created[0], staff_id=RUI)
        running = await panel.create_request(conn, staff_id=ANA, chat_id="500",
                                             kind="message", text="b")
        assert running
        await panel.start(conn, running[0])
        too_late = await panel.withdraw(conn, running[0], staff_id=ANA)
        return by_other, by_owner, too_late, await _status(conn, running[0])

    assert db.run(body) == (False, True, False, "running")


def test_a_withdrawn_request_frees_the_person_to_send_again(db):
    async def body(conn):
        created = await panel.create_request(conn, staff_id=RUI, chat_id="400",
                                             kind="message", text="a")
        assert created
        await panel.mark_handed(conn, created[0], deferred=True)
        blocked = await panel.create_request(conn, staff_id=RUI, chat_id="400",
                                             kind="message", text="b")
        await panel.withdraw(conn, created[0], staff_id=RUI)
        after = await panel.create_request(conn, staff_id=RUI, chat_id="400",
                                           kind="message", text="c")
        turns = await _rows(conn, "SELECT text FROM conversation_turns ORDER BY id")
        return blocked, after, turns

    blocked, after, turns = db.run(body)

    assert blocked is None
    assert after is not None
    assert turns == [("a",), ("c",)], "a refused request leaves no turn behind"


def test_running_requests_fail_when_a_worker_starts(db):
    async def body(conn):
        running = await panel.create_request(conn, staff_id=RUI, chat_id="400",
                                             kind="message", text="a")
        queued = await panel.create_request(conn, staff_id=ANA, chat_id="500",
                                            kind="message", text="b")
        assert running and queued
        await panel.start(conn, running[0])
        await panel.mark_handed(conn, queued[0], deferred=False)
        failed = await panel.fail_running_at_startup(conn)
        return failed, await _status(conn, running[0]), await _status(conn, queued[0])

    assert db.run(body) == (1, "failed", "queued")


def test_two_simultaneous_requests_leave_one_active(db):
    async def body(conn):
        other = await db.connect()
        try:
            results = await asyncio.gather(
                panel.create_request(conn, staff_id=RUI, chat_id="400",
                                     kind="message", text="primeira aba"),
                panel.create_request(other, staff_id=RUI, chat_id="400",
                                     kind="message", text="segunda aba"),
            )
        finally:
            await other.close()
        active = await _rows(conn, "SELECT count(*) FROM panel_requests")
        turns = await _rows(conn, "SELECT count(*) FROM conversation_turns")
        return results, active, turns

    results, active, turns = db.run(body)

    assert sorted(r is None for r in results) == [False, True]
    assert active == [(1,)] and turns == [(1,)]


def test_a_file_is_sealed_and_read_only_by_its_owner_before_it_expires(db, master_key):
    data = b"%PDF-1.7 parecer sigiloso"

    async def body(conn):
        token = await panel.store_file(conn, staff_id=RUI, filename="parecer.pdf",
                                       media_type="application/pdf", data=data)
        assert token
        mine = await panel.read_file(conn, token, staff_id=RUI)
        theirs = await panel.read_file(conn, token, staff_id=ANA)
        stored = await _rows(conn, "SELECT sealed, size_bytes, content_sha FROM panel_files")
        async with conn.cursor() as cur:
            await cur.execute("UPDATE panel_files SET expires_at = NOW() - interval '1 second'")
        await conn.commit()
        expired = await panel.read_file(conn, token, staff_id=RUI)
        purged = await panel.purge_expired_files(conn)
        return token, mine, theirs, stored, expired, purged

    token, mine, theirs, stored, expired, purged = db.run(body)

    assert len(token) >= 40
    assert mine == ("parecer.pdf", "application/pdf", data)
    assert theirs is None
    sealed, size, sha = stored[0]
    assert data not in bytes(sealed) and size == len(data) and len(sha) == 64
    assert expired is None, "expiry is in the query, before the nightly pass"
    assert purged == 1


def test_file_name_is_owner_and_unexpired(db, master_key):
    async def body(conn):
        token = await panel.store_file(conn, staff_id=RUI, filename="a.docx",
                                       media_type="x", data=b"conteudo")
        assert token
        mine = await panel.file_name(conn, token, RUI)
        theirs = await panel.file_name(conn, token, ANA)
        owner = await panel.file_name(conn, token, PAULO)
        unknown = await panel.file_name(conn, "nao-existe", RUI)
        async with conn.cursor() as cur:
            await cur.execute("UPDATE panel_files SET expires_at = NOW() - interval '1 second'")
        await conn.commit()
        expired = await panel.file_name(conn, token, RUI)
        return mine, theirs, owner, unknown, expired

    mine, theirs, owner, unknown, expired = db.run(body)

    assert mine == "a.docx"
    assert (theirs, owner, unknown, expired) == (None, None, None, None)


def test_a_tampered_file_does_not_open(db, master_key):
    async def body(conn):
        token = await panel.store_file(conn, staff_id=RUI, filename="a.pdf",
                                       media_type="application/pdf", data=b"conteudo")
        assert token
        async with conn.cursor() as cur:
            await cur.execute("UPDATE panel_files SET sealed = sealed || '\\x00'::bytea")
        await conn.commit()
        return await panel.read_file(conn, token, staff_id=RUI)

    assert db.run(body) is None


def test_files_over_the_caps_are_refused(db, master_key, monkeypatch):
    monkeypatch.setattr(settings, "panel_file_max_bytes", 10)
    monkeypatch.setattr(settings, "panel_files_max_live_bytes", 15)

    async def body(conn):
        big = await panel.store_file(conn, staff_id=RUI, filename="a", media_type="x",
                                     data=b"x" * 11)
        first = await panel.store_file(conn, staff_id=RUI, filename="b", media_type="x",
                                       data=b"x" * 10)
        over_live = await panel.store_file(conn, staff_id=RUI, filename="c", media_type="x",
                                           data=b"x" * 6)
        other_person = await panel.store_file(conn, staff_id=ANA, filename="d",
                                              media_type="x", data=b"x" * 6)
        async with conn.cursor() as cur:
            await cur.execute("UPDATE panel_files SET expires_at = NOW() - interval '1 second'"
                              " WHERE staff_id = %s", (RUI,))
        await conn.commit()
        after_expiry = await panel.store_file(conn, staff_id=RUI, filename="e",
                                              media_type="x", data=b"x" * 6)
        return big, first, over_live, other_person, after_expiry

    big, first, over_live, other_person, after_expiry = db.run(body)

    assert big is None and over_live is None
    assert first and other_person and after_expiry


def test_erasure_removes_the_persons_panel_rows(db, master_key):
    async def body(conn):
        created = await panel.create_request(conn, staff_id=RUI, chat_id="400",
                                             kind="message", text="a")
        kept = await panel.create_request(conn, staff_id=ANA, chat_id="500",
                                          kind="message", text="b")
        assert created and kept
        await panel.store_file(conn, staff_id=RUI, filename="a", media_type="x", data=b"1")
        await panel.store_file(conn, staff_id=ANA, filename="b", media_type="x", data=b"2")
        await staff.erase_person(conn, actor_id=PAULO, target_id=RUI)
        requests = await _rows(conn, "SELECT staff_id FROM panel_requests")
        files = await _rows(conn, "SELECT staff_id FROM panel_files")
        return requests, files

    assert db.run(body) == ([(ANA,)], [(ANA,)])


def test_old_requests_go_with_the_conversation(db, monkeypatch):
    async def body(conn):
        old = await panel.create_request(conn, staff_id=RUI, chat_id="400",
                                         kind="message", text="a")
        assert old
        await panel.start(conn, old[0])
        await panel.finish(conn, old[0], "done")
        await _age(conn, old[0], column="created_at", seconds=91 * 86400)
        recent = await panel.create_request(conn, staff_id=RUI, chat_id="400",
                                            kind="message", text="b")
        waiting = await panel.create_request(conn, staff_id=ANA, chat_id="500",
                                             kind="message", text="c")
        assert recent and waiting
        await panel.mark_handed(conn, waiting[0], deferred=True)
        await _age(conn, waiting[0], column="created_at", seconds=91 * 86400)
        removed = await panel.purge_old_requests(conn, 90)
        left = await _rows(conn, "SELECT id FROM panel_requests")
        return removed, {row[0] for row in left}, {recent[0], waiting[0]}

    removed, left, kept = db.run(body)

    assert removed == 1 and left == kept, "an old request still waiting is not purged"


def test_a_pending_step_remembers_its_channel(db):
    async def body(conn):
        await pending_actions.remember(conn, "400", kind="decision", ttl_seconds=60,
                                       action="confirm_document_deletion", channel="panel")
        await conn.commit()
        peeked = await pending_actions.peek(conn, "400")
        await pending_actions.remember(conn, "400", kind="identifier", ttl_seconds=60)
        await conn.commit()
        replaced = await pending_actions.consume(conn, "400")
        return peeked, replaced

    peeked, replaced = db.run(body)

    assert peeked is not None and peeked.channel == "panel"
    assert replaced is not None and replaced.channel == "telegram"


def test_the_new_tables_and_columns_reach_an_old_database(db):
    """init.sql runs only on an empty data directory (lesson 17): a database
    that predates the panel gets everything from the startup path."""
    async def body(conn):
        async with conn.cursor() as cur:
            await cur.execute("CREATE TABLE staff (id SERIAL PRIMARY KEY, name TEXT)")
            await cur.execute("""
                CREATE TABLE pending_actions (
                    chat_id VARCHAR(50) PRIMARY KEY, kind VARCHAR(16) NOT NULL,
                    action VARCHAR(32), approved BOOLEAN, target_id INTEGER,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    expires_at TIMESTAMPTZ NOT NULL)""")
        await conn.commit()
        await pending_actions.ensure_pending_actions_schema(conn)
        await conversation.ensure_conversation_schema(conn)
        await panel.ensure_panel_schema(conn)
        await panel.ensure_panel_schema(conn)  # twice: a restart runs it again
        async with conn.cursor() as cur:
            await cur.execute("INSERT INTO staff (id, name) VALUES (4, 'Rui')")
        await conn.commit()
        first = await panel.create_request(conn, staff_id=4, chat_id="400",
                                           kind="message", text="a")
        second = await panel.create_request(conn, staff_id=4, chat_id="400",
                                            kind="message", text="b")
        assert first is not None and second is None, "one request at a time, here too"
        return await _rows(conn, """
            SELECT table_name, column_name FROM information_schema.columns
            WHERE table_schema = %s AND (table_name IN ('panel_requests', 'panel_files')
                  OR (table_name = 'pending_actions' AND column_name = 'channel'))
            """, (db.schema,))

    columns = set(db.run(body, init_sql=False))

    assert ("pending_actions", "channel") in columns
    assert {("panel_requests", c) for c in (
        "id", "staff_id", "chat_id", "kind", "person_turn_id", "status",
        "created_at", "updated_at", "started_at")} <= columns
    assert {("panel_files", c) for c in (
        "token", "staff_id", "filename", "media_type", "sealed", "wrapped_key",
        "content_sha", "size_bytes", "created_at", "expires_at")} <= columns


def test_init_sql_alone_declares_the_same_tables_and_columns(db):
    async def body(conn):
        return await _rows(conn, """
            SELECT table_name, column_name FROM information_schema.columns
            WHERE table_schema = %s AND (table_name IN ('panel_requests', 'panel_files')
                  OR (table_name = 'pending_actions' AND column_name = 'channel'))
            """, (db.schema,))

    columns = set(db.run(body, bootstraps=False))

    assert ("pending_actions", "channel") in columns
    assert ("panel_requests", "started_at") in columns
    assert ("panel_files", "content_sha") in columns


def test_the_daily_pass_purges_panel_files_and_old_requests(monkeypatch):
    """Without these calls the sealed bytes outlive their link, and the
    requests outlive the conversation they belong to."""
    from core.services import maintenance

    class _Pool:
        def connection(self):
            return self

        async def __aenter__(self):
            return "conn"

        async def __aexit__(self, *exc):
            return False

    async def pool():
        return _Pool()

    async def nothing(*_args, **_kwargs):
        return 0

    calls = []

    async def files(_conn):
        calls.append("files")
        return 0

    async def requests(_conn, days):
        calls.append(("requests", days))
        return 0

    monkeypatch.setattr(maintenance, "get_pool", pool)
    for name in ("purge_expired_access_log", "purge_expired_steps", "purge_expired_callbacks",
                 "purge_expired_enrolments", "purge_expired_sign_ins", "purge_expired_sessions", "purge_conversation"):
        monkeypatch.setattr(maintenance, name, nothing)
    monkeypatch.setattr(maintenance, "purge_expired_files", files)
    monkeypatch.setattr(maintenance, "purge_old_requests", requests)

    asyncio.run(maintenance.run_retention_pass())

    assert calls == ["files", ("requests", settings.query_retention_days)]


def test_a_label_is_written_on_the_persons_turn(db):
    async def body(conn):
        created = await panel.create_request(conn, staff_id=RUI, chat_id="400",
                                             kind="message", text="/feriados",
                                             label="Feriados")
        assert created is not None
        return created[1], await _rows(conn, "SELECT id, text, extras FROM conversation_turns")

    turn_id, turns = db.run(body)

    assert turns == [(turn_id, "/feriados", {"action": "Feriados"})]


def test_without_a_label_extras_are_as_before(db):
    async def body(conn):
        await panel.create_request(conn, staff_id=RUI, chat_id="400", kind="message",
                                   text="Qual o prazo?")
        return await _rows(conn, "SELECT extras FROM conversation_turns")

    assert db.run(body) == [(None,)]
