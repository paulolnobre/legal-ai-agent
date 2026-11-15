"""Who sees a followed case and its deadlines, against a real Postgres.

The rule is a query: a private case is its creator's alone (the owner
included sees nothing), a team case is its team's members' while they are
members. A test against mocked statements would pass with the clause gone,
so the rule is proven here.

Cast: Paulo is the owner; Rui and Ana share the team Cível (10); Bia is in
Trabalhista (11).
"""
import asyncio
from datetime import date, timedelta
from typing import Any

from core.repositories import processes, staff
from core.repositories.processes import DUPLICATE
from core.repositories.teams import scope_for

PAULO, RUI, ANA, BIA = 1, 4, 5, 7
CIVEL, TRABALHISTA = 10, 11
NUMBER = "08012345620268140301"
OTHER = "00000011120248140001"

_SEED = """
INSERT INTO staff (id, chat_id, name, role) VALUES (7, '700', 'Bia', 'advogado');
INSERT INTO teams (id, name) VALUES (10, 'Cível'), (11, 'Trabalhista');
INSERT INTO team_members (team_id, staff_id) VALUES (10, 4), (10, 5), (11, 7);
"""


async def _seed(conn):
    await processes.ensure_process_schema(conn)
    async with conn.cursor() as cur:
        await cur.execute(_SEED)
    await conn.commit()


async def _create(conn, who, number=NUMBER, team_id=None, client="Cliente X") -> Any:
    """Any: an id here, None or DUPLICATE where the test expects a refusal."""
    return await processes.create(conn, await scope_for(conn, who), process_number=number,
                                  client=client, note=None, team_id=team_id)


def test_private_process_is_invisible_to_another_lawyer_and_the_owner(db):
    async def body(conn):
        await _seed(conn)
        pid = await _create(conn, RUI)
        assert isinstance(pid, int) and pid > 0
        mine = await processes.list_open(conn, await scope_for(conn, RUI))
        assert [p.id for p in mine] == [pid]
        for who in (ANA, PAULO):
            scope = await scope_for(conn, who)
            assert await processes.list_open(conn, scope) == []
            assert await processes.add_deadline(conn, scope, pid, title="X",
                                                due_date=date.today(), staff_id=who) is None
            assert await processes.archive(conn, scope, pid) is False
        deadline: Any = await processes.add_deadline(conn, await scope_for(conn, RUI), pid,
                                                title="Contestação", due_date=date.today(),
                                                staff_id=RUI)
        assert deadline is not None
        for who in (ANA, PAULO):
            assert await processes.mark_done(conn, await scope_for(conn, who),
                                             deadline, who) is False

    db.run(body)


def test_team_process_is_visible_to_members_and_gone_after_leaving(db):
    async def body(conn):
        await _seed(conn)
        pid = await _create(conn, RUI, team_id=CIVEL)
        assert [p.id for p in await processes.list_open(conn, await scope_for(conn, ANA))] == [pid]
        assert await processes.list_open(conn, await scope_for(conn, BIA)) == []
        assert await processes.list_open(conn, await scope_for(conn, PAULO)) == []
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM team_members WHERE team_id = 10 AND staff_id = 5")
        await conn.commit()
        scope = await scope_for(conn, ANA)
        assert await processes.list_open(conn, scope) == []
        assert await processes.add_deadline(conn, scope, pid, title="X",
                                            due_date=date.today(), staff_id=ANA) is None

    db.run(body)


def test_cannot_create_in_a_team_one_is_not_in(db):
    async def body(conn):
        await _seed(conn)
        assert await _create(conn, RUI, team_id=TRABALHISTA) is None
        assert await _create(conn, PAULO, team_id=CIVEL) is None
        async with conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM tracked_processes")
            assert (await cur.fetchone())[0] == 0

    db.run(body)


def test_duplicate_in_one_scope_is_refused_and_concurrent_creates_leave_one_row(db):
    async def body(conn):
        await _seed(conn)
        assert await _create(conn, RUI) > 0
        assert await _create(conn, RUI) == DUPLICATE
        other = await db.connect()
        third = await db.connect()
        try:
            results = await asyncio.gather(_create(other, ANA, number=OTHER, team_id=CIVEL),
                                           _create(third, RUI, number=OTHER, team_id=CIVEL))
        finally:
            await other.close()
            await third.close()
        assert sorted(r == DUPLICATE for r in results) == [False, True]
        async with conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM tracked_processes WHERE process_number = %s",
                              (OTHER,))
            assert (await cur.fetchone())[0] == 1

    db.run(body)


def test_same_number_private_and_team_coexist(db):
    async def body(conn):
        await _seed(conn)
        ids = [await _create(conn, RUI), await _create(conn, RUI, team_id=CIVEL),
               await _create(conn, ANA)]
        assert all(i > 0 for i in ids) and len(set(ids)) == 3

    db.run(body)


def test_archived_frees_the_number(db):
    async def body(conn):
        await _seed(conn)
        pid = await _create(conn, RUI)
        assert await processes.archive(conn, await scope_for(conn, RUI), pid) is True
        assert await processes.list_open(conn, await scope_for(conn, RUI)) == []
        again = await _create(conn, RUI)
        assert again > 0 and again != pid

    db.run(body)


def test_list_sorted_by_nearest_open_deadline(db):
    async def body(conn):
        await _seed(conn)
        scope = await scope_for(conn, RUI)
        today = date.today()
        none = await _create(conn, RUI, number="00000022220248140001")
        far = await _create(conn, RUI, number="00000033320248140001")
        near = await _create(conn, RUI, number="00000044420248140001")
        for pid, days in ((far, 20), (near, 3)):
            await processes.add_deadline(conn, scope, pid, title="P",
                                         due_date=today + timedelta(days=days), staff_id=RUI)
        done: Any = await processes.add_deadline(conn, scope, far, title="Feito",
                                            due_date=today + timedelta(days=1), staff_id=RUI)
        assert await processes.mark_done(conn, scope, done, RUI) is True
        listed = await processes.list_open(conn, scope)
        assert [p.id for p in listed] == [near, far, none]
        far_row = listed[1]
        assert [d.title for d in far_row.deadlines] == ["Feito", "P"]
        assert far_row.deadlines[0].done_at is not None

    db.run(body)


def test_only_the_creator_changes_where_it_lives(db):
    async def body(conn):
        await _seed(conn)
        pid = await _create(conn, RUI, team_id=CIVEL)
        ana = await scope_for(conn, ANA)
        rui = await scope_for(conn, RUI)
        assert await processes.set_visibility(conn, ana, pid, team_id=None) is False
        assert await processes.set_visibility(conn, rui, pid, team_id=TRABALHISTA) is False
        assert await _create(conn, RUI) is not None
        assert await processes.set_visibility(conn, rui, pid, team_id=None) == DUPLICATE
        assert await processes.list_open(conn, ana) != []
        private = await _create(conn, RUI, number=OTHER)
        assert await processes.set_visibility(conn, rui, private, team_id=CIVEL) is True
        assert OTHER in [p.process_number for p in await processes.list_open(conn, ana)]

    db.run(body)


def test_erasure_removes_private_processes_and_alert_rows_keeps_team_ones(db):
    async def body(conn):
        await _seed(conn)
        scope = await scope_for(conn, RUI)
        private = await _create(conn, RUI)
        team = await _create(conn, RUI, team_id=CIVEL)
        deadline: Any = await processes.add_deadline(conn, scope, team, title="P",
                                                due_date=date.today(), staff_id=RUI)
        async with conn.cursor() as cur:
            await cur.execute("INSERT INTO deadline_alerts (deadline_id, staff_id, threshold) "
                              "VALUES (%s, 4, 0), (%s, 5, 0)", (deadline, deadline))
        await conn.commit()
        await staff.erase_person(conn, actor_id=PAULO, target_id=RUI)
        async with conn.cursor() as cur:
            await cur.execute("SELECT id FROM tracked_processes ORDER BY id")
            assert [r[0] for r in await cur.fetchall()] == [team]
            await cur.execute("SELECT staff_id FROM deadline_alerts")
            assert [r[0] for r in await cur.fetchall()] == [ANA]
        assert private != team

    db.run(body)


class _Interleaved:
    """A connection that, after its first statement returns, lets another
    connection act: what a second statement then reads is the race."""

    def __init__(self, conn, between):
        self.conn, self.between, self.statements = conn, between, 0

    def cursor(self):
        outer = self
        real = self.conn.cursor()

        class _Cur:
            async def __aenter__(self):
                self.cur = await real.__aenter__()
                return self

            async def __aexit__(self, *exc):
                return await real.__aexit__(*exc)

            async def execute(self, *args, **kwargs):
                await self.cur.execute(*args, **kwargs)
                outer.statements += 1
                if outer.statements == 1:
                    await outer.between()

            async def fetchall(self):
                return await self.cur.fetchall()

        return _Cur()

    async def commit(self):
        await self.conn.commit()


def test_a_case_made_private_mid_read_does_not_leak_its_new_deadline(db):
    async def body(conn):
        await _seed(conn)
        pid = await _create(conn, RUI, team_id=CIVEL)

        async def creator_takes_it_back():
            other = await db.connect()
            try:
                rui = await scope_for(other, RUI)
                assert await processes.set_visibility(other, rui, pid, team_id=None) is True
                await processes.add_deadline(other, rui, pid, title="Sigiloso",
                                             due_date=date.today(), staff_id=RUI)
            finally:
                await other.close()

        listed = await processes.list_open(_Interleaved(conn, creator_takes_it_back),
                                           await scope_for(conn, ANA))
        assert all(d.title != "Sigiloso" for p in listed for d in p.deadlines)

    db.run(body)


def test_a_duplicate_keeps_the_callers_earlier_work(db):
    async def body(conn):
        await _seed(conn)
        assert await _create(conn, RUI) > 0
        async with conn.cursor() as cur:
            await cur.execute("INSERT INTO teams (id, name) VALUES (12, 'Antes')")
        assert await _create(conn, RUI) == DUPLICATE
        async with conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM teams WHERE id = 12")
            assert (await cur.fetchone())[0] == 1

    db.run(body)
