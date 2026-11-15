"""A document's name, and the catalogue that shows it.

The archive is read through a list, so the list is the product. What it showed
was the filename a disk had mangled and two dashes standing in for fields
nobody filled:

    [4] Código_De_Processo_Civil_LEI_Nº_13_105,_DE_16_DE_MARÇO_DE_2015.docx (- | - | 09/2026)

Both halves of that are fixed here: a readable default at ingestion, and a way
to give a document a name by hand when the filename never had one worth keeping.
"""
import asyncio
from types import SimpleNamespace

import pytest

from core.handlers import legal_message
from core.models import DocumentSummary, StaffMember
from core.services.authorization import decide
from core.repositories import documents
from core.services.ingestion import readable_title

pytestmark = pytest.mark.usefixtures("isolate_message_rate_limits")

_LAWYER = StaffMember(id=6, chat_id="600", name="Lia", role="advogado")
_INTERN = StaffMember(id=4, chat_id="400", name="Rita", role="estagiario")


def _update(chat_id: int, text: str) -> dict:
    return {
        "update_id": 800,
        "message": {"chat": {"id": chat_id, "type": "private"},
                    "from": {"id": chat_id}, "text": text},
    }


# ── The default name a file arrives with ────────────────────────────────────

@pytest.mark.parametrize("filename,expected", [
    # Both real, from this firm's archive.
    (
        "Código_De_Processo_Civil_LEI_Nº_13_105,_DE_16_DE_MARÇO_DE_2015.docx",
        "Código De Processo Civil LEI Nº 13 105, DE 16 DE MARÇO DE 2015",
    ),
    ("LEI Nº 14.133, DE 1º DE ABRIL DE 2021.docx", "LEI Nº 14.133, DE 1º DE ABRIL DE 2021"),
    ("contrato   acme    2024.pdf", "contrato acme 2024"),
    ("sem_extensao", "sem extensao"),
])
def test_a_filename_becomes_something_a_person_would_have_written(filename, expected):
    assert readable_title(filename) == expected


def test_capitalisation_is_never_guessed():
    """A statute is cited in the case it was published in. A title that guesses
    is worse than one that is merely ugly, and /renomear fixes the rest."""
    assert readable_title("LEI Nº 14.133.docx") == "LEI Nº 14.133"


def test_a_name_that_would_become_empty_keeps_the_filename():
    assert readable_title("___.pdf") == "___.pdf"


# ── The catalogue line ──────────────────────────────────────────────────────

def _document(**overrides):
    fields = {"id": 4, "title": "Contrato Acme", "doc_type": None, "matter": None,
              "created_at": None}
    return SimpleNamespace(**{**fields, **overrides})


def test_an_absent_field_is_absent_rather_than_a_dash():
    """`(- | -)` was two dashes of noise on every line of a list people read to
    find things."""
    assert legal_message._catalog_metadata(_document()) == ""


def test_what_is_known_is_shown(monkeypatch):
    import datetime
    line = legal_message._catalog_metadata(_document(
        doc_type="contrato", matter="Acme", created_at=datetime.date(2026, 9, 1),
    ))

    assert line == " (contrato | Acme | 09/2026)"


def test_a_date_alone_still_gets_its_parenthesis():
    import datetime
    assert legal_message._catalog_metadata(
        _document(created_at=datetime.date(2026, 9, 1))
    ) == " (09/2026)"


# ── Renaming ────────────────────────────────────────────────────────────────

def _wire(monkeypatch, staff=_LAWYER, document=None):
    calls = SimpleNamespace(sent=[], renamed=[], audited=[], scopes=[])

    async def get_staff(*_args, **_kwargs):
        return staff

    async def summary(_conn, document_id, scope=None):
        return document

    async def rename(_conn, document_id, title, **kwargs):
        calls.renamed.append((document_id, title))
        calls.scopes.append(kwargs.get("scope"))
        return True

    async def audit(_conn, action, **kwargs):
        calls.audited.append((action, kwargs.get("document_ids"), kwargs.get("query")))

    async def send(chat_id, text, reply_markup=None):
        calls.sent.append(text)
        return 1

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "get_document_summary", summary)
    monkeypatch.setattr(legal_message, "rename_document", rename)
    monkeypatch.setattr(legal_message, "log_access", audit)
    monkeypatch.setattr(legal_message, "_send", send)
    return calls


def test_a_lawyer_may_rename_and_an_intern_may_not():
    assert decide(_LAWYER, "document.edit").allowed
    assert not decide(_INTERN, "document.edit").allowed


def test_renaming_says_what_it_was_and_what_it_is_now(monkeypatch):
    """The old name is worth printing: it is how somebody notices they renamed
    the wrong document."""
    document = DocumentSummary(id=4, title="Codigo_De_Processo_Civil.docx")
    calls = _wire(monkeypatch, document=document)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(600, "/renomear 4 Código de Processo Civil"),
    ))

    assert calls.renamed == [(4, "Código de Processo Civil")]
    assert "Codigo_De_Processo_Civil.docx" in calls.sent[-1]
    assert "Código de Processo Civil" in calls.sent[-1]


def test_a_name_with_spaces_survives_intact(monkeypatch):
    """Everything after the id is the name, so it is not split on the first
    space and it is not truncated at it."""
    calls = _wire(monkeypatch, document=DocumentSummary(id=4, title="antigo"))

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(600, "/renomear 4 Lei 14.133/2021 (Nova Lei de Licitações)"),
    ))

    assert calls.renamed == [(4, "Lei 14.133/2021 (Nova Lei de Licitações)")]


def test_renaming_is_recorded_in_the_audit_trail(monkeypatch):
    calls = _wire(monkeypatch, document=DocumentSummary(id=4, title="antigo"))

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(600, "/renomear 4 Novo nome"),
    ))

    assert calls.audited == [("rename_document", [4], "Novo nome")]


def test_renaming_writes_on_behalf_of_the_person_asking(monkeypatch):
    """The write carries the asker's scope, not only the read before it: a
    document that left their sight in between is not renamed anyway."""
    calls = _wire(monkeypatch, document=DocumentSummary(id=4, title="antigo"))

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(600, "/renomear 4 Novo nome"),
    ))

    assert [scope.staff_id for scope in calls.scopes] == [6]


def test_the_bare_command_asks_which_document(monkeypatch, pending_store):
    calls = _wire(monkeypatch, document=DocumentSummary(id=4, title="antigo"))

    async def count(_conn, scope=None):
        return 2

    async def catalog(_conn, chat_id, **_kwargs):
        calls.sent.append("[4] antigo")

    monkeypatch.setattr(legal_message, "count_documents", count)
    monkeypatch.setattr(legal_message, "_send_document_catalog", catalog)

    asyncio.run(legal_message.process_legal_message("connection", _update(600, "/renomear")))

    assert "renomear" in calls.sent[-1]
    assert calls.renamed == []
    assert pending_store.kind(600) == "identifier"


def test_giving_only_the_id_asks_for_the_name_and_then_uses_it(
    monkeypatch, pending_store,
):
    """Two steps, because a rename needs two things and asking for both at once
    is the form nobody fills in."""
    calls = _wire(monkeypatch, document=DocumentSummary(id=4, title="antigo"))

    asyncio.run(legal_message.process_legal_message("connection", _update(600, "/renomear 4")))
    assert calls.renamed == []
    assert "antigo" in calls.sent[-1], "the current name is shown before it changes"

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(600, "Código de Processo Civil"),
    ))

    assert calls.renamed == [(4, "Código de Processo Civil")]
    assert not pending_store.waiting(600)


def test_a_name_longer_than_the_column_is_refused_not_truncated(monkeypatch):
    """The database would cut it silently, and a silent truncation is a lie
    about what was saved."""
    calls = _wire(monkeypatch, document=DocumentSummary(id=4, title="antigo"))
    too_long = "x" * (legal_message.MAX_TITLE_CHARS + 1)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(600, f"/renomear 4 {too_long}"),
    ))

    assert calls.renamed == []
    assert str(legal_message.MAX_TITLE_CHARS) in calls.sent[-1]


def test_renaming_a_document_that_is_not_there_says_so(monkeypatch):
    calls = _wire(monkeypatch, document=None)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(600, "/renomear 99 Qualquer nome"),
    ))

    assert calls.renamed == []
    assert "Não encontrei" in calls.sent[-1]


def test_an_intern_never_reaches_the_rename(monkeypatch):
    calls = _wire(monkeypatch, staff=_INTERN, document=DocumentSummary(id=4, title="x"))

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(400, "/renomear 4 Novo nome"),
    ))

    assert calls.renamed == []
    assert "advogados" in calls.sent[-1] or "🔒" in calls.sent[-1]


# ── Editing the classification ──────────────────────────────────────────────

def _wire_edit(monkeypatch, staff=_LAWYER, document=None, after=None):
    """The edit path, with the database and Telegram recorded."""
    calls = SimpleNamespace(sent=[], updated=[], audited=[], types=[], scopes=[])
    seen = {"n": 0}

    async def get_staff(*_args, **_kwargs):
        return staff

    async def summary(_conn, _document_id, scope=None):
        # The second read is the one after the update, so the reply can show
        # what the document became rather than what it was.
        seen["n"] += 1
        return after if (after is not None and seen["n"] > 1) else document

    async def update(_conn, document_id, *, title=None, doc_type=None, matter=None,
                     commit=True, scope=None):
        calls.updated.append((document_id, title, doc_type, matter))
        calls.scopes.append(scope)
        return True

    async def audit(_conn, action, **kwargs):
        calls.audited.append((action, kwargs.get("query")))

    async def types(_conn, *_args, **_kwargs):
        return calls.types

    async def send(chat_id, text, reply_markup=None):
        calls.sent.append(text)
        return 1

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "get_document_summary", summary)
    monkeypatch.setattr(legal_message, "update_document_fields", update)
    monkeypatch.setattr(legal_message, "list_doc_types", types)
    monkeypatch.setattr(legal_message, "log_access", audit)
    monkeypatch.setattr(legal_message, "_send", send)
    return calls


def test_the_type_and_the_matter_can_be_corrected_in_one_message(monkeypatch):
    """The real case: a statute ingested as L10406compilada, with no type."""
    document = DocumentSummary(id=8, title="L10406compilada")
    calls = _wire_edit(monkeypatch, document=document)

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(600, "/editar 8 tipo: lei; caso: legislação federal"),
    ))

    assert calls.updated == [(8, None, "lei", "legislação federal")]


def test_a_field_left_out_is_left_alone(monkeypatch):
    """Setting a type must not quietly erase a matter somebody filled in."""
    calls = _wire_edit(monkeypatch, document=DocumentSummary(id=8, title="x"))

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(600, "/editar 8 tipo: lei"),
    ))

    assert calls.updated == [(8, None, "lei", None)]


def test_the_name_can_be_changed_here_too(monkeypatch):
    calls = _wire_edit(monkeypatch, document=DocumentSummary(id=8, title="x"))

    asyncio.run(legal_message.process_legal_message(
        "connection",
        _update(600, "/editar 8 nome: Código Civil (Lei 10.406/2002); tipo: lei"),
    ))

    assert calls.updated == [(8, "Código Civil (Lei 10.406/2002)", "lei", None)]


def test_the_words_are_the_ones_a_caption_already_uses(monkeypatch):
    """A document attached with "tipo: contrato" in its caption is classified
    that way at upload. Correcting it later must not need a second vocabulary."""
    calls = _wire_edit(monkeypatch, document=DocumentSummary(id=8, title="x"))

    for phrasing in ("caso: Acme", "processo: Acme", "matéria: Acme"):
        calls.updated.clear()
        asyncio.run(legal_message.process_legal_message(
            "connection", _update(600, f"/editar 8 {phrasing}"),
        ))
        assert calls.updated == [(8, None, None, "Acme")], phrasing


def test_the_edit_is_recorded_with_what_changed(monkeypatch):
    calls = _wire_edit(monkeypatch, document=DocumentSummary(id=8, title="x"))

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(600, "/editar 8 tipo: lei"),
    ))

    action, query = calls.audited[0]
    assert action == "edit_document"
    assert "doc_type=lei" in query


def test_editing_writes_on_behalf_of_the_person_asking(monkeypatch):
    calls = _wire_edit(monkeypatch, document=DocumentSummary(id=8, title="x"))

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(600, "/editar 8 caso: Acme"),
    ))

    assert [scope.staff_id for scope in calls.scopes] == [6]


def test_the_reply_shows_the_document_as_it_now_is(monkeypatch):
    """Reading it back is what proves the change landed, to the person who
    asked for it, without them having to run /detalhes."""
    calls = _wire_edit(
        monkeypatch,
        document=DocumentSummary(id=8, title="L10406compilada"),
        after=DocumentSummary(id=8, title="L10406compilada", doc_type="lei"),
    )

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(600, "/editar 8 tipo: lei"),
    ))

    assert "Tipo: lei" in calls.sent[-1]


def test_giving_only_the_id_shows_what_can_change_and_what_is_in_use(
    monkeypatch, pending_store,
):
    calls = _wire_edit(
        monkeypatch, document=DocumentSummary(id=8, title="L10406compilada"),
    )
    calls.types = [("lei", 3), ("contrato", 2)]

    asyncio.run(legal_message.process_legal_message("connection", _update(600, "/editar 8")))

    message = calls.sent[-1]
    assert calls.updated == []
    assert "L10406compilada" in message
    assert "lei (3)" in message, "the types already in use are offered"
    assert "data de ingestão não muda" in message
    assert pending_store.kind(600) == "identifier"


def test_the_answer_to_that_question_edits_the_document(monkeypatch, pending_store):
    calls = _wire_edit(
        monkeypatch, document=DocumentSummary(id=8, title="L10406compilada"),
    )

    asyncio.run(legal_message.process_legal_message("connection", _update(600, "/editar 8")))
    asyncio.run(legal_message.process_legal_message(
        "connection", _update(600, "tipo: lei; caso: legislação federal"),
    ))

    assert calls.updated == [(8, None, "lei", "legislação federal")]
    assert not pending_store.waiting(600)


def test_a_value_longer_than_its_column_is_refused(monkeypatch):
    calls = _wire_edit(monkeypatch, document=DocumentSummary(id=8, title="x"))

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(600, "/editar 8 tipo: " + "x" * 51),
    ))

    assert calls.updated == []
    assert "limite" in calls.sent[-1]


def test_an_intern_never_reaches_the_edit(monkeypatch):
    calls = _wire_edit(monkeypatch, staff=_INTERN, document=DocumentSummary(id=8, title="x"))

    asyncio.run(legal_message.process_legal_message(
        "connection", _update(400, "/editar 8 tipo: lei"),
    ))

    assert calls.updated == []


def test_the_details_reply_says_how_to_fix_the_classification(monkeypatch):
    """The person looking at "Tipo: não informado" is the one who can fix it."""
    rendered = legal_message._render_document_details(
        DocumentSummary(id=8, title="L10406compilada")
    )

    assert "/editar 8 tipo:" in rendered


# ── The update itself, not the command around it ────────────────────────────

class _Cursor:
    def __init__(self, recorder):
        self.recorder = recorder
        self.rowcount = 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, sql, params=None):
        self.recorder.append((" ".join(sql.split()), params))


class _Connection:
    def __init__(self):
        self.statements = []

    def cursor(self):
        return _Cursor(self.statements)

    async def commit(self):
        pass


def test_only_the_named_columns_appear_in_the_update():
    """A field left out must not reach the statement at all.

    Writing NULL for it would erase a matter somebody filled in last week, and
    the command that did it would look like it only set a type.
    """
    conn = _Connection()

    asyncio.run(documents.update_document_fields(conn, 8, doc_type="lei"))

    sql, params = conn.statements[0]
    assert sql == "UPDATE documents SET doc_type = %s WHERE id = %s"
    assert params == ("lei", 8)
    assert "title" not in sql and "matter" not in sql


def test_all_three_can_move_at_once():
    conn = _Connection()

    asyncio.run(documents.update_document_fields(
        conn, 8, title="Código Civil", doc_type="lei", matter="federal",
    ))

    sql, params = conn.statements[0]
    assert sql == (
        "UPDATE documents SET title = %s, doc_type = %s, matter = %s WHERE id = %s"
    )
    assert params == ("Código Civil", "lei", "federal", 8)


def test_a_value_is_cut_to_its_column_before_it_reaches_the_database():
    """The database would truncate silently; the command refuses first, and
    this is the second line of that defence."""
    conn = _Connection()

    asyncio.run(documents.update_document_fields(conn, 8, doc_type="x" * 200))

    _, params = conn.statements[0]
    assert len(params[0]) == documents.MAX_DOC_TYPE_CHARS


def test_an_update_that_names_nothing_touches_nothing():
    conn = _Connection()

    changed = asyncio.run(documents.update_document_fields(conn, 8))

    assert changed is False
    assert conn.statements == []
