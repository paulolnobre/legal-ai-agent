"""Conversational behavior that must not depend on an available LLM."""
import asyncio
from types import SimpleNamespace

import pytest

from core.handlers import legal_message
from core.services import channels
from core.services.legal_assistant import orchestrator
from core.services.legal_assistant import intents
from core.services.legal_assistant.intents import classify_local_intent
from core.services.legal_assistant.state import LegalState

pytestmark = pytest.mark.usefixtures("isolate_message_rate_limits")


def test_local_intents_cover_the_reported_messages_without_hijacking_legal_questions():
    assert classify_local_intent("ola") == "greeting"
    assert (
        classify_local_intent(
            "qual comando eu uso para fazer te enviar arquivos para vc ingerir?"
        )
        == "document_upload"
    )
    assert (
        classify_local_intent("eu gostaria digerir um documento para a memoria do agente")
        == "document_upload"
    )
    assert (
        classify_local_intent(
            "Você acredita que... quais são os documentos que você tem listado no momento?"
        )
        == "document_catalog"
    )
    # Reported in production: both of these reached the model, which answered by
    # inventing a "/equipe" command and by claiming it could not list commands.
    assert classify_local_intent("como eu vejo a equipe?") == "team_directory"
    assert classify_local_intent("me passe a lista de comandos") == "help"
    assert classify_local_intent("me manda os comandos") == "help"
    # A legal question that happens to use "comando" keeps its own route.
    assert classify_local_intent("qual o comando normativo do art. 5º?") is None
    assert classify_local_intent("quais documentos são necessários para usucapião?") is None
    assert classify_local_intent("Olá, revise esta cláusula para mim") is None
    assert classify_local_intent("tem como eu ver a lista de funcionarios?") == "team_directory"
    # Reported in production: the voice transcription used "mandar", which the
    # first version of the rule did not treat as a directory request at all.
    assert (
        classify_local_intent("Você consegue mandar para mim a equipe do escritório?")
        == "team_directory"
    )
    assert classify_local_intent("quem trabalha aqui?") == "team_directory"
    # A legal question that happens to mention a team must not be captured.
    assert classify_local_intent("a equipe adversa protocolou ontem") is None
    # Role names read the real roster instead of letting the model name people,
    # without capturing the company-law sense of "administrador".
    assert classify_local_intent("quem é o owner aqui?") == "team_directory"
    assert classify_local_intent("quais as obrigações do administrador?") is None


def _run_message(monkeypatch: pytest.MonkeyPatch, text: str, *, docs=None):
    sent: list[str] = []

    async def get_staff(*args, **kwargs):
        return SimpleNamespace(id=7, name="Paulo Nobre", role="advogado")

    async def send(chat_id, message, reply_markup=None):
        sent.append(message)

    async def list_docs(conn, **kwargs):
        return docs or []

    async def count_docs(conn, scope=None):
        return len(docs or [])

    async def get_summary(conn, document_id, scope=None):
        return next((d for d in (docs or []) if d.id == document_id), None)

    async def should_not_reach_agent(*args, **kwargs):
        raise AssertionError("basic conversation must not call the LLM graph")

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(legal_message, "search_documents", list_docs)
    monkeypatch.setattr(legal_message, "count_documents", count_docs)
    monkeypatch.setattr(legal_message, "get_document_summary", get_summary)
    monkeypatch.setattr(legal_message, "answer_question", should_not_reach_agent)
    asyncio.run(
        legal_message.process_legal_message(
            "connection",
            {"update_id": 100, "message": {"chat": {"id": 91001}, "text": text}},
        )
    )
    return sent


def test_greeting_is_answered_conversationally_without_the_agent(monkeypatch):
    sent = _run_message(monkeypatch, "ola")
    assert sent == [
        "Olá, Paulo! Como posso ajudar? Você pode escrever, mandar um áudio "
        "ou anexar um documento."
    ]


def test_natural_upload_question_explains_how_to_attach(monkeypatch):
    sent = _run_message(
        monkeypatch,
        "qual comando eu uso para fazer te enviar arquivos para vc ingerir?",
    )
    assert len(sent) == 1
    assert "Não precisa usar comando" in sent[0]
    assert "PDF, DOCX, TXT" in sent[0]
    assert "texto selecionável" in sent[0]
    assert "dependem de OCR" in sent[0]


def test_digest_document_request_explains_how_to_attach(monkeypatch):
    sent = _run_message(
        monkeypatch,
        "eu gostaria digerir um documento para a memoria do agente",
    )
    assert len(sent) == 1
    assert "Não precisa usar comando" in sent[0]
    assert "anexo do Telegram" in sent[0]


def test_natural_catalog_question_reads_the_database(monkeypatch):
    docs = [
        SimpleNamespace(
            id=12,
            title="Contrato social",
            doc_type="contrato",
            matter="Cliente X",
            created_at=None,
        )
    ]
    sent = _run_message(
        monkeypatch,
        "Você acredita que... quais são os documentos que você tem listado no momento?",
        docs=docs,
    )
    assert sent == ["📂 Documentos:\n[12] Contrato social (contrato | Cliente X)"]


def test_orchestrator_uses_local_rule_before_the_provider(monkeypatch):
    async def should_not_call_provider(*args, **kwargs):
        raise AssertionError("local intent must not call Gemini")

    monkeypatch.setattr(orchestrator, "complete_json", should_not_call_provider)
    result = asyncio.run(
        orchestrator.orchestrate(LegalState(conn=None, question="ola"))
    )

    assert result == {
        "task": "chat",
        "query": "ola",
        "routed_by": "rule:greeting",
    }


# ── Requests about a document already in the corpus ──────────────────────────

def test_document_requests_are_recognized_without_capturing_legal_questions():
    detect = intents.detect_document_request
    # Summarising and revising are one action now, and it is refused. They are
    # still detected because recognising the ask costs nothing and is what lets
    # the answer explain the rule instead of guessing at a narrower question.
    assert detect("faça um resumo do doc [2]") == intents.DocumentRequest(
        action="analysis", document_id=2
    )
    assert detect("revisar o documento 12, por favor") == intents.DocumentRequest(
        action="analysis", document_id=12
    )
    assert detect("me manda o arquivo original do documento 2") == intents.DocumentRequest(
        action="original", document_id=2
    )
    assert detect("revise o modelo_contrato_agente_ia.pdf") == intents.DocumentRequest(
        action="analysis", title_hint="modelo_contrato_agente_ia.pdf"
    )
    # A question about the law, or about a document without an explicit pointer,
    # still goes through the agent graph.
    assert detect("resuma a jurisprudência do STJ sobre dano moral") is None
    assert detect("o que diz o contrato 2 sobre rescisão?") is None
    assert detect("[2] modelo_contrato_agente_ia.pdf (- | -)") is None


def _run_document_message(monkeypatch: pytest.MonkeyPatch, text: str, *, docs):
    """Run a message with the corpus stubbed, capturing document-agent calls."""
    sent: list[str] = []
    calls: list[tuple] = []

    async def get_staff(*args, **kwargs):
        return SimpleNamespace(id=7, name="Paulo Nobre", role="advogado")

    async def send(chat_id, message, reply_markup=None):
        sent.append(message)

    async def list_docs(conn, **kwargs):
        return docs

    async def count_docs(conn, scope=None):
        return len(docs)

    async def get_summary(conn, document_id, scope=None):
        return next((d for d in docs if d.id == document_id), None)

    async def typing(*args, **kwargs):
        pass

    async def should_not_reach_agent(*args, **kwargs):
        raise AssertionError("a stored document must not be sent to the free-text agent")

    async def unexpected_document(*args, **kwargs):
        raise AssertionError("no file should be sent while answering in the chat")

    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(legal_message, "search_documents", list_docs)
    monkeypatch.setattr(legal_message, "count_documents", count_docs)
    monkeypatch.setattr(legal_message, "get_document_summary", get_summary)
    monkeypatch.setattr(channels, "send_typing", typing)
    monkeypatch.setattr(channels, "send_document", unexpected_document)
    monkeypatch.setattr(legal_message, "answer_question", should_not_reach_agent)
    legal_message._last_deliveries.clear()
    asyncio.run(
        legal_message.process_legal_message(
            "connection",
            {"update_id": 100, "message": {"chat": {"id": 91002}, "text": text}},
        )
    )
    return sent, calls


_DOCS = [
    SimpleNamespace(
        id=2, title="modelo_contrato_agente_ia.pdf", doc_type=None, matter=None,
        created_at=None,
    )
]


def test_asking_to_summarise_a_stored_document_is_answered_with_the_rule(monkeypatch):
    """The archive is a source. Reading one of its documents whole was an
    uncapped bill: the largest one here is 579,000 characters, and a legal code
    would be several times that."""
    sent, _ = _run_document_message(monkeypatch, "faça um resumo do doc [2]", docs=_DOCS)

    assert "fonte de consulta" in sent[0]
    # A refusal that does not say what to do instead reads as a broken feature.
    assert "pergunta" in sent[0] and "/arquivo" in sent[0]


def test_asking_to_revise_a_stored_document_is_answered_the_same_way(monkeypatch):
    sent, _ = _run_document_message(
        monkeypatch,
        "estou pedindo a revisão desse\n[2] modelo_contrato_agente_ia.pdf (- | -)",
        docs=_DOCS,
    )

    assert "fonte de consulta" in sent[0]


def test_unknown_document_number_asks_which_one_with_the_catalog(monkeypatch):
    sent, calls = _run_document_message(monkeypatch, "resuma o documento 99", docs=_DOCS)
    assert not calls, "an unknown id must not be summarized"
    assert "Não identifiquei de qual documento" in sent[0]
    assert "[2] modelo_contrato_agente_ia.pdf" in sent[1]


def test_short_review_request_is_routed_to_the_corpus(monkeypatch):
    """A one-line "revise aquele contrato" has no text to review."""
    async def fake_router(*args, **kwargs):
        return {"task": "review", "query": "revisão do contrato de locação"}

    monkeypatch.setattr(orchestrator, "complete_json", fake_router)
    result = asyncio.run(
        orchestrator.orchestrate(
            LegalState(conn=None, question="pode revisar aquele contrato de locação?")
        )
    )

    assert result["task"] == "internal_research"
    assert result["routed_by"] == "rule:review-without-text"


def test_pasted_text_is_still_reviewed_as_pasted_text(monkeypatch):
    pasted = "CLÁUSULA PRIMEIRA — DO OBJETO. " + "O contratado prestará serviços. " * 20

    async def fake_router(*args, **kwargs):
        return {"task": "review", "query": pasted}

    monkeypatch.setattr(orchestrator, "complete_json", fake_router)
    result = asyncio.run(orchestrator.orchestrate(LegalState(conn=None, question=pasted)))

    assert result["task"] == "review"


# ── Asking for the menu ─────────────────────────────────────────────────────
# The lawyers asked for the menu, with every command, whenever they ask for it
# in any words. "menu" has no legal meaning to protect; "opções" does, so it
# counts only as a whole message.

@pytest.mark.parametrize("message", [
    "menu", "Menu", "me mostra o menu", "cadê o menu?", "comandos", "opções",
])
def test_asking_for_the_menu_is_asking_for_help(message):
    assert classify_local_intent(message) == "help"


@pytest.mark.parametrize("message", [
    "quais as opções de recurso?",
    "quais as opções de defesa no processo",
])
def test_a_legal_question_about_options_keeps_its_route(message):
    """A lawyer weighing remedies is asking the law, not the assistant."""
    assert classify_local_intent(message) is None


# ── Doors: naming only the action opens its button's flow ───────────────────
# "quero fazer uma pesquisa" names an action and carries nothing to act on, so
# it opens the flow the menu button opens. A sentence that carries its content
# is the request itself and keeps going to the model.

@pytest.mark.parametrize("message, intent", [
    ("quero fazer uma pesquisa", "start_research"),
    ("pesquisa externa", "start_research"),
    ("preciso redigir um documento", "start_draft"),
    ("quero redigir uma cláusula", "start_draft"),
    ("quero ver um documento", "open_documents"),
    ("menu de documentos", "open_documents"),
    ("me mostra os documentos", "open_documents"),
])
def test_naming_only_the_action_opens_its_door(message, intent):
    assert classify_local_intent(message) == intent


@pytest.mark.parametrize("message", [
    "pesquisa sobre prescrição intercorrente",
    "redija uma cláusula de confidencialidade",
    "quero ver os documentos da Acme",
    "pesquisa de jurisprudência",
])
def test_a_sentence_that_carries_its_content_is_not_a_door(message):
    """The content is the request. Opening a button's flow here would make the
    lawyer type again what they had just written."""
    assert classify_local_intent(message) is None


def test_a_numbered_document_is_never_the_documents_menu():
    assert classify_local_intent("quero ver o documento 8") != "open_documents"


def test_asking_how_to_do_it_is_not_doing_it():
    """"como faço uma pesquisa?" wants an explanation, not a prompt."""
    assert classify_local_intent("como faço uma pesquisa?") is None


def test_the_more_specific_checks_still_answer_their_sentences():
    assert classify_local_intent("quais documentos você tem?") == "document_catalog"
    assert classify_local_intent("quero enviar um documento") == "document_upload"
    assert classify_local_intent("menu") == "help"


@pytest.mark.parametrize("message", [
    "quero ver um documento",
    "preciso redigir um documento",
])
def test_a_door_is_never_read_as_an_export(message):
    """"quero" is an export verb and "documento" a file word, so these used to
    be read as "turn the last answer into a file"."""
    from core.services.legal_assistant.intents import detect_export_request

    assert detect_export_request(message) is None


@pytest.mark.parametrize("message, expected", [
    ("quero isso em arquivo", "ask"),
    ("me manda em pdf", "pdf"),
    ("quero um documento com isso", "ask"),
    ("me envia o documento", "ask"),
    ("pode me mandar isso em documento?", "ask"),
])
def test_asking_for_the_answer_as_a_file_is_still_an_export(message, expected):
    from core.services.legal_assistant.intents import detect_export_request

    assert detect_export_request(message) == expected


def test_asking_whether_it_can_be_done_gets_an_answer_not_a_prompt():
    """"dá para pesquisar?" is made only of filler and the action's own word, so
    without the veto it would open the research prompt. It asks whether the
    assistant can do it, and that gets an answer."""
    assert classify_local_intent("dá para pesquisar?") is None
