"""Moving a document between folders, against a real Postgres.

Who may move what is a security property decided by queries: which rows the
scoped read and the scoped UPDATE let through, and whether the move and its
audit entry commit together. The handler tests mock those statements and would
pass with the visibility guard gone, so the rule is proven here.

The cast (that of tests/test_web_files.py, plus an admin): Rui and Ana share
the team Cível, Bia is in Trabalhista, Caio is an admin in no team, Paulo is
the owner and sees no one's private folder.

Skipped without TEST_DATABASE_URL; CI provides one.
"""
import asyncio
import os
import uuid
from pathlib import Path
from typing import LiteralString, cast

import pytest
from psycopg import sql

from core.models import StaffMember
from core.repositories import documents
from core.repositories.documents import ensure_document_storage_schema
from core.repositories.staff import ensure_access_schema
from core.repositories.teams import ensure_team_schema, scope_for
from core.services import folders

URL = os.environ.get("TEST_DATABASE_URL")
SCHEMA = f"folderstest_{uuid.uuid4().hex[:12]}"

pytestmark = pytest.mark.skipif(
    not URL, reason="needs a real Postgres: set TEST_DATABASE_URL",
)

PAULO = StaffMember(id=1, chat_id="100", name="Paulo", role="owner")
RUI = StaffMember(id=4, chat_id="400", name="Rui", role="advogado")
ANA = StaffMember(id=5, chat_id="500", name="Ana", role="estagiario")
BIA = StaffMember(id=6, chat_id="600", name="Bia", role="advogado")
CAIO = StaffMember(id=7, chat_id="700", name="Caio", role="admin")
LAW, ACME, BETA, PETITION = 101, 102, 103, 104
CIVEL = folders.team(10, "Cível")
TRABALHISTA = folders.team(11, "Trabalhista")

_SEED = """
INSERT INTO staff (id, chat_id, name, role) VALUES
    (1, '100', 'Paulo', 'owner'), (4, '400', 'Rui', 'advogado'),
    (5, '500', 'Ana', 'estagiario'), (6, '600', 'Bia', 'advogado'),
    (7, '700', 'Caio', 'admin');
INSERT INTO teams (id, name) VALUES (10, 'Cível'), (11, 'Trabalhista');
INSERT INTO team_members (team_id, staff_id) VALUES (10, 4), (10, 5), (11, 6);
INSERT INTO documents (id, title, doc_type, matter, full_text, uploaded_by,
                       visibility, team_id, content_sha256) VALUES
    (101, 'Lei do Inquilinato', 'lei', NULL, 'Art. 1', 1, 'firm', NULL, 'a1'),
    (102, 'Contrato Acme', 'contrato', 'Acme', 'Cláusula', 4, 'private', NULL, 'a2'),
    (103, 'Parecer Beta', 'parecer', 'Beta', 'Segredo', 6, 'private', NULL, 'a3'),
    (104, 'Petição Cível', 'peticao', 'Gama', 'Excelentíssimo', 5, 'team', 10, 'a4');
"""


def _in_schema(statement: LiteralString) -> sql.Composed:
    return sql.SQL(statement).format(schema=sql.Identifier(SCHEMA))


async def _vector_in_public(conn) -> None:
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
            await cur.execute("SELECT pg_advisory_unlock(hashtext('test-vector-extension'))")
            await conn.commit()


def _run(body):
    """Build this run's schema from init.sql and the bootstraps, then run."""
    import psycopg

    async def main():
        assert URL
        async with await psycopg.AsyncConnection.connect(URL) as conn:
            await _vector_in_public(conn)
            async with conn.cursor() as cur:
                await cur.execute(_in_schema("DROP SCHEMA IF EXISTS {schema} CASCADE"))
                await cur.execute(_in_schema("CREATE SCHEMA {schema}"))
                await cur.execute(_in_schema("SET search_path TO {schema}, public"))
                # The repository's own file, not input: safe to run as SQL.
                await cur.execute(cast(LiteralString,
                                       Path("infra/init.sql").read_text(encoding="utf-8")))
            await conn.commit()
            for bootstrap in (ensure_access_schema, ensure_document_storage_schema,
                              ensure_team_schema):
                await bootstrap(conn)
            async with conn.cursor() as cur:
                await cur.execute(_SEED)
            await conn.commit()
            try:
                return await body(conn)
            finally:
                await conn.rollback()
                async with conn.cursor() as cur:
                    await cur.execute(_in_schema("DROP SCHEMA IF EXISTS {schema} CASCADE"))
                await conn.commit()

    return asyncio.run(main())


async def _committed(statement: LiteralString, params=()) -> list[tuple]:
    """Read through a second connection: only what was committed is there."""
    import psycopg

    assert URL
    async with await psycopg.AsyncConnection.connect(URL) as other:
        async with other.cursor() as cur:
            await cur.execute(_in_schema("SET search_path TO {schema}, public"))
            await cur.execute(statement, params)
            return list(await cur.fetchall())


async def _where(_conn, document_id) -> tuple[str, int | None, int | None]:
    rows = await _committed(
        "SELECT visibility, team_id, uploaded_by FROM documents WHERE id = %s",
        (document_id,),
    )
    return rows[0][0], rows[0][1], rows[0][2]


async def _shares(_conn) -> list[tuple[int, str, list[int]]]:
    rows = await _committed(
        "SELECT staff_id, query, document_ids FROM document_access_log "
        "WHERE action = 'share' ORDER BY id"
    )
    return [(r[0], r[1], list(r[2] or [])) for r in rows]


async def _move(conn, who, document_id, destination):
    scope = await scope_for(conn, who.id)
    return await folders.move(conn, who, scope, document_id, destination,
                              chat_id=who.chat_id)


# ── the scoped read ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("who, document_id, visible", [
    (RUI, ACME, True),        # his own private folder
    (BIA, ACME, False),       # somebody else's private folder
    (PAULO, ACME, False),     # not even the owner's: that door is /assumir
    (ANA, PETITION, True),    # her team's
    (BIA, PETITION, False),   # a team she is not in
    (CAIO, LAW, True),        # the firm's, for everyone
])
def test_the_folder_is_read_only_through_the_readers_scope(who, document_id, visible):
    async def body(conn):
        return await documents.folder_in_scope(conn, document_id,
                                               await scope_for(conn, who.id))

    folder = _run(body)

    assert (folder is not None) is visible


def test_the_scoped_update_refuses_a_document_the_mover_cannot_see():
    """The decision already reads through the scope; the UPDATE keeps its own
    guard, so a caller that skipped the decision still cannot move it."""
    async def body(conn):
        moved = await documents.move_document(
            conn, ACME, visibility="firm", team_id=None,
            scope=await scope_for(conn, BIA.id),
        )
        return moved, await _where(conn, ACME)

    moved, where = _run(body)

    assert moved is False
    assert where == ("private", None, RUI.id)


# ── who may move what ────────────────────────────────────────────────────────


@pytest.mark.parametrize("who, document_id, destination, after", [
    # An intern moves what she sent.
    (ANA, PETITION, folders.FIRM, ("firm", None, ANA.id)),
    # A lawyer moves anybody's.
    (RUI, PETITION, folders.FIRM, ("firm", None, ANA.id)),
    # The sender takes it back into her own folder.
    (ANA, PETITION, folders.PRIVATE, ("private", None, ANA.id)),
    # Into a team he is in.
    (RUI, ACME, CIVEL, ("team", 10, RUI.id)),
    # An admin into a team she is not in.
    (CAIO, LAW, CIVEL, ("team", 10, PAULO.id)),
    # The one who put it on the firm's shelf takes it off again.
    (PAULO, LAW, CIVEL, ("team", 10, PAULO.id)),
])
def test_allowed_moves_happen_and_are_recorded_once(who, document_id, destination, after):
    async def body(conn):
        result = await _move(conn, who, document_id, destination)
        return result, await _where(conn, document_id), await _shares(conn)

    result, where, shares = _run(body)

    assert result.moved, result.reason
    assert result.label == destination.label
    assert where == after
    assert shares == [(who.id, destination.label, [document_id])]


@pytest.mark.parametrize("who, document_id, destination, says", [
    (ANA, LAW, CIVEL, "Este documento está na pasta do escritório"),
    # A lawyer may not take off the firm's shelf what somebody else put there.
    (RUI, LAW, CIVEL, "Só quem o enviou, ou um admin, pode tirá-lo"),
    (RUI, LAW, folders.PRIVATE, "Só quem o enviou, ou um admin, pode tirá-lo"),
    (RUI, PETITION, folders.PRIVATE, "torná-lo pessoal de novo"),
    (RUI, ACME, TRABALHISTA, "Você não está na equipe Trabalhista"),
    # Allowed destinations do not reach into a folder the mover cannot see.
    (BIA, ACME, folders.FIRM, "Não encontrei o documento 102"),
    (PAULO, ACME, folders.FIRM, "Não encontrei o documento 102"),
    (BIA, PETITION, TRABALHISTA, "Não encontrei o documento 104"),
])
def test_refused_moves_change_nothing_and_record_nothing(who, document_id, destination, says):
    async def body(conn):
        before = await _where(conn, document_id)
        result = await _move(conn, who, document_id, destination)
        return result, before, await _where(conn, document_id), await _shares(conn)

    result, before, after, shares = _run(body)

    assert not result.moved
    assert says in result.reason
    assert after == before
    assert shares == []


def test_without_a_rank_on_file_nothing_is_read():
    async def body(conn):
        return await folders.decide_move(conn, None, await scope_for(conn, None),
                                         LAW, folders.FIRM)

    decision = _run(body)

    assert not decision.allowed
    assert "acesso liberado" in decision.reason


def test_an_admin_is_told_the_move_hides_the_document_from_her():
    async def body(conn):
        scope = await scope_for(conn, CAIO.id)
        into_civel = await folders.decide_move(conn, CAIO, scope, LAW, CIVEL)
        to_firm = await folders.decide_move(conn, CAIO, scope, LAW, folders.FIRM)
        return into_civel, to_firm

    into_civel, to_firm = _run(body)

    assert into_civel.allowed and into_civel.hides_from_mover
    assert to_firm.allowed and not to_firm.hides_from_mover


def test_a_member_moving_into_her_own_team_keeps_seeing_it():
    async def body(conn):
        return await folders.decide_move(conn, RUI, await scope_for(conn, RUI.id),
                                         ACME, CIVEL)

    decision = _run(body)

    assert decision.allowed and not decision.hides_from_mover


# ── move and record, together or not at all ────────────────────────────────


def test_a_move_whose_record_fails_does_not_happen():
    """The audit table gone: the UPDATE ran in the same transaction, so it is
    rolled back with the failed insert, and the error goes up for the
    worker to retry rather than becoming a polite refusal."""
    import psycopg

    async def body(conn):
        async with conn.cursor() as cur:
            await cur.execute("ALTER TABLE document_access_log RENAME TO access_log_gone")
        await conn.commit()
        with pytest.raises(psycopg.errors.UndefinedTable):
            await _move(conn, RUI, ACME, folders.FIRM)
        return await _where(conn, ACME)

    assert _run(body) == ("private", None, RUI.id)


def test_work_the_caller_left_open_is_kept_and_the_move_is_its_own():
    """A refusal must not roll back what the caller did before asking."""
    async def body(conn):
        async with conn.cursor() as cur:
            await cur.execute("UPDATE documents SET title = 'Renomeado' WHERE id = %s",
                              (LAW,))
        result = await _move(conn, BIA, ACME, folders.FIRM)
        return result, await _committed("SELECT title FROM documents WHERE id = %s",
                                        (LAW,))

    result, title = _run(body)

    assert not result.moved
    assert title == [("Renomeado",)]


def test_the_bot_and_the_service_name_the_same_folders():
    async def body(conn):
        return [await folders.resolve_destination(conn, word)
                for word in ("Escritório", "privado", "cível", "Tributário")]

    firm, private, civel, unknown = _run(body)

    assert firm == folders.FIRM and private == folders.PRIVATE
    assert civel == CIVEL
    assert unknown is None


def test_a_decision_waits_for_a_move_already_under_way():
    """Ana moves her petition while Paulo is taking it into his own folder.

    Her decision reads the row with a lock, so it waits for his takeover and
    decides on what is there after it: a document she no longer sees, "not
    found". Without the lock she would decide on the old folder and be told
    the move failed for no reason she can act on.
    """
    import psycopg

    async def body(conn):
        assert URL
        async with await psycopg.AsyncConnection.connect(URL) as paulo:
            async with paulo.cursor() as cur:
                await cur.execute(_in_schema("SET search_path TO {schema}, public"))
            await paulo.commit()
            await documents.take_over_document(paulo, PETITION, new_owner_id=PAULO.id,
                                               commit=False)
            ana = asyncio.create_task(_move(conn, ANA, PETITION, folders.FIRM))
            for _ in range(100):
                waiting = await _committed(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND datname = current_database()")
                if waiting[0][0]:
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("Ana's move never waited for the lock")
            await paulo.commit()
            result = await ana
        return result, await _where(conn, PETITION), await _shares(conn)

    result, where, shares = _run(body)

    assert not result.moved
    assert "Não encontrei o documento 104" in result.reason
    assert where == ("private", None, PAULO.id)
    assert shares == []


def test_whoever_put_a_document_on_the_firms_shelf_may_take_it_off_and_a_lawyer_may_not():
    async def body(conn):
        async with conn.cursor() as cur:
            await cur.execute("UPDATE documents SET visibility = 'firm', team_id = NULL, "
                              "uploaded_by = %s WHERE id = %s", (RUI.id, ACME))
        await conn.commit()
        elsewhere = await _move(conn, BIA, ACME, TRABALHISTA)       # a lawyer, not the sender
        sender = await _move(conn, RUI, ACME, CIVEL)                # the sender, a lawyer
        return elsewhere, sender, await _where(conn, ACME), await _shares(conn)

    elsewhere, sender, where, shares = _run(body)

    assert not elsewhere.moved and "Só quem o enviou, ou um admin" in elsewhere.reason
    assert sender.moved
    assert where == ("team", 10, RUI.id)
    assert shares == [(RUI.id, "equipe Cível", [ACME])]


def test_a_document_nobody_sent_is_the_admins_to_take_off_the_firms_shelf():
    async def body(conn):
        async with conn.cursor() as cur:
            await cur.execute("UPDATE documents SET uploaded_by = NULL WHERE id = %s", (LAW,))
        await conn.commit()
        lawyer = await _move(conn, RUI, LAW, CIVEL)
        admin = await _move(conn, CAIO, LAW, TRABALHISTA)
        return lawyer, admin, await _where(conn, LAW)

    lawyer, admin, where = _run(body)

    assert not lawyer.moved and "um admin" in lawyer.reason
    assert admin.moved
    assert where == ("team", 11, None)


def test_the_rule_for_other_folders_is_unchanged_a_lawyer_moves_any_team_document():
    async def body(conn):
        return await _move(conn, RUI, PETITION, folders.FIRM)

    assert _run(body).moved


# ── editing how a document is described ─────────────────────────────────────


def test_the_fields_are_written_only_through_the_readers_scope():
    async def body(conn):
        changed = await documents.set_fields(conn, ACME, {"matter": "Outro"},
                                             scope=await scope_for(conn, BIA.id))
        return changed, await _committed("SELECT matter FROM documents WHERE id = %s", (ACME,))

    changed, matter = _run(body)

    assert changed is False
    assert matter == [("Acme",)]


def test_only_the_description_can_be_written_this_way():
    async def body(conn):
        with pytest.raises(ValueError):
            await documents.set_fields(conn, ACME, {"visibility": "firm"},
                                       scope=await scope_for(conn, RUI.id))
        with pytest.raises(ValueError):
            await documents.set_fields(conn, ACME, {}, scope=await scope_for(conn, RUI.id))
        return await _committed("SELECT visibility FROM documents WHERE id = %s", (ACME,))

    assert _run(body) == [("private",)]


def test_an_edit_whose_record_fails_does_not_happen():
    import psycopg

    async def body(conn):
        async with conn.cursor() as cur:
            await cur.execute("ALTER TABLE document_access_log RENAME TO access_log_gone")
        await conn.commit()
        with pytest.raises(psycopg.errors.UndefinedTable):
            await folders.edit_document(conn, RUI, await scope_for(conn, RUI.id), ACME,
                                        {"matter": "Outro"})
        return await _committed("SELECT matter FROM documents WHERE id = %s", (ACME,))

    assert _run(body) == [("Acme",)]


def test_an_edit_on_a_document_out_of_sight_is_not_found():
    async def body(conn):
        return await folders.edit_document(conn, BIA, await scope_for(conn, BIA.id), ACME,
                                           {"matter": "Outro"})

    result = _run(body)

    assert not result.changed and "Não encontrei o documento 102" in result.reason


def test_the_bots_edit_writes_only_through_the_readers_scope():
    """/editar and /renomear read the document in scope and now write in scope
    too: a document that left the reader's sight in between stays untouched."""
    async def body(conn):
        bia = await documents.update_document_fields(
            conn, ACME, matter="Outro", scope=await scope_for(conn, BIA.id))
        renamed = await documents.rename_document(
            conn, ACME, "Outro nome", scope=await scope_for(conn, BIA.id))
        rui = await documents.update_document_fields(
            conn, ACME, matter="Acme SA", scope=await scope_for(conn, RUI.id))
        return bia, renamed, rui, await _committed(
            "SELECT title, matter FROM documents WHERE id = %s", (ACME,))

    bia, renamed, rui, row = _run(body)

    assert bia is False and renamed is False and rui is True
    assert row == [("Contrato Acme", "Acme SA")]


def test_an_edit_waits_for_a_move_already_under_way():
    """Rui files the petition under a client while Paulo takes it into his own
    folder. The change reads the row with a lock, so it waits and then finds
    a document Rui no longer sees: "not found", and nothing written."""
    import psycopg

    async def body(conn):
        assert URL
        async with await psycopg.AsyncConnection.connect(URL) as paulo:
            async with paulo.cursor() as cur:
                await cur.execute(_in_schema("SET search_path TO {schema}, public"))
            await paulo.commit()
            await documents.take_over_document(paulo, PETITION, new_owner_id=PAULO.id,
                                               commit=False)
            rui = asyncio.create_task(folders.edit_document(
                conn, RUI, await scope_for(conn, RUI.id), PETITION, {"matter": "Outro"}))
            for _ in range(100):
                waiting = await _committed(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND datname = current_database()")
                if waiting[0][0]:
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("the edit never waited for the lock")
            await paulo.commit()
            result = await rui
        return result, await _committed("SELECT matter FROM documents WHERE id = %s",
                                        (PETITION,))

    result, matter = _run(body)

    assert not result.changed and "Não encontrei o documento 104" in result.reason
    assert matter == [("Gama",)]


def test_a_write_expecting_what_is_no_longer_there_writes_nothing():
    async def body(conn):
        scope = await scope_for(conn, RUI.id)
        stale = await documents.set_fields(conn, ACME, {"matter": "Novo"}, scope=scope,
                                           expected={"matter": "Outro cliente"})
        async with conn.cursor() as cur:
            await cur.execute("UPDATE documents SET matter = '  ' WHERE id = %s", (ACME,))
        await conn.commit()
        # Blank as stored counts as "no client", the way every page shows it.
        blank = await documents.set_fields(conn, ACME, {"matter": "Novo"}, scope=scope,
                                           expected={"matter": None})
        return stale, blank, await _committed(
            "SELECT matter FROM documents WHERE id = %s", (ACME,))

    stale, blank, matter = _run(body)

    assert stale is False and blank is True
    assert matter == [("Novo",)]
