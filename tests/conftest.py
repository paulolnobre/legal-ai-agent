"""Shared isolation for unit tests that exercise Telegram message handling."""
import asyncio
import os
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import LiteralString, cast

import pytest

from core.handlers import legal_message
from core.repositories.teams import Scope
from core.services.legal_assistant import graph
from core.repositories.callback_actions import CallbackClaim
from core.repositories.pending_actions import PendingAction


class InMemoryPendingActions:
    """The ``pending_actions`` table without a database behind it.

    It mirrors the real repository's contract rather than approximating it: one
    open step per chat, ``peek`` returns an expired step so the caller can say
    so, and ``consume`` refuses one. Expiry reads ``time.monotonic`` so the
    tests that fast-forward the clock keep working unchanged.
    """

    def __init__(self):
        self.rows: dict[str, dict] = {}

    def _seconds_left(self, row) -> float:
        return row["expires_at"] - time.monotonic()

    def _to_action(self, row) -> PendingAction:
        return PendingAction(
            kind=row["kind"],
            action=row["action"],
            approved=row["approved"],
            target_id=row["target_id"],
            seconds_left=self._seconds_left(row),
            args=row.get("args"),
            channel=row.get("channel", "telegram"),
        )

    async def remember(
        self, _conn, chat_id, *, kind, ttl_seconds,
        action=None, approved=None, target_id=None, args=None, channel="telegram",
    ) -> None:
        self.rows[str(chat_id)] = {
            "args": args,
            "channel": channel,
            "kind": kind,
            "action": action,
            "approved": approved,
            "target_id": target_id,
            "expires_at": time.monotonic() + ttl_seconds,
        }

    async def peek(self, _conn, chat_id) -> PendingAction | None:
        row = self.rows.get(str(chat_id))
        return self._to_action(row) if row else None

    async def consume(self, _conn, chat_id) -> PendingAction | None:
        row = self.rows.get(str(chat_id))
        if not row or self._seconds_left(row) <= 0:
            return None
        del self.rows[str(chat_id)]
        return self._to_action(row)

    async def forget(self, _conn, chat_id) -> None:
        self.rows.pop(str(chat_id), None)

    async def purge_expired(self, _conn) -> int:
        expired = [k for k, r in self.rows.items() if self._seconds_left(r) <= 0]
        for key in expired:
            del self.rows[key]
        return len(expired)

    # -- helpers for assertions -------------------------------------------
    def waiting(self, chat_id) -> bool:
        """True when this chat has a step still answerable."""
        row = self.rows.get(str(chat_id))
        return bool(row) and self._seconds_left(row) > 0

    def kind(self, chat_id) -> str | None:
        row = self.rows.get(str(chat_id))
        return row["kind"] if row else None


class InMemoryCallbackActions:
    """The ``callback_actions`` table without a database behind it.

    Mirrors the contract rather than approximating it: a claim succeeds once,
    an expired or retired row is reported with its own outcome, and a token
    belonging to another chat or person is refused without describing itself.
    """

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self._n = 0

    async def issue(self, _conn, *, chat_id, staff_id, issued_role, action,
                    ttl_seconds, channel, operation=None, resource=None) -> str:
        self._n += 1
        token = f"tok{self._n:03d}" + "x" * 36
        self.rows[token] = {
            "chat_id": str(chat_id), "staff_id": staff_id, "channel": channel,
            "issued_role": issued_role, "action": action, "operation": operation,
            "resource": resource, "state": "pending", "result": None,
            "reference": None, "expires_at": time.monotonic() + ttl_seconds,
        }
        return token

    async def claim(self, _conn, token, *, chat_id, staff_id, channel) -> CallbackClaim:
        row = self.rows.get(token)
        if not row:
            return CallbackClaim(outcome="unknown")
        if row["chat_id"] != str(chat_id) or row["staff_id"] != staff_id:
            return CallbackClaim(outcome="forbidden")
        if row["channel"] != channel:
            return CallbackClaim(outcome="other_channel")
        if row["state"] == "cancelled":
            return CallbackClaim(outcome="cancelled", action=row["action"],
                                 operation=row["operation"], resource=row["resource"])
        if row["state"] == "used":
            return CallbackClaim(outcome="already_used", action=row["action"],
                                 operation=row["operation"], resource=row["resource"],
                                 result=row["result"])
        if row["expires_at"] <= time.monotonic():
            return CallbackClaim(outcome="expired", action=row["action"],
                                 operation=row["operation"], resource=row["resource"])
        row["state"] = "used"
        return CallbackClaim(outcome="granted", action=row["action"],
                             operation=row["operation"], resource=row["resource"])

    async def attach_reference(self, _conn, tokens, *, reference: str) -> None:
        for token in tokens:
            if token in self.rows:
                self.rows[token]["reference"] = str(reference)

    async def record_result(self, _conn, token, result: str, *,
                            unless_recorded: bool = False) -> None:
        if token in self.rows and not (unless_recorded and self.rows[token]["result"]):
            self.rows[token]["result"] = result

    async def cancel_siblings(self, _conn, *, resource, chat_id=None,
                              keep_token=None) -> list[tuple[str, str, str]]:
        retired = []
        for token, row in self.rows.items():
            if row["resource"] != resource or row["state"] != "pending":
                continue
            if chat_id is not None and row["chat_id"] != str(chat_id):
                continue
            if keep_token is not None and token == keep_token:
                continue
            row["state"] = "cancelled"
            if row["reference"] is not None:
                retired.append((row["chat_id"], row["channel"], row["reference"]))
        return retired

    async def purge_expired(self, _conn, *, keep_days: int = 7) -> int:
        return 0

    # -- helpers for assertions -------------------------------------------
    def live(self) -> list[str]:
        return [t for t, r in self.rows.items() if r["state"] == "pending"]

    def state(self, token: str) -> str | None:
        row = self.rows.get(token)
        return row["state"] if row else None


@pytest.fixture
def callback_store(monkeypatch: pytest.MonkeyPatch) -> InMemoryCallbackActions:
    """Swap the persisted button store for an in-memory one, per test."""
    store = InMemoryCallbackActions()
    monkeypatch.setattr(legal_message, "callback_actions", store)
    return store


@pytest.fixture
def pending_store(monkeypatch: pytest.MonkeyPatch) -> InMemoryPendingActions:
    """Swap the persisted step store for an in-memory one, per test."""
    store = InMemoryPendingActions()
    monkeypatch.setattr(legal_message, "pending_actions", store)
    return store


@pytest.fixture
def isolate_message_rate_limits(
    monkeypatch: pytest.MonkeyPatch,
    pending_store: InMemoryPendingActions,
):
    """Keep unit tests independent from Redis and cross-test limiter state."""
    async def allow_shared_rate_limit(*args, **kwargs):
        return True, False

    monkeypatch.setattr(
        legal_message,
        "_limiter",
        SimpleNamespace(check=lambda _chat_id: (True, False)),
    )
    monkeypatch.setattr(legal_message.cache, "rate_limit_hit", allow_shared_rate_limit)
    yield


@pytest.fixture(autouse=True)
def private_media_dir(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Decrypted audio never goes to the machine's real /tmp/panel-media
    from a test, and a worker started by a test sweeps only its own."""
    from core.config import settings as live_settings

    monkeypatch.setattr(live_settings, "panel_media_dir", str(tmp_path / "panel-media"))


@pytest.fixture(autouse=True)
def no_live_djen(monkeypatch: pytest.MonkeyPatch):
    """The DJEN is a live public API, and a test must not reach it by accident.

    Any question with a process number in it would otherwise query the CNJ from
    the test run. The tests about the DJEN turn it back on and fake the call.
    """
    from core.config import settings as live_settings

    monkeypatch.setattr(live_settings, "djen_enabled", False)


@pytest.fixture(autouse=True)
def bind_viewer_scope(monkeypatch: pytest.MonkeyPatch):
    """The folders a handler test's asker may see, without a database.

    `_serving` builds the scope from the teams table, and most handler tests
    pass a string where a connection goes. This gives them the scope of a
    person in no team, which is what a lawyer starts as; a test about teams
    overrides it.
    """
    async def scope_for(_conn, staff_id):
        return Scope(staff_id=staff_id)

    monkeypatch.setattr(legal_message, "scope_for", scope_for)
    monkeypatch.setattr(graph, "scope_for", scope_for)


class PanelDatabase:
    """A schema of its own on the real Postgres, built from init.sql and the
    startup bootstraps and seeded with three people, for the panel's tests."""

    SEED = """
    INSERT INTO staff (id, chat_id, name, role) VALUES
        (1, '100', 'Paulo', 'owner'), (4, '400', 'Rui', 'advogado'),
        (5, '500', 'Ana', 'estagiario'), (6, NULL, 'Sem chat', 'advogado');
    """

    def __init__(self, url: str):
        self.url = url
        self.schema = f"paneltest_{uuid.uuid4().hex[:12]}"

    def _in_schema(self, statement: LiteralString):
        from psycopg import sql

        return sql.SQL(statement).format(schema=sql.Identifier(self.schema))

    async def connect(self):
        import psycopg

        conn = await psycopg.AsyncConnection.connect(self.url)
        async with conn.cursor() as cur:
            await cur.execute(self._in_schema("SET search_path TO {schema}, public"))
        await conn.commit()
        return conn

    async def _vector_in_public(self, conn) -> None:
        """The extension belongs to the database, not this run (see test_web_files)."""
        async with conn.cursor() as cur:
            await cur.execute("SELECT pg_advisory_lock(hashtext('test-vector-extension'))")
            try:
                await cur.execute(
                    "SELECT n.nspname FROM pg_extension e "
                    "JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'vector'"
                )
                row = await cur.fetchone()
                if row is None:
                    await cur.execute("CREATE EXTENSION vector SCHEMA public")
                elif row[0] != "public":
                    await cur.execute("ALTER EXTENSION vector SET SCHEMA public")
                await conn.commit()
            finally:
                await cur.execute(
                    "SELECT pg_advisory_unlock(hashtext('test-vector-extension'))")
                await conn.commit()

    async def build(self, conn, *, init_sql: bool = True, bootstraps: bool = True) -> None:
        from core.repositories import (conversation, documents, panel, pending_actions, staff,
                                       teams, web_access)

        await self._vector_in_public(conn)
        async with conn.cursor() as cur:
            await cur.execute(self._in_schema("DROP SCHEMA IF EXISTS {schema} CASCADE"))
            await cur.execute(self._in_schema("CREATE SCHEMA {schema}"))
            await cur.execute(self._in_schema("SET search_path TO {schema}, public"))
            if init_sql:
                # The repository's own file, not input: safe to run as SQL.
                await cur.execute(cast(LiteralString, Path("infra/init.sql").read_text(
                    encoding="utf-8")))
        await conn.commit()
        if init_sql and bootstraps:
            await staff.ensure_access_schema(conn)
            await documents.ensure_document_storage_schema(conn)
            await teams.ensure_team_schema(conn)
            await pending_actions.ensure_pending_actions_schema(conn)
            await conversation.ensure_conversation_schema(conn)
            await panel.ensure_panel_schema(conn)
            await web_access.ensure_web_access_schema(conn)
            async with conn.cursor() as cur:
                await cur.execute(cast(LiteralString, self.SEED))
            await conn.commit()

    async def drop(self) -> None:
        conn = await self.connect()
        try:
            async with conn.cursor() as cur:
                await cur.execute(self._in_schema("DROP SCHEMA IF EXISTS {schema} CASCADE"))
            await conn.commit()
        finally:
            await conn.close()

    def run(self, body, *, init_sql: bool = True, bootstraps: bool = True):
        """Build the schema, run `body(conn)`, drop the schema."""
        async def main():
            conn = await self.connect()
            try:
                await self.build(conn, init_sql=init_sql, bootstraps=bootstraps)
                return await body(conn)
            finally:
                await conn.close()
                await self.drop()

        return asyncio.run(main())


@pytest.fixture
def db() -> PanelDatabase:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("needs a real Postgres: set TEST_DATABASE_URL")
    return PanelDatabase(url)
