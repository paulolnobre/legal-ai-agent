"""What has already been read out of the document bucket.

Without this, every import would download every object to discover that most of
them are already in the archive. Ingestion does deduplicate, by the sha256 of
the extracted text, but it can only do that after the bytes have arrived and
been parsed, which is the expensive half.

The key is the name and the content's ETag together, not the name alone. A
name can be reused: somebody uploads a corrected version of `contrato.pdf` over
the old one, and that is different content with a different ETag, so it is
imported again, as it should be.
Keying by name would have silently ignored the correction.

Failures are recorded too. A file that cannot be parsed will fail identically on
every run, and re-downloading it nightly forever is the kind of waste nobody
notices; the row says what happened so the import can skip it and the person can
be told once.

A file skipped for its format or its size is not recorded, and that asymmetry is
the point. Those decisions are made from the listing, cost nothing to reach, and
are judgements about the rules rather than about the file. When `.html` joined
the allowlist, every `.html` already written down as skipped would have stayed
skipped forever, because this table would have said it was dealt with.
"""
import logging

logger = logging.getLogger(__name__)


async def ensure_inbox_schema(conn) -> None:
    """Create the ledger on a database that already exists.

    init.sql runs only when PostgreSQL initializes an empty data directory, so
    anything added after the first deploy needs this path too. That is not
    hypothetical here: it has already cost this project one broken deploy.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS inbox_imports (
                file_id     VARCHAR(200) PRIMARY KEY,
                file_name   VARCHAR(500) NOT NULL,
                document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
                outcome     VARCHAR(32) NOT NULL,
                detail      VARCHAR(200),
                imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CHECK (outcome IN ('ingested', 'duplicate', 'skipped', 'failed'))
            )
            """
        )
        # The enumeration is restated as a named constraint even though the
        # CREATE above already carries it. The CREATE only runs when the table
        # is absent, so on the day somebody adds an outcome, every database that
        # already has this table would keep the narrower check and refuse the
        # new value in production while the tests pass. Installing the
        # constraint by name now is what makes that widening safe later.
        await cur.execute(
            """
            ALTER TABLE inbox_imports
            DROP CONSTRAINT IF EXISTS inbox_imports_outcome_check
            """
        )
        await cur.execute(
            """
            ALTER TABLE inbox_imports
            DROP CONSTRAINT IF EXISTS inbox_imports_outcome_allowed
            """
        )
        await cur.execute(
            """
            ALTER TABLE inbox_imports
            ADD CONSTRAINT inbox_imports_outcome_allowed
            CHECK (outcome IN ('ingested', 'duplicate', 'skipped', 'failed'))
            """
        )
        # The import reads by time to report what it did last, never by name.
        await cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_inbox_imports_imported_at
            ON inbox_imports(imported_at DESC)
            """
        )
        # Rows the current rule would never write. Earlier versions recorded a
        # skip for format or size, which meant widening the allowlist could not
        # reach the files it was widened for: three .html statutes sat in the
        # firm's bucket marked as dealt with while .html was being added. The
        # decision costs nothing to make again, so the contradiction is removed
        # rather than left for somebody to notice.
        await cur.execute("DELETE FROM inbox_imports WHERE outcome = 'skipped'")
    await conn.commit()


async def known_file_ids(conn, file_ids: list[str]) -> set[str]:
    """Which of these have been dealt with before, in one query.

    A document deleted from the archive leaves its row here with a NULL
    document_id, which is deliberate: the firm deleted it on purpose, and an
    import that helpfully put it back would be a bug with a very bad shape.
    """
    if not file_ids:
        return set()
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT file_id FROM inbox_imports WHERE file_id = ANY(%s)",
            (list(file_ids),),
        )
        return {row[0] for row in await cur.fetchall()}


async def record(
    conn,
    *,
    file_id: str,
    file_name: str,
    outcome: str,
    document_id: int | None = None,
    detail: str | None = None,
    commit: bool = True,
) -> None:
    """Write down what happened to one object, once."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO inbox_imports (file_id, file_name, document_id, outcome, detail)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (file_id) DO UPDATE
            SET outcome = EXCLUDED.outcome,
                detail = EXCLUDED.detail,
                document_id = COALESCE(EXCLUDED.document_id, inbox_imports.document_id),
                imported_at = NOW()
            """,
            (file_id, file_name[:500], document_id, outcome, (detail or "")[:200] or None),
        )
    if commit:
        await conn.commit()


async def last_imports(conn, limit: int = 10) -> list[tuple[str, str, str]]:
    """The most recent decisions, for a report that says what was skipped."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT file_name, outcome, COALESCE(detail, '')
            FROM inbox_imports
            ORDER BY imported_at DESC
            LIMIT %s
            """,
            (max(1, min(int(limit), 50)),),
        )
        return [(row[0], row[1], row[2]) for row in await cur.fetchall()]


__all__ = ["ensure_inbox_schema", "known_file_ids", "last_imports", "record"]
