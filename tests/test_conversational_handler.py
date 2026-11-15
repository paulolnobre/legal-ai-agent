"""A sentence reaches the same commands a person would type.

The router is replaced by fixed decisions here; what is tested is what the
handler does with each: the switch, the grounding of the deadline regime, the
question and the pending step when something is missing, the command a
complete action becomes, and that it runs through that command's own
authorization and reply.
"""
import asyncio
from datetime import date, datetime

import pytest

from core.config import settings
from core.handlers import legal_message
from core.services import channels
from core.models import StaffMember
from core.repositories.conversation import Turn
from core.services import authorization
from core.services.legal_assistant import conversation_router as router
from core.services.legal_assistant.actions import CATALOGUE

pytestmark = pytest.mark.usefixtures(
    "isolate_message_rate_limits", "pending_store", "callback_store",
)

_PEOPLE = {
    "100": StaffMember(id=1, chat_id="100", name="Paulo", role="admin"),
    "500": StaffMember(id=5, chat_id="500", name="Bia", role="estagiario"),
}


@pytest.fixture
def bot(monkeypatch, pending_store):
    sent: list[str] = []
    decisions: list = []
    asked: list[dict] = []
    history: list[Turn] = []

    async def get_staff(_conn, chat_id):
        return _PEOPLE.get(str(chat_id))

    async def send(chat_id, message, reply_markup=None, *, preview=True):
        sent.append(message)
        return 1

    async def nothing(*_args, **_kwargs):
        return None

    async def no_holidays(_conn, _start, _end):
        return []

    async def recent(_conn, _staff_id, **_kwargs):
        return list(history)

    async def route(text, turns, pending, today):
        asked.append({"text": text, "pending": pending, "today": today, "turns": turns})
        return decisions.pop(0) if decisions else None

    monkeypatch.setattr(settings, "conversational_actions", True)
    monkeypatch.setattr(legal_message, "get_staff_by_chat_id", get_staff)
    monkeypatch.setattr(legal_message, "_send", send)
    monkeypatch.setattr(channels, "send_typing", nothing)
    monkeypatch.setattr(legal_message, "local_now", lambda *_: datetime(2026, 10, 6, 9, 0))
    monkeypatch.setattr(legal_message.firm_holidays, "holidays_between", no_holidays)
    monkeypatch.setattr(legal_message.conversation, "record_turn", nothing)
    monkeypatch.setattr(legal_message.conversation, "recent_turns", recent)
    monkeypatch.setattr(legal_message.conversation_router, "route", route)
    return {"sent": sent, "decisions": decisions, "asked": asked, "history": history,
            "pending": pending_store}


def _say(text: str, chat: int = 100):
    update = {"update_id": 1, "message": {
        "chat": {"id": chat, "type": "private"}, "from": {"id": chat}, "text": text,
    }}
    asyncio.run(legal_message.process_legal_message("connection", update))


def _deadline(**fields):
    return router.ActionRoute("deadline.calculate",
                              CATALOGUE["deadline.calculate"].arguments(**fields))


_WORKING = {"start": "05/10/2026", "days": 15, "regime": "úteis", "regime_quote": "úteis"}


def test_with_the_switch_off_prose_takes_todays_path(bot, monkeypatch):
    monkeypatch.setattr(settings, "conversational_actions", False)
    answered = []

    async def answer_question(*args, **kwargs):
        answered.append(kwargs)
        return legal_message.AgentReply(text="resposta")

    async def deliver(*_args, **_kwargs):
        return None

    monkeypatch.setattr(legal_message, "answer_question", answer_question)
    monkeypatch.setattr(legal_message, "_deliver", deliver)

    _say("qual o vencimento de 15 dias úteis da intimação de ontem?")

    assert bot["asked"] == []
    assert len(answered) == 1 and answered[0].get("routed") is None


def test_a_deadline_in_prose_is_answered_by_the_calculator(bot):
    bot["decisions"].append(_deadline(**_WORKING))

    _say("qual o vencimento de 15 dias úteis da intimação de ontem?")

    reply = bot["sent"][-1]
    assert reply.startswith("📅 Vencimento: 27/10/2026"), reply
    assert "05/10/2026" in reply, "the start date used is shown"
    assert bot["asked"][0]["today"] == date(2026, 10, 6)


def test_without_a_quoted_regime_it_asks_and_remembers(bot):
    bot["decisions"].append(_deadline(start="05/10/2026", days=15, regime="úteis",
                                      regime_quote="úteis"))

    _say("qual o vencimento de 15 dias da intimação de ontem?")

    assert "úteis ou corridos" in bot["sent"][-1]
    assert not any(text.startswith("📅") for text in bot["sent"])
    step = bot["pending"].rows["100"]
    assert step["kind"] == "action" and step["action"] == "deadline.calculate"
    assert step["args"]["days"] == 15 and step["args"]["regime"] is None


def test_the_answer_to_the_question_completes_the_pending_deadline(bot):
    bot["decisions"].append(_deadline(start="05/10/2026", days=15))
    _say("prazo de 15 dias da intimação de ontem")
    assert "úteis ou corridos" in bot["sent"][-1]

    # The router hears the pending step and answers only what was missing.
    bot["decisions"].append(_deadline(regime="úteis", regime_quote="úteis"))
    _say("úteis")

    assert bot["asked"][1]["pending"].action == "deadline.calculate"
    assert bot["sent"][-1].startswith("📅 Vencimento: 27/10/2026")
    assert "100" not in bot["pending"].rows


def test_e_em_dobro_after_a_deadline_doubles_it(bot):
    bot["history"].append(Turn("person", "15 dias úteis da intimação de 05/10/2026",
                               datetime(2026, 10, 6, 9)))
    bot["decisions"].append(_deadline(**_WORKING, doubled=True, doubled_quote="em dobro"))

    _say("e em dobro?")

    assert "Prazo em dobro: 15 dias contados como 30" in bot["sent"][-1]


def test_dobro_without_the_persons_words_is_not_doubled(bot):
    bot["decisions"].append(_deadline(**_WORKING, doubled=True, doubled_quote="em dobro"))

    _say("prazo de 15 dias úteis da intimação de 05/10")

    assert "Prazo em dobro" not in bot["sent"][-1]


def test_a_typed_command_while_an_action_is_pending_runs_as_typed(bot):
    bot["decisions"].append(_deadline(start="05/10/2026", days=15))
    _say("prazo de 15 dias da intimação de ontem")

    _say("/feriados")

    assert "100" not in bot["pending"].rows
    assert len(bot["asked"]) == 1, "a typed command never reaches the router"


def test_a_commands_only_request_names_the_command_and_executes_nothing(bot, monkeypatch):
    decided = []
    monkeypatch.setattr(legal_message, "_decide_access",
                        lambda *a, **k: decided.append(a))
    bot["decisions"].append(router.CommandOnly("/autorizar"))

    _say("autoriza o pedido do João e calcula o prazo")

    assert bot["sent"][-1] == ("Isso eu faço pelo comando /autorizar. "
                               "Envie /autorizar para começar.")
    assert decided == []


def test_a_question_to_ask_is_asked(bot):
    bot["decisions"].append(router.Clarify("Você quer o prazo ou os documentos?"))

    _say("e aquele da Acme?")

    assert bot["sent"][-1] == "Você quer o prazo ou os documentos?"


def test_prose_goes_through_the_commands_own_authorization(bot, monkeypatch):
    """Same refusal as typing it, because it is typing it."""
    monkeypatch.setitem(authorization._REQUIRED_RANK, "deadline.calculate",
                        legal_message.ROLE_ADMIN)

    _say("/prazo 05/10/2026 15 úteis", chat=500)
    typed = bot["sent"][-1]
    bot["decisions"].append(_deadline(**_WORKING))
    _say("prazo de 15 dias úteis da intimação de 05/10", chat=500)

    assert bot["sent"][-1] == typed
    assert not typed.startswith("📅")


def test_a_task_goes_to_the_graph_already_routed(bot, monkeypatch):
    """One routing call: the graph is told the task instead of asking again."""
    answered = []

    async def answer_question(*args, **kwargs):
        answered.append(kwargs)
        return legal_message.AgentReply(text="resposta")

    async def deliver(*_args, **_kwargs):
        return None

    monkeypatch.setattr(legal_message, "answer_question", answer_question)
    monkeypatch.setattr(legal_message, "_deliver", deliver)
    decision = router.TaskRoute(task="external_research", query="vício oculto CDC")
    bot["decisions"].append(decision)

    _say("o que diz o CDC sobre vício oculto?")

    assert answered[0]["routed"] == decision


def test_status_reports_a_pending_conversation_step(bot):
    bot["decisions"].append(_deadline(start="05/10/2026", days=15))
    _say("prazo de 15 dias da intimação de ontem")

    _say("/status")

    assert "/prazo" in bot["sent"][-1] and "/cancelar" in bot["sent"][-1]


def _orchestrate(monkeypatch, question, preset):
    from core.services.legal_assistant import orchestrator
    from core.services.legal_assistant.state import LegalState

    calls = []

    async def complete_json(*args, **kwargs):
        calls.append(args)
        return {"task": "chat", "query": question}

    monkeypatch.setattr(orchestrator, "complete_json", complete_json)
    state = LegalState(conn=None, question=question, query="consulta reescrita",
                       preset_task=preset)
    return asyncio.run(orchestrator.orchestrate(state)), calls


def test_a_preset_task_is_kept_without_asking_the_model_again(monkeypatch):
    out, calls = _orchestrate(monkeypatch, "o que diz o CDC sobre vício oculto?",
                              "external_research")

    assert calls == []
    assert out == {"task": "external_research", "query": "consulta reescrita",
                   "routed_by": "conversation"}


def test_a_preset_task_still_obeys_the_fixed_rules(monkeypatch):
    """A CNJ number goes to the public sources; a one-line "revise" searches."""
    cnj, _ = _orchestrate(monkeypatch, "o processo 1000000-00.2024.8.26.0100 no acervo",
                          "internal_research")
    short, _ = _orchestrate(monkeypatch, "revise aquele contrato", "review")

    assert cnj["task"] == "external_research"
    assert short["task"] == "internal_research"


def test_one_sentence_counts_once_against_the_rate_limit(bot, monkeypatch):
    """The command built from it is the same request, not a second one."""
    from types import SimpleNamespace

    hits = []

    def check(chat_id):
        hits.append(chat_id)
        return True, False

    monkeypatch.setattr(legal_message, "_limiter", SimpleNamespace(check=check))
    bot["decisions"].append(_deadline(**_WORKING))

    _say("prazo de 15 dias úteis da intimação de 05/10")

    assert len(hits) == 1
    assert bot["sent"][-1].startswith("📅 Vencimento")


# ── found in the final review ────────────────────────────────────────────────


def test_a_failed_routing_is_not_tried_twice(bot, monkeypatch):
    """None for the pending step must send the message on today's path, not
    to a second router call that might execute what the first refused."""
    answered = []

    async def answer_question(*args, **kwargs):
        answered.append(kwargs)
        return legal_message.AgentReply(text="resposta")

    async def deliver(*_args, **_kwargs):
        return None

    monkeypatch.setattr(legal_message, "answer_question", answer_question)
    monkeypatch.setattr(legal_message, "_deliver", deliver)
    bot["decisions"].append(_deadline(start="05/10/2026", days=15))
    _say("prazo de 15 dias da intimação de ontem")

    bot["decisions"].extend([None, _deadline(**_WORKING)])
    _say("hmm, depois vejo isso")

    assert len(bot["asked"]) == 2
    assert not any(text.startswith("📅") for text in bot["sent"])
    assert len(answered) == 1


@pytest.mark.parametrize("sentence", [
    "autoriza o João e lista os documentos",
    "lista os usuários",
])
def test_with_conversation_on_the_router_hears_it_before_any_read_runs(bot, monkeypatch, sentence):
    """The fixed-word reads ran before the router and executed a request the
    router would have answered with its command."""
    reads = []

    async def catalog(*args, **kwargs):
        reads.append("catalog")

    async def directory(*args, **kwargs):
        reads.append("directory")

    monkeypatch.setattr(legal_message, "_send_document_catalog", catalog)
    monkeypatch.setattr(legal_message, "_send_staff_directory", directory)
    bot["decisions"].append(router.CommandOnly("/autorizar"))

    _say(sentence)

    assert reads == []
    assert len(bot["asked"]) == 1


def test_greetings_still_need_no_model_call(bot):
    _say("bom dia")

    assert bot["asked"] == []
    assert bot["sent"][-1].startswith("Olá, Paulo!")
