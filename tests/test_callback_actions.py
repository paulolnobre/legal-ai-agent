"""A press is claimed once, by its owner, and repeats are answered not rerun.

The SQL itself was exercised against PostgreSQL; what is checked here is the
decision built on top of it — which outcome a press gets, and that the token
carried through Telegram reveals nothing and fits the payload.
"""
import asyncio

from core.repositories import callback_actions


class _FakeCursor:
    """Enough of a psycopg cursor to drive claim(): queued rows, recorded SQL."""

    def __init__(self, rows: list, log: list):
        self._rows = rows
        self._log = log
        self.rowcount = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self._log.append((" ".join(sql.split()), params))
        self.rowcount = 1 if self._rows and self._rows[0] is not None else 0

    async def fetchone(self):
        return self._rows.pop(0) if self._rows else None


class _FakeConn:
    def __init__(self, rows: list):
        self.rows = rows
        self.log: list = []
        self.commits = 0

    def cursor(self):
        return _FakeCursor(self.rows, self.log)

    async def commit(self):
        self.commits += 1


def _claim(rows):
    conn = _FakeConn(rows)
    claim = asyncio.run(callback_actions.claim(conn, "tok", chat_id=111, staff_id=9,
                                               channel="telegram"))
    return claim, conn


def test_the_first_press_is_granted_and_carries_what_to_run():
    claim, conn = _claim([("access.review", "approve", "NS-1")])

    assert claim.outcome == "granted"
    assert claim.may_run
    assert (claim.action, claim.operation, claim.resource) == (
        "access.review", "approve", "NS-1",
    )
    # The grant came from the conditional UPDATE, not from a read-then-write.
    assert "UPDATE callback_actions" in conn.log[0][0]
    assert len(conn.log) == 1, "granting must not need a second statement"


def test_a_repeat_press_returns_the_known_result_instead_of_running_again():
    claim, _ = _claim([
        None,  # the UPDATE matched nothing: it is no longer pending
        ("111", 9, "used", "access.review", "approve", "NS-1", "já autorizado", False, "telegram"),
    ])

    assert claim.outcome == "already_used"
    assert not claim.may_run, "a repeat must never run the action again"
    assert claim.result == "já autorizado"


def test_an_expired_press_is_refused_and_says_so():
    claim, _ = _claim([
        None,
        ("111", 9, "pending", "access.review", "approve", "NS-1", None, True, "telegram"),
    ])

    assert claim.outcome == "expired"
    assert not claim.may_run


def test_a_retired_sibling_reports_itself_cancelled():
    claim, _ = _claim([
        None,
        ("111", 9, "cancelled", "access.review", "approve", "NS-1", None, False, "telegram"),
    ])

    assert claim.outcome == "cancelled"
    assert not claim.may_run


def test_an_unknown_token_fails_closed():
    claim, _ = _claim([None, None])

    assert claim.outcome == "unknown"
    assert not claim.may_run
    assert claim.action is None, "an unknown token must not describe anything"


def test_a_token_belonging_to_someone_else_is_refused():
    claim, _ = _claim([
        None,
        ("222", 9, "pending", "access.review", "approve", "NS-1", None, False, "telegram"),  # other chat
    ])

    assert claim.outcome == "forbidden"
    assert not claim.may_run
    assert claim.action is None, "a refused press must not leak what it would have done"


def test_a_token_for_another_person_in_the_same_chat_is_refused():
    claim, _ = _claim([
        None,
        ("111", 77, "pending", "access.review", "approve", "NS-1", None, False, "telegram"),  # other staff
    ])

    assert claim.outcome == "forbidden"


def test_the_token_is_opaque_and_fits_the_telegram_payload():
    conn = _FakeConn([])
    tokens = {
        asyncio.run(callback_actions.issue(
            conn, chat_id=111, staff_id=9, issued_role="admin",
            action="access.review", resource="NS-1", ttl_seconds=300, channel="telegram",
        ))
        for _ in range(50)
    }

    assert len(tokens) == 50, "tokens must not repeat"
    for token in tokens:
        assert len(token.encode()) <= 64, "callback_data is capped at 64 bytes"


def test_the_token_carries_nothing_about_the_matter(monkeypatch):
    """Nothing about the matter may be recoverable from what Telegram carries.

    This used to search real random tokens for "NS-1", and a random token of
    43 characters contains any given four once in roughly eight thousand
    runs: it failed CI on 2026-09-22 with nothing wrong. The property is that
    the token is the random value and nothing else, so that is what is pinned.
    """
    monkeypatch.setattr(callback_actions.secrets, "token_urlsafe", lambda _n: "RANDOM")

    token = asyncio.run(callback_actions.issue(
        _FakeConn([]), chat_id=111, staff_id=9, issued_role="admin",
        action="access.review", resource="NS-1", ttl_seconds=300, channel="telegram",
    ))

    assert token == "RANDOM"


def test_the_resource_travels_in_the_row_and_never_in_the_payload():
    conn = _FakeConn([])
    asyncio.run(callback_actions.issue(
        conn, chat_id=111, staff_id=9, issued_role="admin",
        action="access.review", resource="NS-SECRET", ttl_seconds=300, channel="telegram",
    ))

    sql, params = conn.log[0]
    assert "INSERT INTO callback_actions" in sql
    assert "NS-SECRET" in params, "the resource belongs in the row"
