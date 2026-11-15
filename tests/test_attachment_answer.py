"""What the answer sees of an attachment (spec "What the answer sees", "No
shared cache with an attachment", "Bounded prompt and budget").

The reading or transcript reaches every answering node as a data block of
its own, bounded, and reserved in the budget by its UTF-8 bytes; an answer
drawn from it is never read from or written to the shared answer cache; and
a follow-up in the panel carries it for two hours, for its own person only.
"""
import asyncio
import contextlib
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from core import document_crypto
from core.config import settings
from core.handlers import legal_message
from core.integrations import cache, llm
from core.integrations import serper as serper_module
from core.models import SearchHit, StaffMember
from core.repositories import conversation, panel_uploads
from core.services import channels, media, panel_intake, work_hours
from core.services.legal_assistant import (
    attachments,
    chat,
    drafting,
    external_research,
    graph,
    internal_research,
    review,
)
from core.services.legal_assistant.conversation_router import TaskRoute
from core.services.legal_assistant.rendering import document_block
from core.services.legal_assistant.reply import AgentReply
from core.services.legal_assistant.state import LegalState

RUI = StaffMember(id=4, chat_id="400", name="Rui", role="advogado")
PAULO = StaffMember(id=1, chat_id="100", name="Paulo", role="owner")

# Ideographs and an emoji: three and four UTF-8 bytes each, so a reservation
# that counted characters would fall short of the bytes by more than half.
DENSE = "判決理由🙂" * 2_000


async def _nothing(*_args, **_kwargs):
    return None


# ── every answering node ─────────────────────────────────────────────────────

# The graph's answering nodes, by their name in the graph, and the function
# each one runs. Compared with the graph itself below, so a node added later
# without the attachment fails here rather than answering without it.
NODES = {
    "chat": (chat, "chat"),
    "draft": (drafting, "draft"),
    "review": (review, "review"),
    "answer_internal": (internal_research, "answer"),
    "research_external": (external_research, "research"),
}
_NOT_ANSWERING = {"__start__", "orchestrate", "cache_lookup", "retrieve", "verify"}


def test_the_nodes_checked_here_are_every_answering_node_of_the_graph():
    assert set(graph.build_graph().nodes) - _NOT_ANSWERING == set(NODES)


def _no_outside_sources(monkeypatch):
    """External research with nothing found anywhere and nothing fetched."""
    async def no_datajud(_text):
        return [], [], [], []

    async def no_djen(_text):
        return [], [], []

    async def no_web(_query, provider=None):
        return []

    async def no_pages(_sources):
        return [], [], []

    monkeypatch.setattr(settings, "web_search_provider", "serper")
    monkeypatch.setattr(external_research, "_datajud_context", no_datajud)
    monkeypatch.setattr(external_research, "_djen_context", no_djen)
    monkeypatch.setattr(external_research.websearch, "search", no_web)
    monkeypatch.setattr(external_research, "_read_official_pages", no_pages)


def _stub_node(monkeypatch, module, calls: list, answer: str = "Resposta."):
    async def complete(system, content, **kwargs):
        calls.append((system, content, kwargs))
        return answer

    monkeypatch.setattr(module, "complete", complete)
    if hasattr(module, "log_access"):
        monkeypatch.setattr(module, "log_access", _nothing)
    _no_outside_sources(monkeypatch)


@pytest.mark.parametrize("name", sorted(NODES))
def test_every_answering_node_sends_the_attachment_as_data_and_reserves_its_bytes(
        monkeypatch, name):
    module, entry = NODES[name]
    calls: list = []
    _stub_node(monkeypatch, module, calls)
    state = LegalState(conn=None, question="Explique este trecho", query="trecho",
                       attachment_text=DENSE)

    asyncio.run(getattr(module, entry)(state))

    [(system, content, options)] = calls
    assert DENSE not in system, "the attachment is data, never instructions"
    [attachment] = [b for b in content if b.get("title") == attachments.TITLE]
    assert attachment["type"] == "document"
    assert attachment["source"]["content"][0]["text"] == DENSE
    assert options["extra_input_tokens"] == len(DENSE.encode("utf-8"))


@pytest.mark.parametrize("name", sorted(NODES))
def test_without_an_attachment_a_node_asks_as_it_did(monkeypatch, name):
    module, entry = NODES[name]
    calls: list = []
    _stub_node(monkeypatch, module, calls)
    hit = SearchHit(chunk_id=1, document_id=1, title="Lei", content="texto", distance=0.1)
    state = LegalState(conn=None, question="Explique", query="explique",
                       hits=[hit] if name == "answer_internal" else [])

    asyncio.run(getattr(module, entry)(state))

    [(_system, content, options)] = calls
    assert "extra_input_tokens" not in options
    blocks = content if isinstance(content, list) else []
    assert all(b.get("title") != attachments.TITLE for b in blocks)


def test_a_dense_non_latin_attachment_is_reserved_at_least_its_token_count(monkeypatch):
    """Through the real `complete`: the budget is asked for the task's input
    ceiling plus the attachment's UTF-8 bytes. A byte-level tokenizer makes
    at most one token per byte, so that covers its tokens in any script; a
    count of characters would not (each of these is three or four bytes)."""
    reserved, sent = [], []

    async def reserve(input_ceiling, output_ceiling):
        reserved.append(input_ceiling)
        return None

    async def provider(system, user, model, json_mode, max_tokens, temperature):
        sent.append(user)
        return "ok", 10, 2

    monkeypatch.setattr(llm.budget, "reserve", reserve)
    monkeypatch.setattr(llm.budget, "settle", _nothing)
    monkeypatch.setattr(llm, "_openai_chat", provider)
    monkeypatch.setattr(llm, "_provider_for", lambda _task: "openai")

    asyncio.run(chat.chat(LegalState(conn=None, question="resuma", attachment_text=DENSE)))

    assert DENSE in sent[0]
    assert len(DENSE.encode("utf-8")) > 3 * len(DENSE)
    assert reserved == [settings.budget_worst_input_simple + len(DENSE.encode("utf-8"))]


# ── the bounded prompt ───────────────────────────────────────────────────────

def _document_chars(content: list[dict]) -> int:
    return sum(len(part["text"]) for block in content if block.get("type") == "document"
               for part in block["source"]["content"])


def test_the_attachment_is_cut_and_the_other_evidence_fits_beside_it():
    question = "q" * 4_000
    corpus = [document_block(f"Fonte {i}", "B" * 40_000) for i in range(3)]
    corpus.append({"type": "text", "text": question})
    state = LegalState(conn=None, question=question, attachment_text="A" * 100_000)

    bounded = attachments.content_for(state, corpus)

    assert bounded[0]["title"] == attachments.TITLE
    assert bounded[0]["source"]["content"][0]["text"] == "A" * 60_000
    # 90,000 less the attachment and the question: what the evidence may take.
    assert _document_chars(bounded) - 60_000 == 90_000 - 60_000 - 4_000
    assert bounded[-1] == {"type": "text", "text": question}
    assert all(len(b["source"]["content"][0]["text"]) == 40_000 for b in corpus[:3]), (
        "the caller's blocks are not modified")


def test_external_research_cuts_its_evidence_to_fit_the_attachment(monkeypatch):
    calls: list = []
    _stub_node(monkeypatch, external_research, calls)
    results = [serper_module.SearchResult(title=f"R{i}", url=f"https://www.stj.jus.br/{i}",
                                          snippet="EMENTA. " * 2_000) for i in range(10)]

    async def web(_query, provider=None):
        return results

    monkeypatch.setattr(external_research.websearch, "search", web)
    assert settings.external_research_max_context_chars > 30_000, "the test needs a cut"

    asyncio.run(external_research.research(LegalState(
        conn=None, question="o que diz a decisão?", query="decisão STJ",
        attachment_text="A" * 60_000)))

    [(_system, content, _options)] = calls
    texts = sum(len(b["text"]) for b in content if b.get("type") == "text")
    assert content[0]["title"] == attachments.TITLE
    assert _document_chars(content) + texts <= 90_000
    assert _document_chars(content) > 60_000, "some evidence still fits"


# ── the answer cache ─────────────────────────────────────────────────────────

class _Redis:
    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


@pytest.fixture
def shared_cache(monkeypatch):
    """The real answer cache over an in-memory Redis, and an internal
    research whose model answers from whatever attachment it was shown."""
    redis = _Redis()
    monkeypatch.setattr(settings, "cache_enabled", True)
    monkeypatch.setattr(cache, "_offline_until", 0.0)
    monkeypatch.setattr(cache, "_redis", lambda: redis)

    async def scope(*_args):
        return None

    async def embedding(_text):
        return [0.1]

    async def search(*_args, **_kwargs):
        return [SearchHit(chunk_id=1, document_id=1, title="Lei", content="texto da lei",
                          distance=0.1)]

    async def complete(system, content, **kwargs):
        text = llm.content_to_text(content)
        for secret in ("segredo A", "segredo B"):
            if secret in text:
                return f"Resposta com o {secret}"
        return "Resposta sem anexo"

    monkeypatch.setattr(graph, "scope_for", scope)
    monkeypatch.setattr(internal_research, "_embed_cached", embedding)
    monkeypatch.setattr(internal_research, "search_chunks", search)
    monkeypatch.setattr(internal_research, "complete", complete)
    monkeypatch.setattr(internal_research, "log_access", _nothing)
    monkeypatch.setattr(internal_research, "audience_for_hits", lambda *_a: "firm")
    return redis


def _ask(staff_id: int, attachment: str | None) -> AgentReply:
    route = TaskRoute(task="internal_research", query="o que diz o contrato")
    return asyncio.run(graph.run(None, "o que diz o contrato?", staff_id=staff_id,
                                 routed=route, attachment_text=attachment))


def test_the_cache_works_as_before_without_an_attachment(shared_cache):
    """The fixture's cache is live: the second person gets the first one's
    answer from it when neither attached anything."""
    first, second = _ask(4, None), _ask(5, None)
    assert (first.cached, second.cached) == (False, True)
    assert second.text == first.text


@pytest.mark.parametrize("first, second", [
    ("segredo A", None),         # an attachment answer is never written
    (None, "segredo B"),         # a cached answer is never read with one
    ("segredo A", "segredo B"),  # neither, both ways
])
def test_two_people_with_the_same_question_never_share_an_attachment_answer(
        shared_cache, first, second):
    rui, ana = _ask(4, first), _ask(5, second)

    assert rui.text == (f"Resposta com o {first}" if first else "Resposta sem anexo")
    assert ana.text == (f"Resposta com o {second}" if second else "Resposta sem anexo")
    assert not ana.cached
    assert not any("segredo" in value for value in shared_cache.store.values())
    assert len(shared_cache.store) == (0 if first and second else 1), (
        "only an answer without an attachment is written")


# ── the follow-up's attachment, in SQL ───────────────────────────────────────

async def _turn(conn, who: StaffMember, text: str, *, reading: str | None = "photo",
                minutes_ago: int = 0, channel: str = "panel") -> None:
    turn_id = await conversation.record_turn(
        conn, staff_id=who.id, chat_id=who.chat_id, speaker="bot", text=text,
        channel=channel, extras={"reading": reading} if reading else None)
    async with conn.cursor() as cur:
        await cur.execute("UPDATE conversation_turns SET created_at = NOW() - "
                          "make_interval(mins => %s) WHERE id = %s", (minutes_ago, turn_id))
    await conn.commit()


def test_a_follow_up_finds_the_reading_until_two_hours_and_nothing_after(db):
    async def body(conn):
        await _turn(conn, RUI, "Leitura da foto:\ncontrato", minutes_ago=119)
        within = await conversation.latest_attachment_text(conn, RUI.id)
        async with conn.cursor() as cur:
            await cur.execute("UPDATE conversation_turns SET created_at = "
                              "created_at - interval '2 minutes'")
        await conn.commit()
        return within, await conversation.latest_attachment_text(conn, RUI.id)

    within, after = db.run(body)

    assert within == "Leitura da foto:\ncontrato"
    assert after is None


def test_a_follow_up_finds_only_its_own_persons_latest_reading(db):
    async def body(conn):
        await _turn(conn, RUI, "Leitura da foto:\nsegredo do Rui", minutes_ago=30)
        await _turn(conn, RUI, "Transcrição (3 min):\nreunião do Rui", reading="audio",
                    minutes_ago=20)
        await _turn(conn, RUI, "resposta sobre a reunião", reading=None, minutes_ago=19)
        await _turn(conn, PAULO, "Leitura da foto:\nsegredo do Paulo", minutes_ago=1)
        return (await conversation.latest_attachment_text(conn, RUI.id),
                await conversation.latest_attachment_text(conn, 5))

    rui, ana = db.run(body)

    assert rui == "Transcrição (3 min):\nreunião do Rui"
    assert ana is None, "someone with no reading of their own gets nobody else's"


def test_a_voice_note_or_a_telegram_turn_is_no_attachment_and_a_long_one_is_cut(db):
    async def body(conn):
        await _turn(conn, RUI, "Leitura da foto:\n" + "x" * 70_000, minutes_ago=10)
        long = await conversation.latest_attachment_text(conn, RUI.id)
        await _turn(conn, RUI, '🎤 Entendi: "qual o prazo?"', reading="voice", minutes_ago=5)
        await _turn(conn, RUI, "Leitura de outro canal", channel="telegram", minutes_ago=1)
        return long, await conversation.latest_attachment_text(conn, RUI.id)

    long, latest = db.run(body)

    assert long is not None and len(long) == 60_000
    assert latest == long


# ── the handler carries it ───────────────────────────────────────────────────

@pytest.fixture
def served(db, monkeypatch):
    """The panel worker on this run's database with the real handler, and
    the answer replaced by a stub that records what it was given."""
    queued: list[dict] = []
    asked: list[tuple[str, str | None]] = []

    async def enqueue(update, *, deferred):
        queued.append(update)
        return True

    async def allow(*_args, **_kwargs):
        return True, False

    async def sent(*_args, **_kwargs):
        return 1

    class _Pool:
        def connection(self, timeout=None):
            @contextlib.asynccontextmanager
            async def opened():
                conn = await db.connect()
                try:
                    yield conn
                    await conn.commit()
                finally:
                    await conn.close()
            return opened()

    async def get_pool():
        return _Pool()

    async def answer_question(conn, question, **kwargs):
        asked.append((question, kwargs.get("attachment_text")))
        return AgentReply(text="resposta")

    async def complete_with_image(task, prompt, image_jpeg, *, max_tokens):
        return "Contrato de locação, R$ 2.000,00."

    for name in ("send_message", "send_document", "send_typing", "edit_message_text",
                 "answer_callback_query"):
        monkeypatch.setattr(channels, name, sent)
    monkeypatch.setattr(settings, "doc_master_key", document_crypto.new_master_key())
    monkeypatch.setattr(panel_intake.update_queue, "enqueue_update", enqueue)
    monkeypatch.setattr(work_hours, "is_office_hours", lambda: True)
    monkeypatch.setattr(legal_message, "_limiter", SimpleNamespace(check=lambda _c: (True, False)))
    monkeypatch.setattr(legal_message.cache, "rate_limit_hit", allow)
    monkeypatch.setattr(legal_message.settings, "conversational_actions", False)
    monkeypatch.setattr(legal_message, "get_pool", get_pool)
    monkeypatch.setattr(legal_message, "answer_question", answer_question)
    monkeypatch.setattr(media.llm, "complete_with_image", complete_with_image)
    return SimpleNamespace(queued=queued, asked=asked)


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (32, 24), (10, 120, 200)).save(out, "PNG")
    return out.getvalue()


def test_the_answer_to_a_photo_receives_its_reading_in_the_attachment_field(db, served):
    async def body(conn):
        upload_id = await panel_uploads.store_upload(
            conn, RUI.id, kind="photo", media_type="image/png", data=_png(), archive=False)
        intake = await panel_intake.submit_message(conn, RUI, "qual o valor?",
                                                   upload_id=upload_id)
        assert intake.outcome == "queued", intake
        await legal_message.process_legal_message(conn, served.queued[-1])
        await conn.commit()

    db.run(body)

    assert served.asked == [("qual o valor?",
                             "Leitura da foto:\nContrato de locação, R$ 2.000,00.")]


@pytest.fixture(params=["free text", "routed"])
def path(request, monkeypatch):
    """The two ways a sentence reaches the answer: today's free-text path,
    and the conversation router's task route."""
    if request.param == "routed":
        async def route(text, history, pending, today):
            return TaskRoute(task="internal_research", query=text)

        monkeypatch.setattr(legal_message.settings, "conversational_actions", True)
        monkeypatch.setattr(legal_message.conversation_router, "route", route)
    return request.param


@pytest.mark.parametrize("minutes_ago, carried", [(100, True), (125, False)])
def test_a_follow_up_in_the_panel_carries_the_attachment_for_two_hours(
        db, served, path, minutes_ago, carried):
    async def body(conn):
        await _turn(conn, RUI, "Leitura da foto:\ncontrato", minutes_ago=minutes_ago)
        intake = await panel_intake.submit_message(conn, RUI, "e qual o prazo do contrato?")
        assert intake.outcome == "queued", intake
        await legal_message.process_legal_message(conn, served.queued[-1])
        await conn.commit()

    db.run(body)

    expected = "Leitura da foto:\ncontrato" if carried else None
    assert served.asked == [("e qual o prazo do contrato?", expected)]


def test_a_photo_with_its_question_is_counted_once_by_the_limit(db, served, monkeypatch):
    counted = {"local": 0, "shared": 0}

    def check(_chat_id):
        counted["local"] += 1
        return True, False

    async def rate_limit_hit(*_args, **_kwargs):
        counted["shared"] += 1
        return True, False

    monkeypatch.setattr(legal_message, "_limiter", SimpleNamespace(check=check))
    monkeypatch.setattr(legal_message.cache, "rate_limit_hit", rate_limit_hit)

    async def body(conn):
        upload_id = await panel_uploads.store_upload(
            conn, RUI.id, kind="photo", media_type="image/png", data=_png(), archive=False)
        intake = await panel_intake.submit_message(conn, RUI, "qual o valor?",
                                                   upload_id=upload_id)
        assert intake.outcome == "queued", intake
        await legal_message.process_legal_message(conn, served.queued[-1])
        await conn.commit()

    db.run(body)

    assert len(served.asked) == 1
    assert counted == {"local": 1, "shared": 1}, "the media stage and the answer are one request"


def test_a_photo_gone_before_it_is_read_never_answers_with_an_older_one(db, served):
    async def body(conn):
        await _turn(conn, RUI, "Leitura da foto:\ncontrato antigo", minutes_ago=5)
        upload_id = await panel_uploads.store_upload(
            conn, RUI.id, kind="photo", media_type="image/png", data=_png(), archive=False)
        intake = await panel_intake.submit_message(conn, RUI, "e o vencimento?",
                                                   upload_id=upload_id)
        assert intake.outcome == "queued", intake
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM panel_uploads WHERE id = %s", (upload_id,))
        await conn.commit()
        await legal_message.process_legal_message(conn, served.queued[-1])
        await conn.commit()
        async with conn.cursor() as cur:
            await cur.execute("SELECT text FROM conversation_turns WHERE staff_id = %s "
                              "AND speaker = 'bot' ORDER BY id DESC LIMIT 1", (RUI.id,))
            row = await cur.fetchone()
        return row[0] if row else None

    last = db.run(body)

    assert served.asked == [], "no answer, and so no older attachment carried into one"
    assert last == "O anexo não está mais disponível. Anexe de novo e envie."


def test_telegram_never_carries_a_panel_attachment(db, served):
    async def body(conn):
        await _turn(conn, RUI, "Leitura da foto:\ncontrato", minutes_ago=1)
        await legal_message._answer_question(conn, "e o prazo?", staff_id=RUI.id,
                                             chat_id=RUI.chat_id, staff_name=RUI.name)

    db.run(body)

    assert served.asked == [("e o prazo?", None)]
