"""A Microsoft sign-in waiting in the panel, against a real Postgres.

The device code is a bearer credential for fifteen minutes: sealed at rest,
one per person, gone at expiry. A poll is claimed atomically, and every write
after it names its flow, so a sign-in replaced or cancelled while its poll
was out connects nothing.
"""
import asyncio

import pytest
from cryptography.fernet import Fernet

from core.config import settings
from core.repositories import microsoft_accounts as ms

RUI, ANA = 4, 5


@pytest.fixture(autouse=True)
def sealed(monkeypatch):
    monkeypatch.setattr(settings, "ms_token_key", Fernet.generate_key().decode())


async def _start(conn, who: int, code: str = "DEVICE-CODE-A", interval: int = 5,
                 expires_in: int = 900):
    await ms.start_sign_in(conn, who, device_code=code, user_code="ABCD-1234",
                           verification_uri="https://microsoft.com/devicelogin",
                           interval_seconds=interval, expires_in=expires_in)


async def _rows(conn, statement, params=()):
    async with conn.cursor() as cur:
        await cur.execute(statement, params)
        return await cur.fetchall()


async def _due(conn, who: int) -> None:
    async with conn.cursor() as cur:
        await cur.execute("UPDATE microsoft_sign_ins SET next_poll_at = NOW() - "
                          "INTERVAL '1 second' WHERE staff_id = %s", (who,))
    await conn.commit()


def test_a_sign_in_is_sealed_and_read_back(db):
    async def body(conn):
        await _start(conn, RUI)
        stored = await _rows(conn, "SELECT device_code_sealed FROM microsoft_sign_ins")
        return stored, await ms.live_sign_in(conn, RUI)

    stored, live = db.run(body)

    assert b"DEVICE-CODE-A" not in bytes(stored[0][0])
    assert live is not None and live.device_code == "DEVICE-CODE-A"
    assert live.user_code == "ABCD-1234" and live.interval_seconds == 5


def test_a_second_start_replaces_the_first(db):
    async def body(conn):
        await _start(conn, RUI, "DEVICE-CODE-A")
        first = await ms.live_sign_in(conn, RUI)
        await _start(conn, RUI, "DEVICE-CODE-B")
        second = await ms.live_sign_in(conn, RUI)
        return first, second, await _rows(conn, "SELECT count(*) FROM microsoft_sign_ins")

    first, second, count = db.run(body)

    assert first and second and first.flow_id != second.flow_id
    assert second.device_code == "DEVICE-CODE-B"
    assert count == [(1,)]


def test_an_expired_sign_in_is_not_live_and_is_purged(db):
    async def body(conn):
        await _start(conn, RUI)
        await _start(conn, ANA)
        async with conn.cursor() as cur:
            await cur.execute("UPDATE microsoft_sign_ins SET expires_at = NOW() - "
                              "INTERVAL '1 second' WHERE staff_id = %s", (RUI,))
        await conn.commit()
        await _due(conn, RUI)
        live = await ms.live_sign_in(conn, RUI)
        claimed = await ms.claim_poll(conn, RUI)
        purged = await ms.purge_expired_sign_ins(conn)
        return live, claimed, purged, await _rows(conn, "SELECT staff_id FROM microsoft_sign_ins")

    live, claimed, purged, left = db.run(body)

    assert live is None and claimed is None
    assert purged == 1 and left == [(ANA,)]


def test_deleting_the_person_deletes_the_row(db):
    async def body(conn):
        await _start(conn, ANA)
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM staff WHERE id = %s", (ANA,))
        await conn.commit()
        return await _rows(conn, "SELECT count(*) FROM microsoft_sign_ins")

    assert db.run(body) == [(0,)]


def test_a_poll_is_not_claimed_before_it_is_due(db):
    async def body(conn):
        await _start(conn, RUI)
        early = await ms.claim_poll(conn, RUI)
        await _due(conn, RUI)
        due = await ms.claim_poll(conn, RUI)
        again = await ms.claim_poll(conn, RUI)
        return early, due, again

    early, due, again = db.run(body)

    assert early is None
    assert due is not None and due.device_code == "DEVICE-CODE-A"
    assert again is None, "the claim moved the next poll"


def test_only_one_concurrent_claim_gets_the_row(db):
    async def body(conn):
        await _start(conn, RUI)
        await _due(conn, RUI)
        others = [await db.connect() for _ in range(4)]
        try:
            claims = await asyncio.gather(*(ms.claim_poll(c, RUI) for c in others))
        finally:
            for other in others:
                await other.close()
        return claims

    claims = db.run(body)

    assert sum(claim is not None for claim in claims) == 1


def test_finishing_a_replaced_flow_saves_nothing(db):
    async def body(conn):
        await _start(conn, RUI, "DEVICE-CODE-A")
        await _due(conn, RUI)
        claimed = await ms.claim_poll(conn, RUI)
        assert claimed
        await _start(conn, RUI, "DEVICE-CODE-B")
        stale = await ms.finish_sign_in(conn, RUI, claimed.flow_id,
                                        account_name="rui@x.adv.br", refresh_token="t")
        account = await ms.get_account(conn, RUI)
        live = await ms.live_sign_in(conn, RUI)
        return stale, account, live

    stale, account, live = db.run(body)

    assert stale is False and account is None
    assert live is not None and live.device_code == "DEVICE-CODE-B", "the newer flow stays"


def test_finishing_the_current_flow_connects_and_ends_it(db):
    async def body(conn):
        await _start(conn, RUI)
        await _due(conn, RUI)
        claimed = await ms.claim_poll(conn, RUI)
        assert claimed
        done = await ms.finish_sign_in(conn, RUI, claimed.flow_id,
                                       account_name="rui@x.adv.br", refresh_token="t")
        return done, await ms.get_account(conn, RUI), await ms.live_sign_in(conn, RUI)

    done, account, live = db.run(body)

    assert done is True and account and account.account_name == "rui@x.adv.br"
    assert live is None


def test_disconnect_also_cancels_a_waiting_sign_in(db):
    async def body(conn):
        await ms.save_account(conn, RUI, account_name="rui@x.adv.br", refresh_token="t")
        await _start(conn, RUI)
        await _due(conn, RUI)
        claimed = await ms.claim_poll(conn, RUI)
        assert claimed
        removed = await ms.disconnect(conn, RUI)
        late = await ms.finish_sign_in(conn, RUI, claimed.flow_id,
                                       account_name="rui@x.adv.br", refresh_token="t2")
        return removed, late, await ms.get_account(conn, RUI), await ms.live_sign_in(conn, RUI)

    removed, late, account, live = db.run(body)

    assert removed is True and late is False
    assert account is None and live is None


def test_delay_and_end_touch_only_their_flow(db):
    async def body(conn):
        await _start(conn, RUI, "DEVICE-CODE-A")
        old = await ms.live_sign_in(conn, RUI)
        await _start(conn, RUI, "DEVICE-CODE-B")
        assert old
        await ms.delay_poll(conn, RUI, old.flow_id, 60)
        ended = await ms.end_sign_in(conn, RUI, old.flow_id)
        return ended, await ms.live_sign_in(conn, RUI)

    ended, live = db.run(body)

    assert ended is False
    assert live is not None and live.device_code == "DEVICE-CODE-B"
    assert live.interval_seconds == 5, "the old flow's delay did not reach the new one"


def test_slow_down_postpones_the_current_flow(db):
    async def body(conn):
        await _start(conn, RUI)
        live = await ms.live_sign_in(conn, RUI)
        assert live
        await ms.delay_poll(conn, RUI, live.flow_id, 10)
        after = await ms.live_sign_in(conn, RUI)
        await _due(conn, RUI)
        return live, after

    live, after = db.run(body)

    assert after is not None and after.interval_seconds == 10
    assert after.next_poll_at > live.next_poll_at


def test_disconnect_wins_against_a_sign_in_finishing_at_the_same_time(db):
    """The poll has taken its flow (row locked, not committed) when the person
    presses Desconectar: whatever the order the two commit in, no account is
    left connected."""
    async def body(conn):
        await _start(conn, RUI)
        live = await ms.live_sign_in(conn, RUI)
        assert live
        finishing = await db.connect()
        disconnecting = await db.connect()
        try:
            # finish_sign_in, held between its two steps
            assert await ms.end_sign_in(finishing, RUI, live.flow_id, commit=False)
            pressed = asyncio.create_task(ms.disconnect(disconnecting, RUI))
            await asyncio.sleep(0.5)
            await ms.save_account(finishing, RUI, account_name="rui@x.adv.br",
                                  refresh_token="t", commit=False)
            await finishing.commit()
            await asyncio.wait_for(pressed, 10)
        finally:
            await finishing.close()
            await disconnecting.close()
        return await ms.get_account(conn, RUI)

    assert db.run(body) is None


def test_the_table_is_created_by_the_startup_bootstrap_alone(db):
    async def body(conn):
        async with conn.cursor() as cur:
            await cur.execute("CREATE TABLE staff (id INTEGER PRIMARY KEY)")
            await cur.execute("INSERT INTO staff (id) VALUES (4)")
        await conn.commit()
        await ms.ensure_microsoft_schema(conn)
        await ms.ensure_microsoft_schema(conn)
        await _start(conn, RUI)
        return await ms.live_sign_in(conn, RUI)

    live = db.run(body, init_sql=False)

    assert live is not None and live.device_code == "DEVICE-CODE-A"


def test_a_claim_holds_the_turn_longer_than_a_slow_request(db):
    """Two tabs: the second must not poll the same code while the first is
    still waiting on Microsoft, however long that takes within reason."""
    async def body(conn):
        await _start(conn, RUI)
        await _due(conn, RUI)
        claimed = await ms.claim_poll(conn, RUI)
        assert claimed
        rows = await _rows(conn, "SELECT EXTRACT(EPOCH FROM next_poll_at - NOW()) "
                                 "FROM microsoft_sign_ins")
        return float(rows[0][0])

    assert db.run(body) >= 50
