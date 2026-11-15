"""Connecting Microsoft from the panel: one poll per ask, never early.

Real Postgres for the sign-in row and the account; Microsoft itself faked.
"""
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet

from core.config import settings
from core.integrations import microsoft
from core.models import StaffMember
from core.repositories import microsoft_accounts as ms
from core.services import microsoft_link

RUI = StaffMember(id=4, chat_id="400", name="Rui", role="advogado")


class _Microsoft:
    def __init__(self):
        self.codes = iter(["DEVICE-A", "DEVICE-B", "DEVICE-C"])
        self.polled: list[str] = []
        self.answer: object = microsoft.SignInPending("authorization_pending")
        self.during_poll: Callable[[], Awaitable[None]] | None = None

    async def start(self):
        return microsoft.DeviceFlow(user_code="ABCD-1234",
                                    verification_uri="https://microsoft.com/devicelogin",
                                    device_code=next(self.codes), interval=5, expires_in=900)

    async def poll(self, device_code):
        self.polled.append(device_code)
        if self.during_poll:
            await self.during_poll()
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    async def whoami(self, _token):
        return "rui@x.adv.br"


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(settings, "ms_token_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "ms_enabled", True)
    monkeypatch.setattr(microsoft, "configured", lambda: True)
    fake = _Microsoft()
    monkeypatch.setattr(microsoft, "start_device_flow", fake.start)
    monkeypatch.setattr(microsoft, "poll_once", fake.poll)
    monkeypatch.setattr(microsoft, "whoami", fake.whoami)
    return fake


async def _due(conn):
    async with conn.cursor() as cur:
        await cur.execute("UPDATE microsoft_sign_ins SET next_poll_at = NOW() - "
                          "INTERVAL '1 second'")
    await conn.commit()


async def _begin_and_check(conn):
    started = await microsoft_link.begin(conn, RUI)
    assert isinstance(started, ms.SignIn)
    await _due(conn)
    return await microsoft_link.check(conn, RUI)


def test_without_a_code_there_is_nothing_to_check(db, fake):
    async def body(conn):
        return await microsoft_link.check(conn, RUI)

    assert db.run(body) == "sem_codigo"
    assert fake.polled == []


def test_no_call_before_next_poll(db, fake):
    async def body(conn):
        await microsoft_link.begin(conn, RUI)
        return await microsoft_link.check(conn, RUI)

    assert db.run(body) == "aguardando"
    assert fake.polled == []


def test_pending_waits(db, fake):
    async def body(conn):
        return await _begin_and_check(conn), await ms.live_sign_in(conn, RUI.id)

    outcome, live = db.run(body)

    assert outcome == "aguardando" and fake.polled == ["DEVICE-A"]
    assert live is not None


def test_slow_down_pushes_the_next_poll(db, fake):
    fake.answer = microsoft.SignInPending("slow_down")

    async def body(conn):
        outcome = await _begin_and_check(conn)
        return outcome, await ms.live_sign_in(conn, RUI.id)

    outcome, live = db.run(body)

    assert outcome == "aguardando"
    assert live is not None and live.interval_seconds == 10
    assert (live.next_poll_at - datetime.now(timezone.utc)).total_seconds() > 8, (
        "the next poll waits the longer interval, not the first one")


def test_signed_in_connects_and_ends_the_code(db, fake):
    fake.answer = microsoft.Tokens(access_token="a", refresh_token="r")

    async def body(conn):
        outcome = await _begin_and_check(conn)
        return (outcome, await ms.get_account(conn, RUI.id), await ms.live_sign_in(conn, RUI.id),
                await ms.get_refresh_token(conn, RUI.id))

    outcome, account, live, token = db.run(body)

    assert outcome == "conectada"
    assert account and account.account_name == "rui@x.adv.br" and token == "r"
    assert live is None


def test_declined_ends_the_code(db, fake):
    fake.answer = microsoft.SignInDeclined("authorization_declined")

    async def body(conn):
        return await _begin_and_check(conn), await ms.live_sign_in(conn, RUI.id)

    outcome, live = db.run(body)

    assert outcome == "recusada" and live is None


def test_an_error_keeps_the_code_for_the_next_poll(db, fake):
    fake.answer = microsoft.MicrosoftError("Microsoft did not respond.")

    async def body(conn):
        outcome = await _begin_and_check(conn)
        return outcome, await ms.live_sign_in(conn, RUI.id), await ms.get_account(conn, RUI.id)

    outcome, live, account = db.run(body)

    assert outcome == "erro" and live is not None and account is None


def test_two_starts_poll_the_newer_code(db, fake):
    async def body(conn):
        await microsoft_link.begin(conn, RUI)
        await microsoft_link.begin(conn, RUI)
        await _due(conn)
        return await microsoft_link.check(conn, RUI)

    db.run(body)

    assert fake.polled == ["DEVICE-B"]


def test_a_poll_out_while_replaced_does_not_connect(db, fake):
    fake.answer = microsoft.Tokens(access_token="a", refresh_token="r")

    async def body(conn):
        async def replaced():
            other = await db.connect()
            try:
                await microsoft_link.begin(other, RUI)
            finally:
                await other.close()
        fake.during_poll = replaced
        outcome = await _begin_and_check(conn)
        return outcome, await ms.get_account(conn, RUI.id), await ms.live_sign_in(conn, RUI.id)

    outcome, account, live = db.run(body)

    assert outcome == "recusada"
    assert account is None
    assert live is not None and live.device_code == "DEVICE-B"


def test_begin_refuses_when_connected_or_unconfigured(db, fake, monkeypatch):
    async def body(conn):
        await ms.save_account(conn, RUI.id, account_name="rui@x.adv.br", refresh_token="t")
        connected = await microsoft_link.begin(conn, RUI)
        await ms.forget_account(conn, RUI.id)
        monkeypatch.setattr(microsoft, "configured", lambda: False)
        unconfigured = await microsoft_link.begin(conn, RUI)
        return connected, unconfigured, await ms.live_sign_in(conn, RUI.id)

    connected, unconfigured, live = db.run(body)

    assert connected == "ja_conectada" and unconfigured == "indisponivel"
    assert live is None


def test_begin_says_error_when_microsoft_will_not_start(db, fake, monkeypatch):
    async def refuse():
        raise microsoft.MicrosoftError("down")

    monkeypatch.setattr(microsoft, "start_device_flow", refuse)

    async def body(conn):
        return await microsoft_link.begin(conn, RUI), await ms.live_sign_in(conn, RUI.id)

    assert db.run(body) == ("erro", None)


def test_a_failed_poll_never_logs_what_microsoft_said(db, fake, caplog):
    fake.answer = microsoft.MicrosoftError("invalid device_code DEVICE-A for client")

    async def body(conn):
        return await _begin_and_check(conn)

    with caplog.at_level("DEBUG"):
        assert db.run(body) == "erro"

    assert "DEVICE-A" not in caplog.text


def test_after_an_answer_the_next_poll_is_the_interval_again(db, fake):
    async def body(conn):
        outcome = await _begin_and_check(conn)
        return outcome, await ms.live_sign_in(conn, RUI.id)

    outcome, live = db.run(body)

    assert outcome == "aguardando" and live is not None
    left = (live.next_poll_at - datetime.now(timezone.utc)).total_seconds()
    assert 3 < left <= 6, "the hold is released once Microsoft has answered"


def test_after_an_error_the_next_poll_is_the_interval_again(db, fake):
    fake.answer = microsoft.MicrosoftError("down")

    async def body(conn):
        outcome = await _begin_and_check(conn)
        return outcome, await ms.live_sign_in(conn, RUI.id)

    outcome, live = db.run(body)

    assert outcome == "erro" and live is not None
    assert (live.next_poll_at - datetime.now(timezone.utc)).total_seconds() <= 6
