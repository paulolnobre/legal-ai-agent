"""The web panel's conversation: its requests and the files it offers.

A message typed in the panel is a `panel_requests` row before it is a queue
entry. The row is what makes it run at most once (the worker starts it with a
conditional UPDATE, and a redelivery finds it already started), what keeps a
person to one request at a time (a partial unique index, so two tabs cannot
both get in), and what the page polls for its status.

A file the panel offers (an export, an official page saved to the archive) is
sealed with the documents' key, so no plaintext copy sits in the database or
its backups, and it is read only by its owner and only before it expires, both
in the query. Originals are never copied here: the panel links to the scoped
download, which decides at the moment of download.

Everything is by `staff_id`, and erasing a person (`staff.erase_person`)
deletes their rows outright.
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import secrets
import uuid
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from psycopg import errors
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from core import document_crypto
from core.config import settings
from core.repositories import conversation, panel_uploads

logger = logging.getLogger(__name__)

Status = Literal["sending", "queued", "deferred", "running", "done", "failed", "withdrawn"]
Kind = Literal["message", "button"]

# Serialises one person's file stores, so two at once cannot both fit under
# the live cap that only one of them fits under.
_FILES_LOCK = 0x70616E6C  # "panl"


class RequestGone(Exception):
    """The request was erased, or its person deactivated, while the worker
    served it: nothing more is written for it, and it stops."""


_served_request: ContextVar[UUID | None] = ContextVar("panel_served_request", default=None)


@contextlib.contextmanager
def serving_request(request_id: UUID):
    token = _served_request.set(request_id)
    try:
        yield
    finally:
        _served_request.reset(token)


def request_is_bound() -> bool:
    return _served_request.get() is not None


@contextlib.asynccontextmanager
async def publishing_current(conn):
    """Guard ancillary conversation writes as well as delivered turns."""
    request_id = _served_request.get()
    if request_id is None:
        yield
        return
    async with publishing(conn, request_id) as ok:
        if not ok:
            raise RequestGone
        yield


@dataclass(frozen=True)
class RequestRow:
    id: UUID
    kind: str
    status: Status
    created_at: datetime
    updated_at: datetime
    attachment_kind: str | None = None


async def ensure_panel_schema(conn) -> None:
    """Create the tables on a database that already exists (init.sql only
    runs on an empty data directory)."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS panel_requests (
                id             UUID PRIMARY KEY,
                staff_id       INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
                chat_id        VARCHAR(50) NOT NULL,
                kind           VARCHAR(8) NOT NULL,
                person_turn_id BIGINT,
                status         VARCHAR(10) NOT NULL DEFAULT 'sending',
                created_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
                updated_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
                started_at     TIMESTAMPTZ
            )
            """
        )
        # The end of the media stage's deadline (`begin_media`), on a table
        # created before it existed.
        await cur.execute(
            "ALTER TABLE panel_requests ADD COLUMN IF NOT EXISTS media_deadline_at TIMESTAMPTZ"
        )
        # Installed by name, so a value added later reaches a database that
        # already has the table (as pending_actions does).
        await cur.execute(
            "ALTER TABLE panel_requests DROP CONSTRAINT IF EXISTS panel_requests_kind_allowed"
        )
        await cur.execute(
            """
            ALTER TABLE panel_requests ADD CONSTRAINT panel_requests_kind_allowed
            CHECK (kind IN ('message', 'button'))
            """
        )
        await cur.execute(
            "ALTER TABLE panel_requests "
            "DROP CONSTRAINT IF EXISTS panel_requests_status_allowed"
        )
        await cur.execute(
            """
            ALTER TABLE panel_requests ADD CONSTRAINT panel_requests_status_allowed
            CHECK (status IN ('sending', 'queued', 'deferred', 'running', 'done',
                              'failed', 'withdrawn'))
            """
        )
        # One request on its way per person: the rule itself, not a check
        # that two tabs could both pass before either wrote.
        await cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS panel_requests_one_active
            ON panel_requests(staff_id)
            WHERE status IN ('sending', 'queued', 'deferred', 'running')
            """
        )
        await cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_panel_requests_staff_time
            ON panel_requests(staff_id, created_at DESC)
            """
        )
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS panel_files (
                token       TEXT PRIMARY KEY,
                staff_id    INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
                filename    TEXT NOT NULL,
                media_type  TEXT NOT NULL,
                sealed      BYTEA NOT NULL,
                wrapped_key BYTEA NOT NULL,
                content_sha TEXT NOT NULL,
                size_bytes  INTEGER NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at  TIMESTAMPTZ NOT NULL
            )
            """
        )
        await cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_panel_files_staff_expiry
            ON panel_files(staff_id, expires_at)
            """
        )
        # An attachment sent in the chat, sealed until the worker reads it
        # (see panel_uploads). After panel_requests, which it references.
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS panel_uploads (
                id          UUID PRIMARY KEY,
                staff_id    INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
                request_id  UUID REFERENCES panel_requests(id) ON DELETE CASCADE,
                kind        VARCHAR(8) NOT NULL,
                media_type  TEXT NOT NULL,
                size_bytes  INTEGER NOT NULL,
                sealed      BYTEA NOT NULL,
                wrapped_key BYTEA NOT NULL,
                content_sha TEXT NOT NULL,
                archive     BOOLEAN NOT NULL DEFAULT FALSE,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at  TIMESTAMPTZ NOT NULL
            )
            """
        )
        await cur.execute(
            "ALTER TABLE panel_uploads DROP CONSTRAINT IF EXISTS panel_uploads_kind_allowed"
        )
        await cur.execute(
            "ALTER TABLE panel_uploads DROP CONSTRAINT IF EXISTS panel_uploads_document_named"
        )
        # A document to archive (Task M.6): a longer kind, and its name.
        await cur.execute("ALTER TABLE panel_uploads ALTER COLUMN kind TYPE VARCHAR(8)")
        await cur.execute("ALTER TABLE panel_uploads ADD COLUMN IF NOT EXISTS filename TEXT")
        await cur.execute(
            """
            ALTER TABLE panel_uploads ADD CONSTRAINT panel_uploads_kind_allowed
            CHECK (kind IN ('photo', 'voice', 'audio', 'document'))
            """
        )
        await cur.execute(
            """
            ALTER TABLE panel_uploads ADD CONSTRAINT panel_uploads_document_named
            CHECK ((kind = 'document') = (filename IS NOT NULL))
            """
        )
        # One attachment per message.
        await cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS panel_uploads_one_per_request
            ON panel_uploads(request_id) WHERE request_id IS NOT NULL
            """
        )
        await cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_panel_uploads_staff_expiry
            ON panel_uploads(staff_id, expires_at)
            """
        )
    await conn.commit()


async def create_request(conn, *, staff_id: int, chat_id, kind: Kind,
                         text: str | None,
                         upload_id: UUID | None = None,
                         label: str | None = None) -> tuple[UUID, int] | None:
    """Write the request and the person's turn together, as `sending`, and
    give it the upload it names in the same transaction, so the worker never
    finds the request without its attachment.

    Returns (request id, turn id), the turn id 0 for a button press, which
    says nothing of its own. None when the person already has a request on
    its way: then nothing at all is written. Raises `UploadUnavailable` when
    the upload is not theirs to give (someone else's, taken, expired): then
    nothing is written either.

    `label` is what the person's bubble shows in place of the text, for a
    message sent from the chat's actions: the text stays the command.
    """
    request_id = uuid.uuid4()
    try:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO panel_requests (id, staff_id, chat_id, kind) "
                    "VALUES (%s, %s, %s, %s)",
                    (request_id, staff_id, str(chat_id), kind),
                )
                turn_id = 0
                if kind == "message":
                    turn_id = await conversation.insert_turn(
                        cur, staff_id=staff_id, chat_id=chat_id, speaker="person",
                        text=conversation.redact_access_links(text or ""),
                        channel="panel", request_id=request_id,
                    )
                    await cur.execute(
                        "UPDATE panel_requests SET person_turn_id = %s WHERE id = %s",
                        (turn_id, request_id),
                    )
                if upload_id is not None and not await panel_uploads.attach_upload(
                        conn, upload_id, staff_id=staff_id, request_id=request_id,
                        commit=False):
                    raise panel_uploads.UploadUnavailable
                if upload_id is not None and turn_id:
                    await cur.execute(
                        "UPDATE conversation_turns t SET extras = jsonb_build_object("
                        "'attachment', jsonb_strip_nulls(jsonb_build_object("
                        "'kind', u.kind, 'size_bytes', u.size_bytes, 'name', u.filename))) "
                        "FROM panel_uploads u WHERE u.id = %s AND t.id = %s AND t.staff_id = %s",
                        (upload_id, turn_id, staff_id),
                    )
                if label is not None and turn_id:
                    await cur.execute(
                        "UPDATE conversation_turns SET extras = COALESCE(extras, '{}'::jsonb) "
                        "|| jsonb_build_object('action', %s::text) WHERE id = %s AND staff_id = %s",
                        (label, turn_id, staff_id),
                    )
    except errors.UniqueViolation:
        await conn.commit()
        return None
    except panel_uploads.UploadUnavailable:
        await conn.commit()
        raise
    await conn.commit()
    return request_id, turn_id


async def _transition(conn, statement, params) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(statement, params)
        changed = (cur.rowcount or 0) == 1
    await conn.commit()
    return changed


async def mark_handed(conn, request_id: UUID, *, deferred: bool) -> bool:
    """The queue took it. Only a request still `sending` moves, so the
    endpoint never overwrites a worker that already started or ended it."""
    return await _transition(
        conn,
        "UPDATE panel_requests SET status = %s, updated_at = clock_timestamp() "
        "WHERE id = %s AND status = 'sending'",
        ("deferred" if deferred else "queued", request_id),
    )


async def start(conn, request_id: UUID) -> bool:
    """The worker takes it, at most once: committed on its own, so a retry
    or a redelivery after a crash finds it already started and does nothing.
    `sending` too: Redis can hand the payload to the worker before the
    endpoint has written `queued`."""
    return await _transition(
        conn,
        "UPDATE panel_requests SET status = 'running', started_at = clock_timestamp(), "
        "updated_at = clock_timestamp() "
        "WHERE id = %s AND status IN ('sending', 'queued', 'deferred')",
        (request_id,),
    )


async def begin_media(conn, request_id: UUID, seconds: float) -> None:
    """The worker starts reading this request's attachment, with `seconds`
    to do it: `settle` counts its grace from the end of that, so a long
    transcription is not given up on, and the person cannot send another
    message meanwhile."""
    await _transition(
        conn,
        "UPDATE panel_requests "
        "SET media_deadline_at = clock_timestamp() + make_interval(secs => %s), "
        "updated_at = clock_timestamp() WHERE id = %s AND status = 'running'",
        (float(seconds), request_id),
    )


@contextlib.asynccontextmanager
async def publishing(conn, request_id: UUID, *,
                     require_active: bool = True) -> AsyncIterator[bool]:
    """One write for a request the worker is serving, serialized against
    erasure: `async with publishing(conn, request_id) as ok:`.

    In its own transaction it locks the person's staff row `FOR SHARE`, then
    the request row `FOR UPDATE`, the order `erase_person` takes them in
    (staff `FOR UPDATE`, then the person's requests), so the two never
    deadlock. `ok` is False when the request is gone or its person is no
    longer active; the body then writes nothing. Either the erasure ran
    first (nothing more is written) or it waits for this write and then
    deletes what it finds. The body must not commit; the guard does.

    `require_active=False` only for telling a deactivated person so: the
    request must still exist, which erasure alone takes away.
    """
    # Whatever the connection left open is committed first, as every write
    # here has always done, so the locks below live exactly as long as this.
    if conn.info.transaction_status != TransactionStatus.IDLE:
        await conn.commit()
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute("SELECT staff_id FROM panel_requests WHERE id = %s",
                              (request_id,))
            row = await cur.fetchone()
            ok = False
            if row is not None:
                staff_id = int(row[0])
                await cur.execute("SELECT active FROM staff WHERE id = %s FOR SHARE",
                                  (staff_id,))
                person = await cur.fetchone()
                await cur.execute(
                    "SELECT 1 FROM panel_requests WHERE id = %s AND staff_id = %s FOR UPDATE",
                    (request_id, staff_id),
                )
                still = await cur.fetchone()
                ok = (still is not None and person is not None
                      and (bool(person[0]) or not require_active))
        yield ok


async def owner(conn, request_id: UUID) -> tuple[int, str] | None:
    """(staff id, chat id) of whoever wrote the request: the worker serves
    them, whatever the queued payload says."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT staff_id, chat_id FROM panel_requests WHERE id = %s", (request_id,))
        row = await cur.fetchone()
    return (int(row[0]), str(row[1])) if row else None


async def attachment_sent(conn, request_id: UUID) -> bool:
    """Whether this request was written with an attachment, whatever has
    become of the upload since. `create_request` records it on the person's
    turn (`extras.attachment`) in the same transaction that binds the upload,
    so a purged, expired or unopenable upload still reads as sent."""
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM panel_requests r JOIN conversation_turns t "
                "ON t.id = r.person_turn_id AND t.staff_id = r.staff_id "
                "WHERE r.id = %s AND t.extras->'attachment' IS NOT NULL",
                (request_id,))
            row = await cur.fetchone()
    return row is not None


async def message_writer(conn, request_id: UUID) -> int | None:
    """Staff id of whoever wrote this request, when it is a message; None for
    a button press, which is not a new answer, and for no such request. The
    kind is the one the intake stored, never read back from the payload."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT staff_id FROM panel_requests WHERE id = %s AND kind = 'message'",
            (request_id,))
        row = await cur.fetchone()
    return int(row[0]) if row else None


async def finish(conn, request_id: UUID, status: Literal["done", "failed"]) -> None:
    """End it. A request that already ended keeps its ending."""
    await _transition(
        conn,
        "UPDATE panel_requests SET status = %s, updated_at = clock_timestamp() "
        "WHERE id = %s AND status NOT IN ('done', 'failed', 'withdrawn')",
        (status, request_id),
    )


async def withdraw(conn, request_id: UUID, *, staff_id: int) -> bool:
    """The person takes back a request still waiting. Only theirs, and only
    before the worker started it: a withdrawn request never runs."""
    return await _transition(
        conn,
        "UPDATE panel_requests SET status = 'withdrawn', updated_at = clock_timestamp() "
        "WHERE id = %s AND staff_id = %s AND status IN ('queued', 'deferred')",
        (request_id, staff_id),
    )


async def settle(conn, staff_id: int) -> int:
    """Give up on this person's requests nobody will finish.

    `sending` past its grace: the process died between writing it and handing
    it to the queue. `running` past the worker's own time limit plus a
    margin, counted from the start or, for a request reading an attachment,
    from the end of its media stage's deadline: the worker died with it.
    Queued and deferred requests wait for the worker, however long that is
    (a deferred one waits for morning).
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE panel_requests SET status = 'failed', updated_at = clock_timestamp()
            WHERE staff_id = %s AND (
                (status = 'sending'
                 AND updated_at < NOW() - make_interval(secs => %s))
                OR (status = 'running'
                    AND COALESCE(media_deadline_at, started_at)
                        < NOW() - make_interval(secs => %s)))
            """,
            (staff_id, float(settings.panel_sending_grace_seconds),
             float(settings.panel_running_grace_seconds)),
        )
        settled = cur.rowcount or 0
    await conn.commit()
    return settled


async def in_flight(conn, staff_id: int) -> RequestRow | None:
    """This person's request on its way, after giving up on the stale ones."""
    await settle(conn, staff_id)
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id, kind, status, created_at, updated_at,
                   (SELECT u.kind FROM panel_uploads u WHERE u.request_id = panel_requests.id)
            FROM panel_requests
            WHERE staff_id = %s AND status IN ('sending', 'queued', 'deferred', 'running')
            ORDER BY created_at DESC LIMIT 1
            """,
            (staff_id,),
        )
        row = await cur.fetchone()
    return RequestRow(*row) if row else None


async def latest(conn, staff_id: int) -> RequestRow | None:
    """This person's newest request, whatever became of it, after giving up
    on the stale ones: what the page says about the last thing they sent."""
    await settle(conn, staff_id)
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id, kind, status, created_at, updated_at,
                   (SELECT u.kind FROM panel_uploads u WHERE u.request_id = panel_requests.id)
            FROM panel_requests
            WHERE staff_id = %s ORDER BY created_at DESC LIMIT 1
            """,
            (staff_id,),
        )
        row = await cur.fetchone()
    return RequestRow(*row) if row else None


async def fail_running_at_startup(conn) -> int:
    """A worker starting finds no request of its own running: whatever is
    marked running was left by one that died, and is never replayed."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE panel_requests SET status = 'failed', updated_at = clock_timestamp() "
            "WHERE status = 'running'"
        )
        failed = cur.rowcount or 0
    await conn.commit()
    return failed


async def purge_old_requests(conn, days: int) -> int:
    """Requests go with the conversation they belong to (query_retention_days)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM panel_requests WHERE created_at < NOW() - make_interval(days => %s) "
            "AND status IN ('done', 'failed', 'withdrawn')",
            (int(days),),
        )
        deleted = cur.rowcount or 0
    await conn.commit()
    return deleted


def buttons_extras(rows: list[list[tuple[str, str]]]) -> dict:
    """How a panel reply's buttons are kept in its turn's `extras`: rows of
    {label, token}, the tokens `callback_actions` issued for them."""
    return {"buttons": [[{"label": label, "token": token} for label, token in row]
                        for row in rows]}


async def button_turn_text(conn, *, staff_id: int, turn_id: int, token: str) -> str | None:
    """The text of this person's panel reply `turn_id` when it carries the
    button `token`, else None. A press is accepted only for a button the
    panel showed them, and its text is what a decision's rewrite keeps."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT text FROM conversation_turns
            WHERE id = %s AND staff_id = %s AND channel = 'panel' AND speaker = 'bot'
              AND extras -> 'buttons' @> %s
            """,
            (turn_id, staff_id, Jsonb([[{"token": token}]])),
        )
        row = await cur.fetchone()
    return str(row[0]) if row else None


async def store_file(conn, *, staff_id: int, filename: str, media_type: str,
                     data: bytes, commit: bool = True) -> str | None:
    """Seal a file for this person and return its token, or None when it is
    over the size of one file or would take them over their live total.
    `commit=False` inside a caller's transaction (`publishing`)."""
    if len(data) > settings.panel_file_max_bytes:
        return None
    content_sha = hashlib.sha256(data).hexdigest()
    sealed = document_crypto.seal(
        data, master_key=settings.doc_master_key, document_sha=content_sha,
    )
    token = secrets.token_urlsafe(32)
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (_FILES_LOCK, staff_id))
            await cur.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) FROM panel_files "
                "WHERE staff_id = %s AND expires_at > NOW()",
                (staff_id,),
            )
            row = await cur.fetchone()
            live = int(row[0]) if row else 0
            if live + len(data) > settings.panel_files_max_live_bytes:
                token = None
            else:
                await cur.execute(
                    """
                    INSERT INTO panel_files (token, staff_id, filename, media_type, sealed,
                                             wrapped_key, content_sha, size_bytes, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                            NOW() + make_interval(hours => %s))
                    """,
                    (token, staff_id, filename, media_type, sealed.blob, sealed.wrapped_key,
                     content_sha, len(data), int(settings.panel_file_ttl_hours)),
                )
    if commit:
        await conn.commit()
    return token


async def read_file(conn, token: str, *, staff_id: int) -> tuple[str, str, bytes] | None:
    """(filename, media type, bytes) for its owner, before it expires; None
    for anyone else, after expiry, or when the seal does not open."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT filename, media_type, sealed, wrapped_key, content_sha FROM panel_files
            WHERE token = %s AND staff_id = %s AND expires_at > NOW()
            """,
            (token, staff_id),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    filename, media_type, sealed, wrapped_key, content_sha = row
    try:
        data = document_crypto.open_sealed(
            bytes(sealed), bytes(wrapped_key), master_key=settings.doc_master_key,
            document_sha=content_sha,
        )
    except document_crypto.EncryptionError:
        logger.error("A panel file did not open: its seal or its key does not match")
        return None
    return filename, media_type, data


async def file_name(conn, token: str, staff_id: int) -> str | None:
    """The filename of this person's file while it lives, else None: the owner
    and expiry test of `read_file`, without opening the seal. For a request
    that needs to know the file has not lapsed, or what it is called, and
    nothing of what is inside."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT filename FROM panel_files WHERE token = %s AND staff_id = %s "
            "AND expires_at > NOW()",
            (token, staff_id),
        )
        row = await cur.fetchone()
    return None if row is None else str(row[0])


async def purge_expired_files(conn) -> int:
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM panel_files WHERE expires_at <= NOW()")
        deleted = cur.rowcount or 0
    await conn.commit()
    return deleted


__all__ = [
    "Kind", "RequestGone", "RequestRow", "Status", "attachment_sent", "begin_media",
    "buttons_extras",
    "create_request", "ensure_panel_schema", "file_name", "fail_running_at_startup", "finish", "in_flight",
    "latest", "mark_handed", "message_writer", "owner", "publishing", "purge_expired_files",
    "purge_old_requests", "read_file", "settle", "start", "store_file", "button_turn_text",
    "withdraw",
]
