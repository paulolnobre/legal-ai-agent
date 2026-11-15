"""Persistence and semantic search of legal documents (pgvector).

Project rule: history/records in Postgres are NEVER deleted by a user command.
The audit log is append-only, and the agent never removes anything on its own.
A document leaves the archive only through the owner's explicit, confirmed
command, which is the routine `delete_document` below serves.

Embeddings are written and compared through explicit `%s::vector` casts, so no
pgvector client adapter is needed (embeddings are never read back as values).
"""
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from core.config import settings
from core.legislation import group_sql
from core.repositories.teams import Scope, visibility_clause
from core.models import (
    DocumentRecord,
    DocumentSource,
    DocumentSummary,
    OfficialSource,
    SearchHit,
)

logger = logging.getLogger(__name__)

_MAX_TOP_K = 50


def _vec(embedding) -> str:
    """Serialize an embedding as a pgvector literal: '[0.1,0.2,...]'.

    Using a literal + '::vector' cast in SQL avoids the adapter and works with
    plain Python lists (what the embeddings API returns)."""
    return "[" + ",".join(str(float(x)) for x in embedding) + "]"


async def find_document_by_sha(conn, sha256):
    async with conn.cursor() as cur:
        await cur.execute("SELECT id FROM documents WHERE content_sha256 = %s", (sha256,))
        row = await cur.fetchone()
    return row[0] if row else None


async def create_document(
    conn,
    title,
    full_text,
    sha256,
    doc_type=None,
    matter=None,
    source_filename=None,
    source_storage_key=None,
    source_sha256=None,
    source_size_bytes=None,
    source_wrapped_key=None,
    source_kek_version=None,
    source_url=None,
    uploaded_by=None,
    visibility: str = "private",
    team_id: int | None = None,
    commit=True,
):
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO documents
                (title, doc_type, matter, source_filename, content_sha256, full_text,
                 source_storage_key, source_sha256, source_size_bytes,
                 source_wrapped_key, source_kek_version, source_url,
                 uploaded_by, visibility, team_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                title,
                doc_type,
                matter,
                source_filename,
                sha256,
                full_text,
                source_storage_key,
                source_sha256,
                source_size_bytes,
                source_wrapped_key,
                source_kek_version,
                source_url,
                uploaded_by,
                visibility,
                team_id,
            ),
        )
        row = await cur.fetchone()
        if commit:
            await conn.commit()
        return row[0]


async def add_chunks(conn, document_id, chunks, embeddings, commit=True):
    """chunks: list[str]; embeddings: list[list[float]] (same order)."""
    if len(chunks) != len(embeddings):
        raise ValueError("chunks and embeddings must have the same length")

    async with conn.cursor() as cur:
        for idx, (content, emb) in enumerate(zip(chunks, embeddings)):
            await cur.execute(
                """
                INSERT INTO document_chunks (document_id, chunk_index, content, embedding)
                VALUES (%s, %s, %s, %s::vector)
                """,
                (document_id, idx, content, _vec(emb)),
            )
        if commit:
            await conn.commit()


async def replace_document_text(conn, document_id, *, full_text, content_sha256,
                                chunks, embeddings, commit=True) -> bool:
    """Swap a document's extracted text and its indexed chunks, keeping its id.

    The id is what the audit log, the access log and every past answer point
    at, so a corrected text must land on the same row rather than on a new one
    beside the old. Returns False when there is no such document.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE documents SET full_text = %s, content_sha256 = %s WHERE id = %s",
            (full_text, content_sha256, document_id),
        )
        if cur.rowcount == 0:
            return False
        await cur.execute("DELETE FROM document_chunks WHERE document_id = %s", (document_id,))
    await add_chunks(conn, document_id, chunks, embeddings, commit=False)
    if commit:
        await conn.commit()
    return True


async def replace_document_original(conn, document_id, *, source_filename,
                                    source_storage_key, source_sha256,
                                    source_size_bytes, source_wrapped_key=None,
                                    source_kek_version=None, commit=True) -> None:
    """Point a document at a new original, for when its official page changed.

    Unlike `set_document_source`, this overwrites what is there: the stored
    original is the version the text was extracted from, and a text taken
    from a newer page must not keep offering the older file through /arquivo.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE documents
            SET source_filename = %s, source_storage_key = %s,
                source_sha256 = %s, source_size_bytes = %s,
                source_wrapped_key = %s, source_kek_version = %s
            WHERE id = %s
            """,
            (source_filename, source_storage_key, source_sha256, source_size_bytes,
             source_wrapped_key, source_kek_version, document_id),
        )
    if commit:
        await conn.commit()


async def get_official_source(conn, document_id) -> OfficialSource | None:
    """The official page a document is kept current against, if any."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, title, source_url, source_checked_at FROM documents WHERE id = %s",
            (document_id,),
        )
        row = await cur.fetchone()
    if not row:
        return None
    return OfficialSource(id=row[0], title=row[1], source_url=row[2], checked_at=row[3])


async def link_official_source(conn, document_id, url: str, commit=True) -> bool:
    """Point an already-ingested document at the official page it came from.

    Only when it has none: a link a person set with /atualizar, and the check
    time that goes with it, are not overwritten by an ingestion that happens
    to carry a URL.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE documents SET source_url = %s WHERE id = %s AND source_url IS NULL",
            (url, document_id),
        )
        linked = bool(cur.rowcount)
    if commit:
        await conn.commit()
    return linked


async def mark_source_checked(conn, document_id, *, source_url=None, commit=True) -> None:
    """Record a comparison with the official page, linking it when it is new.

    Only the URL that was passed is written. Without one, the link already on
    the row stays as it is.
    """
    async with conn.cursor() as cur:
        if source_url is None:
            await cur.execute(
                "UPDATE documents SET source_checked_at = NOW() WHERE id = %s",
                (document_id,),
            )
        else:
            await cur.execute(
                """
                UPDATE documents SET source_checked_at = NOW(), source_url = %s
                WHERE id = %s
                """,
                (source_url, document_id),
            )
    if commit:
        await conn.commit()


async def list_due_for_refresh(conn, *, older_than_days: int) -> list[OfficialSource]:
    """Linked documents not compared with their page for the given days.

    Never compared comes first, then the longest waiting, so a failing page
    cannot keep the others from their turn.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id, title, source_url, source_checked_at FROM documents
            WHERE source_url IS NOT NULL
              AND (source_checked_at IS NULL
                   OR source_checked_at < NOW() - make_interval(days => %s))
            ORDER BY source_checked_at NULLS FIRST, id
            """,
            (older_than_days,),
        )
        rows = await cur.fetchall()
    return [
        OfficialSource(id=row[0], title=row[1], source_url=row[2], checked_at=row[3])
        for row in rows
    ]


async def search_chunks(conn, query_embedding, top_k=8, matter=None,
                        scope: Scope | None = None) -> list[SearchHit]:
    """Find the nearest chunks this reader may see, optionally within a matter.

    The visibility filter belongs here, in the statement, and not in whatever
    reads the hits afterwards: a passage that reaches the model has already
    been quoted, and an answer cannot un-quote a client's contract.
    """
    top_k = max(1, min(int(top_k), _MAX_TOP_K))
    vec = _vec(query_embedding)
    visible, visible_params = visibility_clause(scope or Scope())
    clauses = [visible]
    params: list = [vec, *visible_params]
    if matter:
        clauses.append("d.matter = %s")
        params.append(matter)
    params.append(top_k)

    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT c.id, c.document_id, d.title, d.doc_type, d.matter,
                   c.content, c.embedding <=> %s::vector AS distance, d.visibility
            FROM document_chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE {' AND '.join(clauses)}
            ORDER BY distance
            LIMIT %s
            """,
            params,
        )
        rows = await cur.fetchall()

    return [
        SearchHit(
            chunk_id=r[0], document_id=r[1], title=r[2], doc_type=r[3],
            matter=r[4], content=r[5], distance=float(r[6]), visibility=r[7],
        )
        for r in rows
    ]


async def get_document(conn, document_id, scope: Scope | None = None) -> DocumentRecord | None:
    """One document, if this reader may see it. Without a scope: the firm's only."""
    visible, params = visibility_clause(scope or Scope())
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT d.id, d.title, d.doc_type, d.matter, d.source_filename,
                   d.source_storage_key, d.source_sha256, d.source_size_bytes, d.full_text
            FROM documents d WHERE d.id = %s AND {visible}
            """,
            [document_id, *params],
        )
        row = await cur.fetchone()
    if not row:
        return None
    return DocumentRecord(
        id=row[0], title=row[1], doc_type=row[2], matter=row[3],
        source_filename=row[4], source_storage_key=row[5], source_sha256=row[6],
        source_size_bytes=row[7], full_text=row[8],
    )


@dataclass(frozen=True)
class DocumentCard:
    """Everything a document's page shows, read together."""

    id: int
    title: str
    doc_type: str | None
    matter: str | None
    created_at: datetime | None
    visibility: str
    team_name: str | None
    uploader_name: str | None
    source_filename: str | None
    source_size_bytes: int | None
    has_original: bool
    sphere: str | None = None


async def document_card(conn, document_id, scope: Scope | None = None) -> DocumentCard | None:
    """One document's page, in one scoped statement.

    One statement and not several reads, because reads made one after the
    other can straddle a change: a document moved into someone else's
    private folder between a scoped read and an unscoped one would have had
    its new holder's name shown to a reader who can no longer see it. A
    single statement sees one moment, and the scope governs all of it.
    """
    visible, params = visibility_clause(scope or Scope())
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT d.id, d.title, d.doc_type, d.matter, d.created_at, d.visibility,
                   t.name, s.name, d.source_filename, d.source_size_bytes,
                   d.source_storage_key IS NOT NULL
            FROM documents d
            LEFT JOIN teams t ON t.id = d.team_id
            LEFT JOIN staff s ON s.id = d.uploaded_by
            WHERE d.id = %s AND {visible}
            """,
            [document_id, *params],
        )
        row = await cur.fetchone()
    if not row:
        return None
    return DocumentCard(
        id=row[0], title=row[1], doc_type=row[2], matter=row[3], created_at=row[4],
        visibility=row[5], team_name=row[6], uploader_name=row[7],
        source_filename=row[8], source_size_bytes=row[9], has_original=bool(row[10]),
    )


@dataclass(frozen=True)
class DocumentPreview:
    """A document's page in the explorer: its card, the start of its text,
    and where it sits, for the way back and for moving it."""

    card: DocumentCard
    team_id: int | None
    preview: str
    more: bool


async def document_preview(conn, document_id, scope: Scope, *,
                           chars: int = 600) -> DocumentPreview | None:
    """`document_card` and the first `chars` characters of the text, in one
    scoped statement. The text is cut by the database: a statute can be
    megabytes, and only its opening is shown here."""
    visible, params = visibility_clause(scope)
    chars = max(1, min(int(chars), 5000))
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT d.id, d.title, d.doc_type, d.matter, d.created_at, d.visibility,
                   t.name, s.name, d.source_filename, d.source_size_bytes,
                   d.source_storage_key IS NOT NULL,
                   CASE WHEN d.visibility = 'team' THEN d.team_id END,
                   substring(d.full_text FROM 1 FOR %s),
                   char_length(d.full_text) > %s, d.sphere
            FROM documents d
            LEFT JOIN teams t ON t.id = d.team_id
            LEFT JOIN staff s ON s.id = d.uploaded_by
            WHERE d.id = %s AND {visible}
            """,
            [chars, chars, document_id, *params],
        )
        row = await cur.fetchone()
    if not row:
        return None
    card = DocumentCard(
        id=row[0], title=row[1], doc_type=row[2], matter=row[3], created_at=row[4],
        visibility=row[5], team_name=row[6], uploader_name=row[7],
        source_filename=row[8], source_size_bytes=row[9], has_original=bool(row[10]),
        sphere=row[14],
    )
    return DocumentPreview(card=card, team_id=row[11], preview=row[12] or "",
                           more=bool(row[13]))


@dataclass(frozen=True)
class DocumentText:
    """The start of a document's text, for a person to read, and its length."""

    id: int
    title: str
    text: str
    length: int


async def read_document_text(conn, document_id, *, limit: int,
                             scope: Scope | None = None) -> DocumentText | None:
    """A document's text for reading on a page, cut in the query.

    Cut by Postgres rather than in Python, so a statute of several megabytes
    never crosses into this process to be thrown away. The length comes back
    too, so the page can say how much it left out.
    """
    visible, params = visibility_clause(scope or Scope())
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT d.id, d.title, left(coalesce(d.full_text, ''), %s),
                   length(coalesce(d.full_text, ''))
            FROM documents d WHERE d.id = %s AND {visible}
            """,
            [max(0, int(limit)), document_id, *params],
        )
        row = await cur.fetchone()
    if not row:
        return None
    return DocumentText(id=row[0], title=row[1], text=row[2], length=int(row[3]))


async def get_document_summary(conn, document_id,
                               scope: Scope | None = None) -> DocumentSummary | None:
    """Identify one document this reader may see, without loading its text.

    Resolving "resuma o doc [2]" only needs the id and the title, and full_text
    can be megabytes, so it stays where it is until something actually reads it.
    """
    visible, params = visibility_clause(scope or Scope())
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT d.id, d.title, d.doc_type, d.matter, d.created_at
            FROM documents d WHERE d.id = %s AND {visible}
            """,
            [document_id, *params],
        )
        row = await cur.fetchone()
    if not row:
        return None
    return DocumentSummary(id=row[0], title=row[1], doc_type=row[2], matter=row[3],
                           created_at=row[4])


async def get_document_source(conn, document_id,
                              scope: Scope | None = None) -> DocumentSource | None:
    """Return original-file metadata without loading the legal document text."""
    visible, params = visibility_clause(scope or Scope())
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT d.id, d.source_filename, d.source_storage_key, d.source_sha256,
                   d.source_size_bytes, d.source_wrapped_key
            FROM documents d
            WHERE d.id = %s AND d.source_storage_key IS NOT NULL AND {visible}
            """,
            [document_id, *params],
        )
        row = await cur.fetchone()
    if not row:
        return None
    return DocumentSource(
        id=row[0], source_filename=row[1], source_storage_key=row[2],
        source_sha256=row[3], source_size_bytes=row[4],
        source_wrapped_key=bytes(row[5]) if row[5] else None,
    )


async def set_document_source(
    conn,
    document_id,
    *,
    source_filename: str,
    source_storage_key: str,
    source_sha256: str,
    source_size_bytes: int,
    source_wrapped_key: bytes | None = None,
    source_kek_version: int | None = None,
    commit: bool = True,
) -> None:
    """Attach an original to a legacy document that only had extracted text."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE documents
            SET source_filename = %s,
                source_storage_key = %s,
                source_sha256 = %s,
                source_size_bytes = %s,
                source_wrapped_key = %s,
                source_kek_version = %s
            WHERE id = %s AND source_storage_key IS NULL
            """,
            (source_filename, source_storage_key, source_sha256, source_size_bytes,
             source_wrapped_key, source_kek_version, document_id),
        )
    if commit:
        await conn.commit()


async def ensure_document_storage_schema(conn) -> None:
    """Migrate existing databases created before original-file storage existed."""
    async with conn.cursor() as cur:
        await cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_storage_key TEXT")
        await cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_sha256 VARCHAR(64)")
        await cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_size_bytes BIGINT")
        # The wrapped data key of a document kept in the bucket. Its presence
        # is what says where the original lives: bucket objects are always
        # encrypted and volume files never are, so a second column naming the
        # backend would only be a second thing to fall out of agreement.
        # Deleting this row destroys the only copy of that key, which is what
        # makes the object left in the bucket unrecoverable.
        await cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_wrapped_key BYTEA")
        await cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_kek_version INTEGER")
        await cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_url TEXT")
        await cur.execute(
            "ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_checked_at TIMESTAMPTZ"
        )
        # Legislation's sphere (federal, estadual, municipal), shown on its
        # card. Legislation has no client; this is what says where it applies.
        await cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS sphere TEXT")
        # Filtering the catalogue by time and kind is the normal way to read it
        # once the archive is real; listing it whole is the exception.
        await cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_created_at ON documents(created_at DESC)"
        )
        await cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_doc_type ON documents(doc_type)"
        )
    await conn.commit()


async def list_documents(conn, matter=None, limit=50,
                         scope: Scope | None = None) -> list[DocumentSummary]:
    visible, params = visibility_clause(scope or Scope())
    clauses = [visible]
    if matter:
        clauses.append("d.matter = %s")
        params.append(matter)
    params.append(limit)
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT d.id, d.title, d.doc_type, d.matter, d.created_at
            FROM documents d
            WHERE {' AND '.join(clauses)}
            ORDER BY d.created_at DESC LIMIT %s
            """,
            params,
        )
        rows = await cur.fetchall()
    return [
        DocumentSummary(id=r[0], title=r[1], doc_type=r[2], matter=r[3], created_at=r[4])
        for r in rows
    ]


async def search_documents(
    conn,
    *,
    matter: str | None = None,
    doc_type: str | None = None,
    title: str | None = None,
    since=None,
    until=None,
    limit: int = 50,
    scope: Scope | None = None,
    visibility: str | None = None,
) -> list[DocumentSummary]:
    """List the corpus narrowed by the filters a lawyer actually asks for.

    Listing everything stops working as soon as the archive is real, so the
    catalogue is filtered instead: by client or matter, by kind of document, by
    words in the title, and by a window in time. Text filters match loosely and
    ignore case and accents, because nobody types the matter exactly as it was
    filed.
    """
    visible, visible_params = visibility_clause(scope or Scope())
    clauses: list[str] = [visible]
    params: list = list(visible_params)

    # unaccent() is not assumed to be installed; translate() folds the vowels
    # that actually differ in Brazilian legal vocabulary.
    fold = "translate(lower({0}), 'áàâãäéèêëíìîïóòôõöúùûüç', 'aaaaaeeeeiiiiooooouuuuc')"
    for column, value in (("d.matter", matter), ("d.doc_type", doc_type), ("d.title", title)):
        if value:
            clauses.append(f"{fold.format(column)} LIKE {fold.format('%s')}")
            params.append(f"%{value.strip()}%")
    if since is not None:
        clauses.append("d.created_at >= %s")
        params.append(since)
    if until is not None:
        clauses.append("d.created_at < %s")
        params.append(until)
    # Narrows what the scope already allows; it never widens it, because the
    # scope's clause is the first one and every clause is ANDed.
    if visibility is not None:
        clauses.append("d.visibility = %s")
        params.append(visibility)

    where = f"WHERE {' AND '.join(clauses)}"
    params.append(max(1, min(int(limit), 200)))

    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT d.id, d.title, d.doc_type, d.matter, d.created_at, d.visibility
            FROM documents d
            {where}
            ORDER BY d.created_at DESC
            LIMIT %s
            """,
            params,
        )
        rows = await cur.fetchall()
    return [
        DocumentSummary(id=r[0], title=r[1], doc_type=r[2], matter=r[3], created_at=r[4],
                        visibility=r[5])
        for r in rows
    ]


# ── The explorer: folders, then clients, then documents ─────────────────────
# Every read below starts from the reader's scope and only narrows it. A
# folder is (kind, team id): ("private", None), ("team", 10), ("firm", None).
# A client that is NULL or only blanks is "Sem cliente", the same way in the
# counts and in the listing, so a tile never promises documents its page
# does not show.

FolderKind = Literal["private", "team", "firm"]
_NO_CLIENT = "(d.matter IS NULL OR btrim(d.matter) = '')"
_NOT_LEGISLATION = f"{group_sql()} IS NULL"


def _in_folder(kind: FolderKind, team_id: int | None) -> tuple[str, list]:
    if kind == "team":
        return "d.visibility = 'team' AND d.team_id = %s", [team_id]
    if kind == "private":
        return "d.visibility = 'private'", []
    return "d.visibility = 'firm'", []


def _like_literal(text: str) -> str:
    """Typed text as a LIKE pattern that matches it literally, anywhere."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@dataclass(frozen=True)
class ExplorerCard:
    """A document as a card: what it is and which folder it sits in."""

    id: int
    title: str
    doc_type: str | None
    matter: str | None
    created_at: datetime | None
    visibility: str
    team_id: int | None
    team_name: str | None
    sphere: str | None = None


_CARD_COLUMNS = """d.id, d.title, d.doc_type, d.matter, d.created_at, d.visibility,
                   CASE WHEN d.visibility = 'team' THEN d.team_id END, t.name, d.sphere"""
# Newest first, and the id breaks ties, so a page boundary never repeats or
# skips a document that shares its timestamp with another.
_CARD_ORDER = "d.created_at DESC NULLS LAST, d.id DESC"


def _card(row) -> ExplorerCard:
    return ExplorerCard(id=row[0], title=row[1] or "", doc_type=row[2], matter=row[3],
                        created_at=row[4], visibility=row[5], team_id=row[6],
                        team_name=row[7] if row[5] == "team" else None, sphere=row[8])


async def folder_counts(conn, scope: Scope) -> dict[tuple[str, int | None], int]:
    """How many documents this reader sees in each folder."""
    visible, params = visibility_clause(scope)
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT d.visibility, CASE WHEN d.visibility = 'team' THEN d.team_id END,
                   count(*)
            FROM documents d WHERE {visible} GROUP BY 1, 2
            """,
            params,
        )
        return {(r[0], r[1]): int(r[2]) for r in await cur.fetchall()}


async def client_counts(conn, scope: Scope, kind: FolderKind,
                        team_id: int | None = None) -> list[tuple[str | None, int]]:
    """Each client or case in one folder with its count; None is "Sem cliente",
    listed last. The name is returned as stored, so the link built from it
    matches it exactly. Legislation is not here: it belongs to no client and
    is counted by `legislation_counts`."""
    visible, params = visibility_clause(scope)
    folder, folder_params = _in_folder(kind, team_id)
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT client, n FROM (
                SELECT CASE WHEN {_NO_CLIENT} THEN NULL ELSE d.matter END AS client,
                       count(*) AS n
                FROM documents d WHERE {visible} AND {folder} AND {_NOT_LEGISLATION}
                GROUP BY 1
            ) c
            ORDER BY client IS NULL, lower(client), client
            """,
            [*params, *folder_params],
        )
        return [(r[0], int(r[1])) for r in await cur.fetchall()]


async def documents_in(conn, scope: Scope, kind: FolderKind, team_id: int | None = None,
                       *, client: str | None = None, no_client: bool = False,
                       group: str | None = None, page: int = 1,
                       per_page: int = 48) -> tuple[list[ExplorerCard], bool]:
    """One page of one client's documents in one folder, or of one kind of
    legislation (`group`), and whether more follow.

    The client matches exactly, unlike the loose catalogue search: the tile
    for "Acme" must not also list "Acme Holdings". Legislation is never under
    a client, whatever its `matter` says.
    """
    visible, params = visibility_clause(scope)
    folder, folder_params = _in_folder(kind, team_id)
    clauses, extra = [visible, folder], [*params, *folder_params]
    if group is not None:
        clauses.append(f"{group_sql()} = %s")
        extra.append(group)
    elif no_client:
        clauses += [_NO_CLIENT, _NOT_LEGISLATION]
    else:
        clauses += ["d.matter = %s", _NOT_LEGISLATION]
        extra.append(client)
    per_page = max(1, min(int(per_page), 200))
    extra += [per_page + 1, (max(1, int(page)) - 1) * per_page]
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT {_CARD_COLUMNS}
            FROM documents d LEFT JOIN teams t ON t.id = d.team_id
            WHERE {' AND '.join(clauses)}
            ORDER BY {_CARD_ORDER} LIMIT %s OFFSET %s
            """,
            extra,
        )
        rows = await cur.fetchall()
    return [_card(r) for r in rows[:per_page]], len(rows) > per_page


async def legislation_counts(conn, scope: Scope, kind: FolderKind,
                             team_id: int | None = None) -> dict[str, int]:
    """How many documents of each kind of legislation one folder holds."""
    visible, params = visibility_clause(scope)
    folder, folder_params = _in_folder(kind, team_id)
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT g, count(*) FROM (
                SELECT {group_sql()} AS g FROM documents d WHERE {visible} AND {folder}
            ) x WHERE g IS NOT NULL GROUP BY g
            """,
            [*params, *folder_params],
        )
        return {r[0]: int(r[1]) for r in await cur.fetchall()}


async def search_everywhere(conn, scope: Scope, text: str, *, page: int = 1,
                            per_page: int = 48) -> tuple[list[ExplorerCard], bool]:
    """Documents whose title, client or type contains the text, in every
    folder this reader sees, folding case and accents as the catalogue does.
    The text is matched literally: a typed % or _ is not a wildcard."""
    visible, params = visibility_clause(scope)
    fold = "translate(lower({0}), 'áàâãäéèêëíìîïóòôõöúùûüç', 'aaaaaeeeeiiiiooooouuuuc')"
    pattern = _like_literal(text.strip())
    matches = " OR ".join(
        f"{fold.format(column)} LIKE {fold.format('%s')} ESCAPE '\\'"
        for column in ("d.title", "d.matter", "d.doc_type")
    )
    per_page = max(1, min(int(per_page), 200))
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT {_CARD_COLUMNS}
            FROM documents d LEFT JOIN teams t ON t.id = d.team_id
            WHERE {visible} AND ({matches})
            ORDER BY {_CARD_ORDER} LIMIT %s OFFSET %s
            """,
            [*params, pattern, pattern, pattern, per_page + 1,
             (max(1, int(page)) - 1) * per_page],
        )
        rows = await cur.fetchall()
    return [_card(r) for r in rows[:per_page]], len(rows) > per_page


async def count_documents(conn, scope: Scope | None = None) -> int:
    """How large this reader's archive is, so a filtered list says what it left out."""
    visible, params = visibility_clause(scope or Scope())
    async with conn.cursor() as cur:
        await cur.execute(f"SELECT count(*) FROM documents d WHERE {visible}", params)
        row = await cur.fetchone()
    return int(row[0]) if row else 0


@dataclass(frozen=True)
class DocumentDeletion:
    """What leaving the archive removed, and what it deliberately did not."""

    status: Literal["deleted", "not_found"]
    title: str = ""
    # The original to remove from disk, or None when there is nothing to remove
    # or another document still needs it.
    storage_key: str | None = None
    # Set when that original is in the bucket: the caller needs it to know
    # which store to delete from, for the same reason a reader does.
    wrapped_key: bytes | None = None
    shared_original: bool = False


async def delete_document(conn, document_id, *, commit: bool = True) -> DocumentDeletion:
    """Remove one document and its chunks, and say which file is now orphaned.

    The chunks go with it through the schema's ON DELETE CASCADE, so the corpus
    cannot keep answering from a document the firm deleted.

    The file on disk is not touched here. Deciding it is safe to unlink is a
    database question, answered in this transaction; doing it is a filesystem
    action that cannot be rolled back, so it belongs to the caller, after the
    commit. The order matters: a file removed before a failed commit would
    leave a live document whose original is gone, silently, while a row removed
    before a failed unlink leaves an orphan file that can still be found and
    reported.

    The key is the sha256 of the file's own bytes, so two documents can share
    one file. The sibling check runs after the DELETE and inside the same
    transaction, which is why it needs no `id <> ...`: the deleted row is
    already gone from this transaction's view.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM documents WHERE id = %s "
            "RETURNING title, source_storage_key, source_wrapped_key",
            (document_id,),
        )
        row = await cur.fetchone()
        if not row:
            return DocumentDeletion(status="not_found")
        title, storage_key = row[0], row[1]
        wrapped_key = bytes(row[2]) if row[2] else None
        shared = False
        if storage_key:
            await cur.execute(
                "SELECT 1 FROM documents WHERE source_storage_key = %s LIMIT 1",
                (storage_key,),
            )
            shared = await cur.fetchone() is not None
    if commit:
        await conn.commit()
    return DocumentDeletion(
        status="deleted",
        title=title or "",
        storage_key=None if shared else (storage_key or None),
        wrapped_key=None if shared else wrapped_key,
        shared_original=shared,
    )


# The column is VARCHAR(500); a longer title would be truncated by the database
# rather than refused, and a silent truncation is a lie about what was saved.
MAX_TITLE_CHARS = 500


# Both are VARCHAR(50) and VARCHAR(255). A longer value would be truncated by
# the database rather than refused, and a silent truncation is a lie about what
# was saved.
MAX_DOC_TYPE_CHARS = 50
MAX_MATTER_CHARS = 255


async def update_document_fields(
    conn,
    document_id,
    *,
    title: str | None = None,
    doc_type: str | None = None,
    matter: str | None = None,
    commit: bool = True,
    scope: Scope | None = None,
) -> bool:
    """Change how a document is described. False when there is no such document.

    Only what is named moves: a field left as None is not touched, so setting a
    type cannot quietly erase a matter somebody filled in last week.

    Nothing about the document itself changes. The text, its chunks, the
    original file, the ingestion date and the audit trail are all untouched.
    This is the label on the folder, not what is inside it, which is why the
    date is the one thing here nobody can edit: it records when the archive
    received the document, and that already happened.
    """
    fields: list[str] = []
    values: list[object] = []
    for column, value, limit in (
        ("title", title, MAX_TITLE_CHARS),
        ("doc_type", doc_type, MAX_DOC_TYPE_CHARS),
        ("matter", matter, MAX_MATTER_CHARS),
    ):
        if value is not None:
            fields.append(f"{column} = %s")
            values.append(value[:limit])
    if not fields:
        return False

    # With a scope, the write itself refuses a document this person can no
    # longer see: the read that found it may be a moment old, and a move or
    # a takeover in between must not leave the edit going through.
    if scope is not None:
        visible, params = visibility_clause(scope)
        statement = f"UPDATE documents d SET {', '.join(fields)} WHERE d.id = %s AND {visible}"
        arguments = (*values, document_id, *params)
    else:
        statement = f"UPDATE documents SET {', '.join(fields)} WHERE id = %s"
        arguments = (*values, document_id)
    async with conn.cursor() as cur:
        await cur.execute(statement, arguments)
        changed = cur.rowcount > 0
    if commit:
        await conn.commit()
    return changed


async def move_document(conn, document_id, *, visibility: str, team_id: int | None,
                        scope: Scope, commit: bool = True) -> bool:
    """Move one document to another folder, if this person may see it at all.

    The scope is required rather than optional: moving is the one write that
    decides who else can read a document, and a caller that forgot to say on
    whose behalf it is moving must not silently move somebody else's.
    """
    visible, params = visibility_clause(scope)
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            UPDATE documents d SET visibility = %s, team_id = %s
            WHERE d.id = %s AND {visible}
            """,
            [visibility, team_id, document_id, *params],
        )
        moved = bool(cur.rowcount)
    if commit:
        await conn.commit()
    return moved


async def private_documents_of(conn, staff_id: int) -> list[tuple[int, str]]:
    """What only this person can see, which is what goes dark when they leave.

    Used before deactivating somebody, so the firm is told what it is about to
    lose reach of rather than discovering it the day a client asks.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, title FROM documents "
            "WHERE uploaded_by = %s AND visibility = 'private' ORDER BY id",
            (staff_id,),
        )
        return [(row[0], row[1] or "") for row in await cur.fetchall()]


async def take_over_document(conn, document_id, *, new_owner_id: int,
                             commit: bool = True) -> tuple[str, int | None] | None:
    """Move one document into the owner's own folder, whoever had it.

    **This is the one read-write in the archive that ignores visibility on
    purpose**, and it is the only one that should. Everything else takes a
    `Scope` precisely so it cannot reach a folder that is not the caller's; if
    this took one, it could not do the job it exists for.

    The job: the case file belongs to the firm, not to the lawyer who uploaded
    it. When somebody leaves, falls ill or dies, their private folder becomes
    unreachable by everyone, and the client the firm still owes a duty to is
    the one who pays for that. Losing a case file to a resignation is a harm
    the firm answers for.

    What keeps this from being a back door into every private folder is not
    the code, which cannot tell a legitimate takeover from a nosy one. It is
    that every use writes to the audit log and the previous holder is told.
    The owner may reach, but cannot browse, and cannot do it quietly.

    Returns the title and the previous holder, or None when there is no such
    document. The destination is the owner's personal folder rather than the
    firm's shelf: taking something out of somebody's private folder and
    publishing it to everyone in one step is two decisions, and the second one
    belongs to `/compartilhar`, which already exists.
    """
    async with conn.cursor() as cur:
        # Read before write, in two statements rather than a subquery inside
        # RETURNING. Whether such a subquery sees the row before or after the
        # update is snapshot semantics, and the previous holder is exactly the
        # value the notification and the audit entry depend on: too important
        # to rest on a rule somebody has to remember.
        await cur.execute(
            "SELECT title, uploaded_by FROM documents WHERE id = %s FOR UPDATE",
            (document_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        title, previous = row[0] or "", row[1]
        await cur.execute(
            "UPDATE documents SET uploaded_by = %s, visibility = 'private', "
            "team_id = NULL WHERE id = %s",
            (new_owner_id, document_id),
        )
    if commit:
        await conn.commit()
    return title, previous


async def folder_of(conn, document_id) -> tuple[str, int | None, int | None] | None:
    """Where a document lives: visibility, team, and who put it there."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT visibility, team_id, uploaded_by FROM documents WHERE id = %s",
            (document_id,),
        )
        row = await cur.fetchone()
    return (row[0], row[1], row[2]) if row else None


@dataclass(frozen=True)
class Folder:
    """Where a document lives and who put it there, as its reader may see it."""

    visibility: str
    team_id: int | None
    uploaded_by: int | None
    title: str


async def folder_in_scope(conn, document_id, scope: Scope, *,
                          lock: bool = False) -> Folder | None:
    """`folder_of` for a person: None when this reader may not see the document.

    Moving decides from this, so being allowed into the destination never
    lets anyone move a document out of a folder they cannot see. `lock` holds
    the row until the caller commits, so the folder decided on is the folder
    moved from.
    """
    visible, params = visibility_clause(scope)
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT d.visibility, d.team_id, d.uploaded_by, d.title FROM documents d "
            f"WHERE d.id = %s AND {visible}{' FOR UPDATE' if lock else ''}",
            [document_id, *params],
        )
        row = await cur.fetchone()
    return Folder(row[0], row[1], row[2], row[3] or "") if row else None


# The columns the explorer's "Editar" may write: how a document is described,
# never where it is, who sent it or what it says.
EDITABLE_FIELDS = ("title", "doc_type", "matter", "sphere")


async def set_fields(conn, document_id, fields: dict[str, str | None], *, scope: Scope,
                     expected: dict[str, str | None] | None = None,
                     commit: bool = True) -> bool:
    """Write these description fields of one document, and only these.

    None empties a field (no type, no client). Scoped like `move_document`:
    the page that sent the change may be minutes old, and a write that forgot
    on whose behalf it runs must not reach a document that person cannot see.

    `expected` is what each field held when that page was opened (None for
    empty). If any of them has changed since, nothing is written and False
    comes back: somebody else edited it meanwhile, and their edit stands.
    """
    expected = expected or {}
    unknown = (set(fields) | set(expected)) - set(EDITABLE_FIELDS)
    if not fields or unknown:
        raise ValueError(f"not editable here: {sorted(unknown) or 'nothing named'}")
    columns = [column for column in EDITABLE_FIELDS if column in fields]
    visible, params = visibility_clause(scope)
    guards, guard_params = [], []
    for column in (c for c in EDITABLE_FIELDS if c in expected):
        if expected[column] is None:
            guards.append(f"(d.{column} IS NULL OR btrim(d.{column}) = '')")
        else:
            guards.append(f"d.{column} = %s")
            guard_params.append(expected[column])
    unchanged = "".join(f" AND {guard}" for guard in guards)
    async with conn.cursor() as cur:
        await cur.execute(
            f"UPDATE documents d SET {', '.join(f'{c} = %s' for c in columns)} "
            f"WHERE d.id = %s AND {visible}{unchanged}",
            [*(fields[c] for c in columns), document_id, *params, *guard_params],
        )
        changed = bool(cur.rowcount)
    if commit:
        await conn.commit()
    return changed


async def rename_document(conn, document_id, title: str, *, commit: bool = True,
                          scope: Scope | None = None) -> bool:
    """Give one document a different name, the common case of the above."""
    return await update_document_fields(
        conn, document_id, title=title, commit=commit, scope=scope,
    )


async def list_doc_types(conn, limit: int = 30, scope: Scope | None = None) -> list[tuple[str, int]]:
    """The types actually in use, most populated first.

    Offered when somebody is asked for one, so the archive converges on the
    words the firm already uses instead of collecting "lei", "Lei", "LEI" and
    "legislacao" as four different kinds of thing.
    """
    visible, params = visibility_clause(scope or Scope())
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT d.doc_type, count(*) AS total
            FROM documents d
            WHERE d.doc_type IS NOT NULL AND d.doc_type <> '' AND {visible}
            GROUP BY d.doc_type
            ORDER BY total DESC, d.doc_type
            LIMIT %s
            """,
            [*params, max(1, min(int(limit), 100))],
        )
        return [(row[0], int(row[1])) for row in await cur.fetchall()]


async def list_matters(conn, limit: int = 40, scope: Scope | None = None) -> list[tuple[str, int]]:
    """The matters actually in use, most populated first, to suggest filters."""
    visible, params = visibility_clause(scope or Scope())
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT d.matter, count(*) AS total
            FROM documents d
            WHERE d.matter IS NOT NULL AND d.matter <> '' AND {visible}
            GROUP BY d.matter
            ORDER BY total DESC, d.matter
            LIMIT %s
            """,
            [*params, max(1, min(int(limit), 100))],
        )
        return [(row[0], int(row[1])) for row in await cur.fetchall()]


async def purge_expired_access_log(conn, retention_days: int) -> int:
    """Delete audit entries older than the retention period, and redact queries.

    The log must prove who read which document — that is the point of an audit
    trail. The question text is different: it carries the client's own words, so
    it is dropped as soon as the shorter retention window closes, while the
    access record itself is kept for the full period.

    This is the administrative routine the module rule allows: it is scheduled by
    the operator, never reachable from a chat command, and it never touches
    documents or chunks.
    """
    if retention_days <= 0:
        return 0
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE document_access_log
            SET query = NULL
            WHERE query IS NOT NULL
              AND created_at < NOW() - (%s || ' days')::interval
            """,
            (settings.query_retention_days,),
        )
        redacted = cur.rowcount or 0
        await cur.execute(
            "DELETE FROM document_access_log WHERE created_at < NOW() - (%s || ' days')::interval",
            (retention_days,),
        )
        deleted = cur.rowcount or 0
    await conn.commit()
    if redacted or deleted:
        logger.info(
            "Access log retention: %s queries redacted, %s entries deleted",
            redacted, deleted,
        )
    return deleted


async def log_access(conn, action, staff_id=None, chat_id=None, query=None,
                     document_ids=None, commit=True):
    """Record document access. Append-only — never deleted."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO document_access_log
                (staff_id, chat_id, action, query, document_ids)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (staff_id, str(chat_id) if chat_id else None, action, query, document_ids),
        )
        if commit:
            await conn.commit()
