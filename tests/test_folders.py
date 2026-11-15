"""The archive has three folders, and the search only reaches one person's.

The firm decided this on 2026-09-22: knowledge circulates, client material does
not. A law, a súmula, a model pleading belong to everybody; a contract somebody
uploaded about their own client belongs to them until they share it.

One vector index still, with the visibility travelling into the query. The
failure this guards against is quiet and serious: a passage from a private
document reaching another lawyer's answer, which no later step can undo,
because by then it has been quoted.
"""
import asyncio
from types import SimpleNamespace

import pytest
from psycopg import pq

from core.handlers import legal_message
from core.integrations import cache
from core.models import SearchHit, StaffMember
from core.repositories import documents
from core.repositories.teams import Scope, visibility_clause
from core.services import document_storage, folders, ingestion
from core.services.authorization import decide
from core.services.legal_assistant import graph, internal_research

pytestmark = pytest.mark.usefixtures(
    "isolate_message_rate_limits", "pending_store", "callback_store",
)

_INTERN = StaffMember(id=4, chat_id="400", name="Rita", role="estagiario")
_LAWYER = StaffMember(id=6, chat_id="600", name="Lia", role="advogado")
_ADMIN = StaffMember(id=9, chat_id="900", name="Ana", role="admin")


class _Cursor:
    def __init__(self, state):
        self.state = state
        self.rowcount = 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self.state.statements.append((" ".join(sql.split()), list(params or [])))

    async def fetchall(self):
        return self.state.rows

    async def fetchone(self):
        return self.state.rows[0] if self.state.rows else None


class _Conn:
    def __init__(self, rows=None):
        self.statements = []
        self.rows = rows or []
        self.commits = 0

    def cursor(self):
        return _Cursor(self)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


# ── the rule itself ─────────────────────────────────────────────────────────


def test_a_reader_with_no_identity_sees_only_the_firms_shelf():
    """The default everywhere. A path that forgets to say who is asking shows
    less than it should, never more."""
    clause, params = visibility_clause(Scope())

    assert clause == "d.visibility = 'firm'"
    assert params == []


def test_a_person_sees_the_firm_their_own_and_their_teams():
    clause, params = visibility_clause(Scope(staff_id=6, team_ids=(2, 3)))

    assert "d.visibility = 'firm'" in clause
    assert "d.visibility = 'private' AND d.uploaded_by = %s" in clause
    assert "d.visibility = 'team' AND d.team_id = ANY(%s)" in clause
    assert params == [6, [2, 3]]


def test_maintenance_can_reach_everything_and_says_so():
    """The CLI and the weekly law refresh act on a document by id, for the
    operator. That is a different thing from a person browsing."""
    assert visibility_clause(Scope.unrestricted()) == ("TRUE", [])


# ── the search ──────────────────────────────────────────────────────────────


def test_the_search_filters_in_the_statement_not_afterwards():
    conn = _Conn(rows=[])

    asyncio.run(documents.search_chunks(
        conn, [0.1, 0.2], top_k=5, scope=Scope(staff_id=6, team_ids=(2,)),
    ))

    sql, params = conn.statements[0]
    assert "WHERE (d.visibility = 'firm'" in sql
    assert "d.uploaded_by = %s" in sql and "d.team_id = ANY(%s)" in sql
    assert params[1] == 6 and params[2] == [2]


def test_a_matter_filter_narrows_further_it_does_not_replace_the_folder():
    conn = _Conn(rows=[])

    asyncio.run(documents.search_chunks(
        conn, [0.1], matter="Acme", scope=Scope(staff_id=6),
    ))

    sql, _params = conn.statements[0]
    assert "d.visibility" in sql and "d.matter = %s" in sql


def test_every_catalogue_query_carries_the_folder_filter():
    for call in (
        lambda conn: documents.list_documents(conn, scope=Scope(staff_id=6)),
        lambda conn: documents.search_documents(conn, title="x", scope=Scope(staff_id=6)),
        lambda conn: documents.count_documents(conn, scope=Scope(staff_id=6)),
        lambda conn: documents.list_matters(conn, scope=Scope(staff_id=6)),
        lambda conn: documents.list_doc_types(conn, scope=Scope(staff_id=6)),
        lambda conn: documents.get_document_summary(conn, 3, Scope(staff_id=6)),
        lambda conn: documents.get_document_source(conn, 3, Scope(staff_id=6)),
        lambda conn: documents.get_document(conn, 3, Scope(staff_id=6)),
    ):
        conn = _Conn(rows=[])
        asyncio.run(call(conn))
        sql, _params = conn.statements[0]
        assert "d.visibility" in sql, sql


def test_a_read_by_id_without_a_scope_still_only_sees_the_firms_shelf():
    """The default matters more than the explicit case: a caller that forgets
    is the one that would leak, so forgetting has to be the safe direction."""
    for call in (
        lambda conn: documents.get_document_summary(conn, 3),
        lambda conn: documents.get_document_source(conn, 3),
        lambda conn: documents.get_document(conn, 3),
    ):
        conn = _Conn(rows=[])
        asyncio.run(call(conn))
        sql, params = conn.statements[0]
        assert "d.visibility = 'firm'" in sql, sql
        assert "uploaded_by" not in sql, sql
        assert params == [3]


def test_the_agent_asks_on_behalf_of_the_person_who_asked(monkeypatch):
    """The scope is built in `run` and carried into the graph; without it the
    retrieval falls back to the firm's shelf and a lawyer stops finding their
    own documents."""
    seen = {}

    class _App:
        async def ainvoke(self, state, config=None):
            seen["scope"] = state.scope
            return {"answer": "ok", "task": "internal_research", "sources": []}

    async def scope_for(_conn, staff_id):
        return Scope(staff_id=staff_id, team_ids=(2,))

    monkeypatch.setattr(graph, "_app", _App())
    monkeypatch.setattr(graph, "scope_for", scope_for)

    asyncio.run(graph.run("connection", "qual o prazo?", staff_id=6))

    assert seen["scope"] == Scope(staff_id=6, team_ids=(2,))


def test_retrieval_passes_that_scope_to_the_search(monkeypatch):
    captured = {}

    async def search(_conn, _vector, top_k=8, matter=None, scope=None):
        captured["scope"] = scope
        return []

    async def embed(_query):
        return [0.1]

    monkeypatch.setattr(internal_research, "search_chunks", search)
    monkeypatch.setattr(internal_research, "_embed_cached", embed)

    state = internal_research.LegalState(
        conn="connection", question="q", query="q", staff_id=6,
        scope=Scope(staff_id=6, team_ids=(2,)),
    )
    asyncio.run(internal_research.retrieve(state))

    assert captured["scope"] == Scope(staff_id=6, team_ids=(2,))


# ── the answer cache ────────────────────────────────────────────────────────


def test_an_answer_built_on_a_private_document_is_cached_for_that_person_alone():
    hits = [
        SearchHit(chunk_id=1, document_id=7, title="Lei", content="...", distance=0.1),
        SearchHit(chunk_id=2, document_id=9, title="Contrato", content="...",
                  distance=0.2, visibility="private"),
    ]

    assert internal_research.audience_for_hits(6, hits) == "staff:6"


def test_an_answer_built_only_on_the_firms_shelf_is_shared():
    hits = [SearchHit(chunk_id=1, document_id=7, title="Lei", content="...", distance=0.1)]

    assert internal_research.audience_for_hits(6, hits) == "firm"
    assert internal_research.audience_for_hits(6, []) == "firm"


def test_the_audience_is_part_of_the_cache_key():
    """Two people asking the same question do not share an entry that was built
    on one of their private documents."""
    mine = cache._answer_identity("internal_research", None, "gpt", "staff:6")
    theirs = cache._answer_identity("internal_research", None, "gpt", "staff:9")
    shared = cache._answer_identity("internal_research", None, "gpt", "firm")

    assert mine != theirs != shared and mine != shared


# ── where a document lands ──────────────────────────────────────────────────


def _ingestion(monkeypatch, state):
    async def none(_conn, _sha):
        return None

    async def embed(chunks):
        return [[0.0] for _ in chunks]

    async def create(_conn, **kwargs):
        state.created = kwargs
        return 42

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ingestion, "find_document_by_sha", none)
    monkeypatch.setattr(ingestion, "embed_texts", embed)
    async def _store(data, **kwargs):
        return document_storage.StoredOriginal(key="aa/bb/cc", newly_created=True)

    monkeypatch.setattr(ingestion.document_storage, "store", _store)
    monkeypatch.setattr(ingestion, "create_document", create)
    monkeypatch.setattr(ingestion, "add_chunks", nothing)
    monkeypatch.setattr(ingestion, "log_access", nothing)
    monkeypatch.setattr(ingestion.cache, "invalidate_answers", nothing)


def test_what_a_person_sends_is_theirs_by_default(monkeypatch):
    state = SimpleNamespace(created=None)
    _ingestion(monkeypatch, state)

    asyncio.run(ingestion.ingest_document(
        _Conn(), b"contrato do cliente", filename="contrato.txt", uploaded_by=6,
    ))

    assert state.created["visibility"] == "private"
    assert state.created["uploaded_by"] == 6


def test_the_shared_inbox_lands_on_the_firms_shelf(monkeypatch):
    state = SimpleNamespace(created=None)
    _ingestion(monkeypatch, state)

    asyncio.run(ingestion.ingest_document(
        _Conn(), b"lei", filename="lei.txt", visibility="firm",
    ))

    assert state.created["visibility"] == "firm"


# ── moving between folders ──────────────────────────────────────────────────


def test_moving_asks_the_database_whether_this_person_may_see_it():
    conn = _Conn()

    asyncio.run(documents.move_document(
        conn, 12, visibility="firm", team_id=None, scope=Scope(staff_id=6),
    ))

    sql, params = conn.statements[0]
    assert "UPDATE documents" in sql and "d.visibility = 'private' AND d.uploaded_by = %s" in sql
    assert params[:3] == ["firm", None, 12]


class _Transaction:
    """A connection to the handler, which only the move service touches: it
    opens a transaction. Anything else reaching for a cursor fails loudly, as
    the bare string these tests used to pass did."""

    def __init__(self):
        self.info = SimpleNamespace(transaction_status=pq.TransactionStatus.IDLE)
        self.open = False
        self.committed = 0

    async def commit(self):
        pass

    def transaction(self):
        import contextlib

        @contextlib.asynccontextmanager
        async def opened():
            self.open = True
            try:
                yield
            finally:
                self.open = False
            self.committed += 1
        return opened()


def _command(monkeypatch, staff, text, *, folder=("private", None, 6), teams=(),
             my_teams=(), moved=True, summary=True):
    legal_message._last_deliveries.clear()
    sent: list[str] = []
    moves: list[dict] = []
    logged: list[dict] = []

    async def get_staff(_conn, chat_id):
        return staff if str(chat_id) == str(staff.chat_id) else None

    async def send(_chat_id, message, reply_markup=None):
        sent.append(message)
        return 1

    async def folder_in_scope(_conn, _doc_id, _scope, lock=False):
        if not summary:
            return None
        return documents.Folder(*folder, title="Contrato Acme")

    async def move(conn, doc_id, **kwargs):
        assert conn.open, "moved outside the service's transaction"
        moves.append({"id": doc_id, **kwargs})
        return moved

    async def find_team(_conn, name):
        return next(((tid, tname) for tid, tname, _ in teams
                     if tname.casefold() == name.casefold().strip()), None)

    async def list_teams(_conn):
        return list(teams)

    async def teams_of(_conn, _staff_id):
        return list(my_teams)

    async def log(conn, **kwargs):
        assert conn.open, "recorded outside the service's transaction"
        logged.append(kwargs)

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(folders, "folder_in_scope", folder_in_scope)
    monkeypatch.setattr(folders, "move_document", move)
    monkeypatch.setattr(folders, "find_team", find_team)
    monkeypatch.setattr(legal_message, "list_teams", list_teams)
    monkeypatch.setattr(folders, "teams_of", teams_of)
    monkeypatch.setattr(folders, "log_access", log)
    update = {"update_id": 1, "message": {
        "chat": {"id": int(staff.chat_id), "type": "private"},
        "from": {"id": int(staff.chat_id)}, "text": text,
    }}
    conn = _Transaction()
    asyncio.run(legal_message.process_legal_message(conn, update))
    return sent, moves


def test_every_rank_may_share_their_own_document():
    assert decide(_INTERN, "document.share").allowed


def test_sharing_with_the_firm_says_what_changed(monkeypatch):
    sent, moves = _command(monkeypatch, _LAWYER, "/compartilhar 12 escritorio")

    assert moves == [{"id": 12, "visibility": "firm", "team_id": None,
                      "scope": moves[0]["scope"], "commit": False}]
    assert "escritório" in sent[-1]
    assert "passa a encontrá-lo" in sent[-1]


def test_sharing_with_a_team_the_person_is_in(monkeypatch):
    sent, moves = _command(
        monkeypatch, _LAWYER, "/compartilhar 12 Trabalhista",
        teams=((2, "Trabalhista", 3),), my_teams=((2, "Trabalhista"),),
    )

    assert moves[0]["visibility"] == "team" and moves[0]["team_id"] == 2
    assert "equipe Trabalhista" in sent[-1]


def test_a_team_the_person_is_not_in_is_refused(monkeypatch):
    sent, moves = _command(
        monkeypatch, _LAWYER, "/compartilhar 12 Societario",
        teams=((5, "Societario", 2),), my_teams=(),
    )

    assert moves == []
    assert "não está na equipe" in sent[-1]


def test_an_admin_may_move_into_a_team_she_is_not_in(monkeypatch):
    sent, moves = _command(
        monkeypatch, _ADMIN, "/compartilhar 12 Societario",
        teams=((5, "Societario", 2),), my_teams=(),
    )

    assert moves[0]["visibility"] == "team" and moves[0]["team_id"] == 5
    assert "equipe Societario" in sent[-1]


def test_an_unknown_team_lists_the_ones_there_are(monkeypatch):
    sent, moves = _command(
        monkeypatch, _LAWYER, "/compartilhar 12 Tributario",
        teams=((2, "Trabalhista", 3),),
    )

    assert moves == []
    assert "Não conheço a equipe “Tributario”" in sent[-1]
    assert "Trabalhista" in sent[-1]


def test_a_document_this_person_cannot_see_is_not_found_whatever_the_team(monkeypatch):
    sent, moves = _command(monkeypatch, _LAWYER, "/compartilhar 99 Tributario",
                           summary=False, teams=((2, "Trabalhista", 3),))

    assert moves == []
    assert "Não encontrei o documento 99" in sent[-1]


def test_not_yours_to_move_comes_before_an_unknown_team(monkeypatch):
    sent, moves = _command(monkeypatch, _INTERN, "/compartilhar 12 Tributario",
                           folder=("private", None, 6))

    assert moves == []
    assert "Só quem enviou" in sent[-1]


def test_a_document_somebody_else_uploaded_is_not_yours_to_move(monkeypatch):
    sent, moves = _command(monkeypatch, _INTERN, "/compartilhar 12 escritorio",
                           folder=("private", None, 6))

    assert moves == []
    assert "Só quem enviou" in sent[-1]


def test_a_document_this_person_cannot_see_is_not_found(monkeypatch):
    sent, moves = _command(monkeypatch, _LAWYER, "/compartilhar 99 escritorio", summary=False)

    assert moves == []
    assert "Não encontrei" in sent[-1]


def test_bringing_a_document_back_to_the_personal_folder(monkeypatch):
    _sent, moves = _command(monkeypatch, _LAWYER, "/compartilhar 12 privado",
                            folder=("firm", None, 6))

    assert moves[0]["visibility"] == "private"


# ── teams ───────────────────────────────────────────────────────────────────


def _teams_command(monkeypatch, staff, text, teams=(), my_teams=(), created=7):
    sent: list[str] = []
    calls: list[tuple] = []

    async def get_staff(_conn, chat_id):
        return staff

    async def send(_chat_id, message, reply_markup=None):
        sent.append(message)
        return 1

    async def create_team(_conn, name, created_by=None):
        calls.append(("create", name, created_by))
        return created

    async def add_member(_conn, team_id, staff_id):
        calls.append(("add", team_id, staff_id))
        return True

    async def remove_member(_conn, team_id, staff_id):
        calls.append(("remove", team_id, staff_id))
        return True

    async def find_team(_conn, name):
        return next(((tid, tname) for tid, tname, _ in teams
                     if tname.casefold() == name.casefold().strip()), None)

    async def list_teams(_conn):
        return list(teams)

    async def teams_of(_conn, _staff_id):
        return list(my_teams)

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(legal_message, "create_team", create_team)
    monkeypatch.setattr(legal_message, "add_member", add_member)
    monkeypatch.setattr(legal_message, "remove_member", remove_member)
    monkeypatch.setattr(legal_message, "find_team", find_team)
    monkeypatch.setattr(legal_message, "list_teams", list_teams)
    monkeypatch.setattr(legal_message, "teams_of", teams_of)
    update = {"update_id": 1, "message": {
        "chat": {"id": int(staff.chat_id), "type": "private"},
        "from": {"id": int(staff.chat_id)}, "text": text,
    }}
    asyncio.run(legal_message.process_legal_message("connection", update))
    return sent, calls


def test_only_an_admin_creates_a_team():
    assert decide(_ADMIN, "team.manage").allowed
    assert not decide(_LAWYER, "team.manage").allowed


def test_creating_a_team_puts_its_creator_in_it(monkeypatch):
    sent, calls = _teams_command(monkeypatch, _ADMIN, "/grupos criar Trabalhista")

    assert ("create", "Trabalhista", 9) in calls
    assert ("add", 7, 9) in calls
    assert "Trabalhista" in sent[-1]


def test_a_lawyer_cannot_create_a_team(monkeypatch):
    _sent, calls = _teams_command(monkeypatch, _LAWYER, "/grupos criar Societario")

    assert calls == []


def test_anybody_may_see_which_teams_exist(monkeypatch):
    sent, calls = _teams_command(
        monkeypatch, _INTERN, "/grupos",
        teams=((2, "Trabalhista", 3), (5, "Societario", 2)), my_teams=((2, "Trabalhista"),),
    )

    assert calls == []
    assert "Trabalhista: 3 pessoa(s) (você)" in sent[-1]
    assert "Societario: 2 pessoa(s)" in sent[-1]


def test_the_menu_offers_sharing_in_the_firms_words():
    menu = {entry["command"]: entry["description"] for entry in legal_message.command_menu()}
    assert menu["compartilhar"] == "move um documento seu de pasta"
    assert menu["grupos"] == "mostra as equipes e quem está em cada uma"
