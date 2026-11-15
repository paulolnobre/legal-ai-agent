"""The four ranks, and reading a catalogue too large to list.

Two rules the firm depends on: a rank never gains privilege by accident, and
the archive stays usable once it is big enough that listing it is useless.
"""
import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from core.handlers import legal_message
from core.services import channels
from core.models import StaffMember
from core.repositories import staff as staff_repository
from core.services.legal_assistant.intents import (
    classify_local_intent,
    parse_document_filters,
)

pytestmark = pytest.mark.usefixtures("isolate_message_rate_limits")


# ── The scale ────────────────────────────────────────────────────────────────

def test_the_four_ranks_are_ordered_from_least_to_most_privileged():
    assert staff_repository.ROLE_ORDER == ("estagiario", "advogado", "admin", "owner")
    assert staff_repository.has_rank("owner", "admin")
    assert staff_repository.has_rank("admin", "advogado")
    assert not staff_repository.has_rank("advogado", "admin")
    assert not staff_repository.has_rank("estagiario", "advogado")


def test_the_legacy_role_becomes_a_lawyer_and_anything_unknown_fails_closed():
    # "user" predates the scale and meant a working lawyer.
    assert staff_repository.normalize_role("user") == "advogado"
    # A corrupted value must lose privilege, never gain it.
    for broken in ("chefe", "", None, "OWNER_"):
        assert staff_repository.normalize_role(broken) == "estagiario", broken


def test_role_labels_cover_every_rank():
    for role in staff_repository.ROLE_ORDER:
        assert role in staff_repository.ROLE_LABELS


# ── What each rank may do ────────────────────────────────────────────────────

def _run(monkeypatch, staff, text, *, docs=None, total=None, summary=None):
    sent: list[str] = []
    reached: list[str] = []

    async def get_staff(*args, **kwargs):
        return staff

    async def send(chat_id, message, reply_markup=None):
        sent.append(message)

    async def typing(*args, **kwargs):
        pass

    async def search(conn, **kwargs):
        return docs or []

    async def count(conn, scope=None):
        # The whole corpus, which is not the same as what this filter matched.
        return len(docs or []) if total is None else total

    async def reached_ingestion(*args, **kwargs):
        reached.append("ingestion")
        return 1, 1

    async def reached_original(conn, chat_id, document_id, staff_id):
        reached.append("original")

    async def get_summary(conn, document_id, scope=None):
        # Deliberately NOT read out of `docs`: a document is looked up by id, so
        # a test can hand back one that no page of the catalogue would contain.
        return summary

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(channels, "send_typing", typing)
    monkeypatch.setattr(legal_message, "search_documents", search)
    monkeypatch.setattr(legal_message, "count_documents", count)
    monkeypatch.setattr(legal_message, "ingest_bytes", reached_ingestion)
    monkeypatch.setattr(legal_message, "_send_original_document", reached_original)
    monkeypatch.setattr(legal_message, "get_document_summary", get_summary)
    monkeypatch.setattr(
        legal_message, "_limiter", SimpleNamespace(check=lambda _c: (True, False)),
    )
    legal_message._last_deliveries.clear()
    assert staff.chat_id is not None, "the staff fixtures above all carry a chat id"
    asyncio.run(
        legal_message.process_legal_message(
            "connection",
            {"update_id": 1, "message": {
                "chat": {"id": int(staff.chat_id), "type": "private"},
                "from": {"id": int(staff.chat_id)},
                "text": text,
            }},
        )
    )
    return sent, reached


_INTERN = StaffMember(id=5, chat_id="500", name="Estagiário", role="estagiario")
_LAWYER = StaffMember(id=6, chat_id="600", name="Advogada", role="advogado")


def test_an_intern_may_read_the_catalogue(monkeypatch):
    docs = [SimpleNamespace(id=1, title="Contrato", doc_type="contrato",
                            matter="Acme", created_at=None)]
    sent, _ = _run(monkeypatch, _INTERN, "/docs", docs=docs)
    assert "Contrato" in sent[0]


def test_an_intern_may_not_pull_an_original_out_of_the_archive(monkeypatch):
    sent, reached = _run(monkeypatch, _INTERN, "/arquivo 1")
    assert "original" not in reached, "the archive must stay closed to interns"
    assert "estagiário" in sent[0]


def test_an_intern_may_not_ingest_a_document(monkeypatch):
    sent, reached = _run(monkeypatch, _INTERN, "/ingerir https://www.planalto.gov.br/lei")
    assert "ingestion" not in reached
    assert "advogados" in sent[0]


def test_a_lawyer_may_do_both(monkeypatch):
    _, reached = _run(monkeypatch, _LAWYER, "/arquivo 1")
    assert reached == ["original"]


def test_a_lawyer_still_may_not_manage_access(monkeypatch):
    sent, _ = _run(monkeypatch, _LAWYER, "/usuarios")
    assert "admins e do owner" in sent[0]


def test_the_help_shows_only_what_the_rank_can_run():
    intern_help = legal_message._help_for(_INTERN)
    lawyer_help = legal_message._help_for(_LAWYER)

    assert "/docs" in intern_help
    assert "/arquivo" not in intern_help, "an intern must not be shown the archive"
    assert "/arquivo" in lawyer_help
    assert "/autorizar" not in lawyer_help


# ── Working outside office hours ─────────────────────────────────────────────

def test_only_the_owner_is_served_outside_office_hours():
    rows = {"111": "owner", "222": "admin", "333": "advogado"}

    class _Cursor:
        def __init__(self):
            self.result = None

        async def execute(self, sql, params):
            self.result = (rows.get(params[0]),) if params[0] in rows else None

        async def fetchone(self):
            return self.result

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _Conn:
        def cursor(self):
            return _Cursor()

    unrestricted = staff_repository.has_unrestricted_access
    assert asyncio.run(unrestricted(_Conn(), "111")) is True
    assert asyncio.run(unrestricted(_Conn(), "222")) is False
    assert asyncio.run(unrestricted(_Conn(), "999")) is False


def test_a_database_problem_defers_rather_than_opening_the_night(monkeypatch):
    """On any doubt the message waits for morning — the queue keeps it safe."""
    from core.services import work_hours

    async def broken_pool():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(work_hours, "get_pool", broken_pool)
    assert asyncio.run(work_hours._may_work_out_of_hours("111")) is False


# ── Reading a catalogue too large to list ────────────────────────────────────

_NOW = datetime(2026, 9, 4, tzinfo=UTC)


@pytest.mark.parametrize(
    "message, doc_type, matter",
    [
        ("contratos da Acme de 2024", "contrato", "Acme"),
        ("contratos do cliente Banco Alfa", "contrato", "Banco Alfa"),
        ("petições dos últimos 6 meses", "petição", None),
        ("acórdãos de março de 2025", "acórdão", None),
        ("procurações da empresa XYZ Ltda dos últimos 2 anos", "procuração", "XYZ Ltda"),
        ("contratos", "contrato", None),
    ],
)
def test_filters_are_read_from_the_way_lawyers_actually_ask(message, doc_type, matter):
    filters = parse_document_filters(message, now=_NOW)
    assert filters.doc_type == doc_type
    assert filters.matter == matter


def test_a_year_becomes_a_closed_window():
    filters = parse_document_filters("contratos de 2024", now=_NOW)
    assert filters.since == datetime(2024, 1, 1, tzinfo=UTC)
    assert filters.until == datetime(2025, 1, 1, tzinfo=UTC)


def test_a_month_becomes_that_month_only():
    filters = parse_document_filters("acórdãos de março de 2025", now=_NOW)
    assert filters.since == datetime(2025, 3, 1, tzinfo=UTC)
    assert filters.until == datetime(2025, 4, 1, tzinfo=UTC)


def test_a_relative_window_counts_back_from_today():
    filters = parse_document_filters("documentos dos últimos 6 meses", now=_NOW)
    assert filters.since == datetime(2026, 3, 8, tzinfo=UTC)
    assert filters.until is None


def test_a_request_with_no_filter_stays_empty():
    assert parse_document_filters("me mostra tudo", now=_NOW).is_empty()
    assert parse_document_filters("", now=_NOW).is_empty()


def test_the_answer_says_what_it_searched_for():
    described = parse_document_filters("contratos da Acme de 2024", now=_NOW).describe()
    assert "contrato" in described
    assert "Acme" in described
    assert "01/01/2024" in described


def test_a_filter_that_matches_nothing_says_so_and_offers_a_way_out(monkeypatch):
    sent, _ = _run(
        monkeypatch, _LAWYER, "/docs contratos da Inexistente", docs=[], total=42,
    )
    assert "Nenhum documento encontrado" in sent[0]
    assert "42 documento" in sent[0], "say how big the corpus is, so the filter is the suspect"
    assert "Inexistente" in sent[0]


def test_a_long_catalogue_is_paged_and_explains_how_to_narrow_it(monkeypatch):
    docs = [
        SimpleNamespace(id=n, title=f"Documento {n}", doc_type="contrato",
                        matter="Acme", created_at=None)
        for n in range(1, 40)
    ]
    sent, _ = _run(monkeypatch, _LAWYER, "/docs", docs=docs)

    assert sent[0].count("[") <= 31, "a chat message cannot hold the whole archive"
    assert "Refine com" in sent[0]


# ── Asking in words is the same act as typing the command ────────────────────

_ORIGINAL_IN_WORDS = "me manda o arquivo original do documento 2"
_STORED = SimpleNamespace(id=2, title="Contrato Acme", doc_type="contrato",
                          matter="Acme", created_at=None)


def test_an_intern_cannot_get_around_the_gate_by_asking_in_words(monkeypatch):
    """/arquivo and "me manda o arquivo original" are one action, not two."""
    sent, reached = _run(monkeypatch, _INTERN, _ORIGINAL_IN_WORDS, summary=_STORED)
    assert "original" not in reached, "phrasing must not decide who reaches the archive"
    assert "estagiário" in sent[0]


def test_a_lawyer_asking_in_words_still_gets_the_original(monkeypatch):
    _, reached = _run(monkeypatch, _LAWYER, _ORIGINAL_IN_WORDS, summary=_STORED)
    assert reached == ["original"]


def test_a_document_older_than_the_newest_page_still_answers_to_its_number(monkeypatch):
    """The archive outgrows any page; pointing at a document by number must not.

    `docs` is empty here — nothing this document would ever appear on — and the
    request still resolves, because the id is looked up rather than scanned for.
    """
    _, reached = _run(monkeypatch, _LAWYER, _ORIGINAL_IN_WORDS, docs=[], summary=_STORED)
    assert reached == ["original"]


def test_a_number_that_names_nothing_asks_which_document(monkeypatch):
    docs = [SimpleNamespace(id=1, title="Outro", doc_type=None, matter=None, created_at=None)]
    sent, reached = _run(monkeypatch, _LAWYER, _ORIGINAL_IN_WORDS, docs=docs, summary=None)
    assert "original" not in reached
    assert "Não identifiquei" in sent[0]


def test_a_month_names_its_year_with_or_without_the_preposition():
    for message in ("acórdãos de março de 2024", "acórdãos de março 2024"):
        filters = parse_document_filters(message, now=_NOW)
        assert filters.since == datetime(2024, 3, 1, tzinfo=UTC), message
        assert filters.until == datetime(2024, 4, 1, tzinfo=UTC), message


# ── A question is not a filter ───────────────────────────────────────────────
# On 2026-09-09 a lawyer asked "o documento tem que ser ingerido para aparecer
# na lista de documentos? os objetos nos buckets n entram" and was answered
# "Nenhum documento encontrado com esse filtro (caso/cliente contendo 'aparecer
# na lista de documentos? os objetos nos buckets n entram')". Two independent
# faults, and both are covered here: the sentence was classified as a request
# for the catalogue, and then the preposition "para" was read as introducing a
# client name.

_THE_QUESTION = (
    "o documento tem que ser ingerido para aparecer na lista de documentos? "
    "os objetos nos buckets n entram"
)


@pytest.mark.parametrize(
    "message",
    [
        _THE_QUESTION,
        "preciso ingerir o arquivo para ele entrar no acervo?",
        "como funciona a lista de documentos ingeridos?",
        "tem como salvar um documento sem ingerir?",
    ],
)
def test_a_question_about_the_rules_is_not_a_request_for_the_catalogue(message):
    """Asking what the rule is has to reach something that can explain it.

    A listing can only enumerate, so routing one of these to the catalogue
    answers a question nobody asked and hides the one that was.
    """
    assert classify_local_intent(message) is None


@pytest.mark.parametrize(
    "message",
    [
        "quais documentos você tem?",
        "quais documentos estão salvos?",
        "o que tem no acervo",
    ],
)
def test_a_real_request_for_the_catalogue_still_gets_there(message):
    assert classify_local_intent(message) == "document_catalog"


def test_prose_never_turns_a_bare_preposition_into_a_client():
    filters = parse_document_filters(_THE_QUESTION, now=_NOW, explicit=False)
    assert filters.matter is None, (
        "'para aparecer na lista' is a clause, not a client called "
        "'aparecer na lista'"
    )


def test_prose_still_reads_a_client_that_a_noun_names():
    filters = parse_document_filters(
        "me mostra os contratos do cliente Acme", now=_NOW, explicit=False,
    )
    assert filters.doc_type == "contrato"
    assert filters.matter == "Acme"


def test_typing_the_command_is_the_claim_that_the_text_is_a_filter():
    """/docs contratos da Acme must keep working exactly as it did.

    The stricter reading applies to a sentence the classifier caught, not to a
    filter somebody wrote on purpose.
    """
    filters = parse_document_filters("contratos da Acme de 2024", now=_NOW)
    assert filters.doc_type == "contrato"
    assert filters.matter == "Acme"


def test_a_client_name_never_spans_a_question_mark():
    filters = parse_document_filters(
        "contratos da Acme? e os outros documentos", now=_NOW,
    )
    assert filters.matter == "Acme"


def test_the_question_reaches_the_agent_instead_of_the_catalogue(monkeypatch):
    """End to end, because the two faults were in two different files.

    Fixing either one alone still leaves a lawyer's question answered by a
    search that was never asked for.
    """
    asked: list[str] = []

    async def answer(conn, text, **kwargs):
        asked.append(text)
        return SimpleNamespace(text="Só entra no acervo o que foi ingerido.", sources=[])

    monkeypatch.setattr(legal_message, "answer_question", answer)
    sent, _ = _run(monkeypatch, _LAWYER, _THE_QUESTION, docs=[], total=42)

    assert asked == [_THE_QUESTION], "the question must reach the agent verbatim"
    assert not any("Nenhum documento encontrado" in message for message in sent)
    assert not any("caso/cliente" in message for message in sent)


def test_a_real_catalogue_request_is_not_narrowed_by_its_own_grammar(monkeypatch):
    """The seam between the two halves of the fix, which nothing else covers.

    "quais documentos foram ingeridos para revisão" is a genuine request for the
    archive, so the intent veto lets it through. What must not happen next is the
    preposition becoming a client: typed after /docs this text yields a filter on
    "revisão", and a lawyer would be told the archive holds nothing for a client
    of that name. Reverting the handler alone reintroduces exactly that, and no
    other test in this file notices.
    """
    listed = [
        SimpleNamespace(id=1, title="Contrato Acme", doc_type="contrato",
                        matter="Acme", created_at=_NOW),
    ]
    sent, _ = _run(
        monkeypatch,
        _LAWYER,
        "quais documentos foram ingeridos para revisão",
        docs=listed,
        total=1,
    )

    assert "Contrato Acme" in sent[0]
    assert "revisão" not in sent[0], "'para revisão' is a purpose, not a client"
    assert "caso/cliente" not in sent[0]
