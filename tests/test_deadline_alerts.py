"""Deadline alerts: 10 and 5 days before and on the day, against a real Postgres.

Who is told is a query (the creator, or the team's active members), and
"already told" is the `deadline_alerts` table alone. Telegram and the push
service are fakes; the rows and the recipients are real.

Cast: Paulo is the owner; Rui and Ana share the team Cível (10); Bia is in
Cível too but deactivated; Caio has no Telegram chat.
"""
import asyncio
import contextlib
from datetime import date, datetime, time, timedelta
from typing import Any

import pytest

from core.repositories import processes
from core.repositories.teams import scope_for
from core.services import deadline_alerts
from core.services.deadline_alerts import threshold

PAULO, RUI, ANA, BIA, CAIO = 1, 4, 5, 7, 8
CIVEL = 10
TODAY = date(2026, 11, 10)
NUMBER = "08012345620268140301"

_SEED = """
INSERT INTO staff (id, chat_id, name, role, active) VALUES
    (7, '700', 'Bia', 'advogado', FALSE), (8, NULL, 'Caio', 'advogado', TRUE);
INSERT INTO teams (id, name) VALUES (10, 'Cível');
INSERT INTO team_members (team_id, staff_id) VALUES (10, 4), (10, 5), (10, 7);
"""


@pytest.mark.parametrize(("days", "expected"), [
    (11, None), (10, 10), (7, 10), (6, 10), (5, 5), (1, 5), (0, 0), (-1, None),
])
def test_threshold(days, expected):
    assert threshold(days) == expected


class World:
    def __init__(self, db, monkeypatch):
        self.db = db
        self.sent: list[tuple[str, str, Any]] = []
        self.pushed: list[int] = []
        self.telegram_down: set[str] = set()
        self.push_accepts: set[int] = set()

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

        async def send_message(chat_id, text, *args, parse_mode="Markdown", **kwargs):
            if str(chat_id) in self.telegram_down:
                return None
            self.sent.append((str(chat_id), text, parse_mode))
            return 1

        async def notify(staff_id, kind):
            assert kind == "deadline"
            self.pushed.append(staff_id)
            return staff_id in self.push_accepts

        monkeypatch.setattr(deadline_alerts, "get_pool", get_pool)
        monkeypatch.setattr(deadline_alerts.telegram, "send_message", send_message)
        monkeypatch.setattr(deadline_alerts.push, "notify", notify)

    def run(self, body):
        async def main(conn):
            await processes.ensure_process_schema(conn)
            async with conn.cursor() as cur:
                await cur.execute(_SEED)
            await conn.commit()
            return await body(conn)
        return self.db.run(main)

    def chats(self) -> list[str]:
        return sorted(chat for chat, _, _ in self.sent)


async def _deadline(conn, who, days, *, team_id=None, title="Contestação", number=NUMBER,
                    client: str | None = "Cliente X") -> int:
    scope = await scope_for(conn, who)
    pid: Any = await processes.create(conn, scope, process_number=number, client=client,
                                      note=None, team_id=team_id)
    if pid == processes.DUPLICATE:
        found = [p.id for p in await processes.list_open(conn, scope)
                 if p.process_number == number]
        pid = found[0]
    made: Any = await processes.add_deadline(conn, scope, pid, title=title,
                                             due_date=TODAY + timedelta(days=days),
                                             staff_id=who)
    return made


def deadline_alerts_settings():
    from core.config import settings
    return settings


@pytest.fixture
def world(db, monkeypatch):
    return World(db, monkeypatch)


def test_private_alert_goes_to_creator_only(world):
    async def body(conn):
        await _deadline(conn, RUI, 5)
        assert await deadline_alerts.check_once(TODAY) == 1
        assert world.chats() == ["400"]

    world.run(body)


def test_team_alert_goes_to_active_members_only(world):
    async def body(conn):
        await _deadline(conn, RUI, 0, team_id=CIVEL)
        async with conn.cursor() as cur:
            await cur.execute("INSERT INTO team_members (team_id, staff_id) VALUES (10, 1)")
            await cur.execute("DELETE FROM team_members WHERE team_id = 10 AND staff_id = 1")
        await conn.commit()
        await deadline_alerts.check_once(TODAY)
        assert world.chats() == ["400", "500"]

    world.run(body)


def test_no_alert_twice_across_two_passes(world):
    async def body(conn):
        await _deadline(conn, RUI, 10)
        await deadline_alerts.check_once(TODAY)
        await deadline_alerts.check_once(TODAY)
        await deadline_alerts.check_once(TODAY + timedelta(days=1))
        assert len(world.sent) == 1

    world.run(body)


def test_failed_telegram_writes_no_row_and_retries(world):
    async def body(conn):
        await _deadline(conn, RUI, 5)
        world.telegram_down.add("400")
        assert await deadline_alerts.check_once(TODAY) == 0
        world.telegram_down.clear()
        assert await deadline_alerts.check_once(TODAY) == 1
        assert world.chats() == ["400"]

    world.run(body)


def test_push_only_person_not_recorded_when_no_subscription_accepted(world):
    async def body(conn):
        await _deadline(conn, CAIO, 0)
        assert await deadline_alerts.check_once(TODAY) == 0
        world.push_accepts.add(CAIO)
        assert await deadline_alerts.check_once(TODAY) == 1
        assert await deadline_alerts.check_once(TODAY) == 0
        assert world.pushed == [CAIO, CAIO]
        assert world.sent == []

    world.run(body)


def test_done_and_archived_send_nothing(world):
    async def body(conn):
        scope = await scope_for(conn, RUI)
        done = await _deadline(conn, RUI, 0)
        await processes.mark_done(conn, scope, done, RUI)
        await _deadline(conn, RUI, 5, number="00000011120248140001")
        pid = [p.id for p in await processes.list_open(conn, scope)
               if p.process_number == "00000011120248140001"][0]
        await processes.archive(conn, scope, pid)
        assert await deadline_alerts.check_once(TODAY) == 0
        assert world.sent == []

    world.run(body)


def test_missed_higher_threshold_not_sent_late(world):
    async def body(conn):
        await _deadline(conn, RUI, 7)
        await deadline_alerts.check_once(TODAY)
        await deadline_alerts.check_once(TODAY + timedelta(days=1))
        await deadline_alerts.check_once(TODAY + timedelta(days=2))
        texts = [text for _, text, _ in world.sent]
        assert len(texts) == 2
        assert "em 7 dias" in texts[0] and "em 5 dias" in texts[1]

    world.run(body)


def test_message_wording(world, monkeypatch):
    monkeypatch.setattr(deadline_alerts_settings(), "public_base_url", "https://lex.example")
    monkeypatch.setattr(deadline_alerts_settings(), "web_base_url", None)

    async def body(conn):
        await _deadline(conn, RUI, 0, title="Recurso_*x*", client=None)
        await _deadline(conn, RUI, 1, title="Réplica", number="00000011120248140001")
        await deadline_alerts.check_once(TODAY)
        [(_, text, parse_mode)] = world.sent
        assert parse_mode is None
        assert "🔴 Prazo vence HOJE (10/11/2026): Recurso_*x* · proc. 0801234-56.2026.8.14.0301" \
            in text
        assert "⚠️ Prazo vence amanhã (11/11/2026): Réplica · proc. " \
            "0000001-11.2024.8.14.0001 · Cliente X" in text
        assert text.endswith("Veja em https://lex.example/web/processos/")

    world.run(body)


def test_long_lists_are_split_under_telegram_s_limit():
    items = [deadline_alerts.Due(deadline_id=i, staff_id=RUI, chat_id="400", title="T" * 100,
                                 due_date=TODAY, process_number=NUMBER, client="C" * 150,
                                 threshold=0) for i in range(80)]
    messages = deadline_alerts.messages_for(items, TODAY)
    assert len(messages) > 1
    assert all(len(m) <= 3800 for m in messages)
    assert sum(m.count("Prazo vence") for m in messages) == 80


def test_loop_alerts_a_deadline_added_after_the_first_pass(world, monkeypatch):
    times = iter([time(7, 59), time(8, 0), time(10, 0)])
    now: list[time] = []

    def local_now():
        now.append(next(times))
        return datetime.combine(TODAY, now[-1])

    monkeypatch.setattr(deadline_alerts, "local_now", local_now)
    real = deadline_alerts.check_once
    passes: list[int] = []
    ran_at: list[time] = []

    async def body(conn):
        stop = asyncio.Event()

        async def counted(today=None):
            delivered = await real(today)
            passes.append(delivered)
            ran_at.append(now[-1])
            if len(passes) == 1:
                await _deadline(conn, RUI, 0)
            else:
                stop.set()
            return delivered

        monkeypatch.setattr(deadline_alerts, "check_once", counted)
        await asyncio.wait_for(deadline_alerts.run_deadline_alerts_worker(stop, interval=0.01),
                               timeout=10)
        assert passes == [0, 1]
        assert ran_at == [time(8, 0), time(10, 0)], "no pass before 08:00"
        assert world.chats() == ["400"]

    world.run(body)


def test_a_person_deactivated_mid_pass_is_not_told(world, monkeypatch):
    """Recipients are read again just before each person is told."""
    sent = deadline_alerts.telegram.send_message

    async def body(conn):
        await _deadline(conn, RUI, 0, team_id=CIVEL)

        async def deactivating(chat_id, text, *args, **kwargs):
            if chat_id == "400":
                async with conn.cursor() as cur:
                    await cur.execute("UPDATE staff SET active = FALSE WHERE id = 5")
                await conn.commit()
            return await sent(chat_id, text, *args, **kwargs)

        monkeypatch.setattr(deadline_alerts.telegram, "send_message", deactivating)
        await deadline_alerts.check_once(TODAY)
        assert world.chats() == ["400"]

    world.run(body)


def test_a_failed_telegram_is_retried_even_when_the_push_arrived(world):
    async def body(conn):
        await _deadline(conn, RUI, 5)
        world.telegram_down.add("400")
        world.push_accepts.add(RUI)
        assert await deadline_alerts.check_once(TODAY) == 0
        world.telegram_down.clear()
        assert await deadline_alerts.check_once(TODAY) == 1
        assert world.chats() == ["400"]

    world.run(body)
