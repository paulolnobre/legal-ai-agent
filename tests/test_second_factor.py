"""The second-factor check the web login and the bot share.

The SQL that counts and locks is tested against a real database in
test_web_access_sql.py. What is tested here is the order around it, which is
where a check like this goes wrong: a locked account must not be tried at all,
a right code must clear the count, and the owners must hear of a lock even
though the person's own Telegram may be the thing under attack.
"""
import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core import totp
from core.models import StaffMember
from core.repositories.web_access import Enrolment
from core.services import second_factor

SECRET = "JBSWY3DPEHPK3PXP"
_PERSON = StaffMember(id=4, chat_id="400", name="Rui", role="advogado")
_OWNER = StaffMember(id=1, chat_id="100", name="Paulo", role="owner")


@pytest.fixture
def store(monkeypatch):
    state = SimpleNamespace(
        confirmed=True, locked=False, failures=0, cleared=0, claimed=[],
        recovery={"A1B2C3D4"}, sent=[], owners=[_OWNER], confirmed_calls=0,
    )
    repo = second_factor.web_access

    async def enrolment_of(_conn, staff_id):
        return Enrolment(staff_id=staff_id, enrolled_at=datetime.now(timezone.utc),
                         recovery_left=len(state.recovery),
                         confirmed=state.confirmed)

    async def secret_of(_conn, _staff_id):
        return SECRET

    async def claim_counter(_conn, _staff_id, counter, commit=True):
        if counter in state.claimed:
            return False
        state.claimed.append(counter)
        return True

    async def confirm_enrolment(_conn, _staff_id, commit=True):
        state.confirmed_calls += 1
        state.confirmed = True

    async def spend_recovery_code(_conn, _staff_id, code):
        code = code.strip().upper()
        if code in state.recovery:
            state.recovery.discard(code)
            return True
        return False

    async def locked(_conn, _staff_id):
        return state.locked

    async def record_failure(_conn, _staff_id, *, limit, lock_seconds):
        state.failures += 1
        if state.failures >= limit:
            state.failures = 0
            state.locked = True
            return True
        return False

    async def clear_failures(_conn, _staff_id):
        state.cleared += 1
        state.failures = 0

    async def get_staff_by_id(_conn, _staff_id):
        return _PERSON

    async def list_staff(_conn, roles=None):
        assert roles == ("owner",)
        return state.owners

    async def send_message(chat_id, text, parse_mode=None, **_kwargs):
        state.sent.append((str(chat_id), text))
        return 1

    for name, fn in {
        "enrolment_of": enrolment_of, "secret_for_check": secret_of,
        "claim_counter": claim_counter, "confirm_enrolment": confirm_enrolment,
        "spend_recovery_code": spend_recovery_code,
        "second_factor_locked": locked, "record_failure": record_failure,
        "clear_failures": clear_failures,
    }.items():
        monkeypatch.setattr(repo, name, fn)
    monkeypatch.setattr(second_factor, "get_staff_by_id", get_staff_by_id)
    monkeypatch.setattr(second_factor, "list_staff", list_staff)
    monkeypatch.setattr(second_factor.telegram, "send_message", send_message)
    return state


class _Conn:
    async def commit(self):
        return None


def _check(code: str):
    return asyncio.run(second_factor.check(_Conn(), _PERSON.id, code))


def _now_code() -> str:
    return totp.code_at(SECRET, time.time())


def test_a_current_code_is_accepted_and_clears_the_count(store):
    store.failures = 3

    assert _check(_now_code()) is second_factor.Check.ACCEPTED
    assert store.failures == 0 and store.cleared == 1


def test_a_locked_account_is_not_even_tried(store):
    """Otherwise the lock would only slow the guesses it is meant to stop,
    and a right guess during the lock would still open the door."""
    store.locked = True

    assert _check(_now_code()) is second_factor.Check.LOCKED
    assert store.claimed == [], "the code was checked while locked"


def test_a_wrong_code_is_counted(store):
    assert _check("000000") is second_factor.Check.REFUSED
    assert store.failures == 1


def test_the_same_code_twice_is_refused_the_second_time(store):
    code = _now_code()

    assert _check(code) is second_factor.Check.ACCEPTED
    assert _check(code) is second_factor.Check.REFUSED


def test_the_fifth_wrong_code_locks_and_tells_the_owners(store):
    verdicts = [_check("000000") for _ in range(second_factor.MAX_FAILURES)]

    assert verdicts[:-1] == [second_factor.Check.REFUSED] * (second_factor.MAX_FAILURES - 1)
    assert verdicts[-1] is second_factor.Check.LOCKED
    told = {chat for chat, _ in store.sent}
    assert _OWNER.chat_id in told, "the owner, on a different account, must hear"
    assert _PERSON.chat_id in told


def test_no_one_is_told_before_the_lock(store):
    for _ in range(second_factor.MAX_FAILURES - 1):
        _check("000000")

    assert store.sent == []


def test_an_owner_locked_out_is_not_told_twice(store):
    store.owners = [_OWNER]
    person = second_factor.get_staff_by_id

    async def owner_is_the_person(_conn, _staff_id):
        return _OWNER

    second_factor.get_staff_by_id = owner_is_the_person  # type: ignore[assignment]
    try:
        for _ in range(second_factor.MAX_FAILURES):
            asyncio.run(second_factor.check(_Conn(), _OWNER.id, "000000"))
    finally:
        second_factor.get_staff_by_id = person  # type: ignore[assignment]

    assert [chat for chat, _ in store.sent] == [_OWNER.chat_id]


def test_a_failed_notice_does_not_undo_the_lock(store, monkeypatch):
    async def broken(*_args, **_kwargs):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(second_factor.telegram, "send_message", broken)
    verdicts = [_check("000000") for _ in range(second_factor.MAX_FAILURES)]

    assert verdicts[-1] is second_factor.Check.LOCKED


def test_a_recovery_code_works_once_the_enrolment_is_confirmed(store):
    assert _check("a1b2c3d4") is second_factor.Check.ACCEPTED
    assert _check("a1b2c3d4") is second_factor.Check.REFUSED


def test_a_recovery_code_does_not_confirm_an_enrolment(store):
    """Signing in by recovery code before the app ever worked would leave the
    person without a working authenticator and meet the lock the next day."""
    store.confirmed = False

    assert _check("a1b2c3d4") is second_factor.Check.REFUSED
    assert "A1B2C3D4" in store.recovery, "a recovery code was spent for nothing"


def test_the_first_working_code_confirms_the_enrolment(store):
    store.confirmed = False

    assert _check(_now_code()) is second_factor.Check.ACCEPTED
    assert store.confirmed_calls == 1


def test_a_six_digit_typo_never_spends_a_recovery_code(store):
    store.recovery = {"123456"}

    _check("123456")

    assert "123456" in store.recovery
