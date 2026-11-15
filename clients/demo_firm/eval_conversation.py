"""Real phrasings against the real router model, before turning it on.

CONVERSATIONAL_ACTIONS stays off until every line here passes. This needs the
provider key, so it runs inside the production container, never in CI:

    docker compose ... exec -T legal-bot python clients/demo_firm/eval_conversation.py

It prints one line per phrase, PASS or FAIL with what the router decided, and
exits non-zero if any failed. It sends the phrases below and nothing else:
no client data, no conversation from the database.
"""
import asyncio
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

from core.repositories.conversation import Turn  # noqa: E402
from core.services.legal_assistant import conversation_router as router  # noqa: E402
from core.services.legal_assistant.actions import ground  # noqa: E402
from core.services.legal_assistant.intents import (  # noqa: E402
    classify_local_intent,
    parse_document_filters,
)

TODAY = date(2026, 10, 6)
_AT = datetime(2026, 10, 6, 9, 0)

_FIRST = "qual o vencimento de 15 dias úteis da intimação de ontem?"


@dataclass(frozen=True)
class Case:
    phrase: str
    kind: str                       # action, task, command_only, clarify, local
    name: str | None = None         # action name, command, or local intent
    expect: dict = field(default_factory=dict)   # argument -> value, after grounding
    history: tuple[Turn, ...] = ()
    # documents.list only: what /docs reads from the filters the router wrote.
    reads_as: dict = field(default_factory=dict)


CASES = (
    Case(_FIRST, "action", "deadline.calculate",
         {"start": date(2026, 10, 5), "days": 15, "regime": "úteis", "doubled": False}),
    Case("prazo de 15 dias a partir de hoje", "action", "deadline.calculate",
         {"start": TODAY, "days": 15, "regime": None}),
    Case("e em dobro?", "action", "deadline.calculate",
         {"days": 15, "regime": "úteis", "doubled": True},
         history=(Turn("person", _FIRST, _AT),
                  Turn("bot", "📅 Vencimento: 27/10/2026 (terça-feira)", _AT))),
    Case("me mostra os contratos da Acme", "action", "documents.list",
         reads_as={"doc_type": "contrato", "matter": "Acme"}),
    Case("detalhes do documento 12", "action", "document.details", {"document_id": 12}),
    Case("como está o processo 1000000-00.2024.8.26.0100?", "action", "lawsuit.status",
         {"number": "1000000-00.2024.8.26.0100"}),
    Case("quais feriados estão cadastrados?", "action", "holidays.list"),
    Case("autoriza o pedido do João", "command_only", "/autorizar"),
    Case("o que diz o CDC sobre vício oculto?", "task"),
    Case("bom dia", "local", "greeting"),
)


async def _check(case: Case) -> tuple[bool, str]:
    if case.kind == "local":
        got = classify_local_intent(case.phrase)
        return got == case.name, f"local intent {got}"
    decision = await router.route(case.phrase, list(case.history), None, TODAY)
    if case.kind == "task":
        return isinstance(decision, router.TaskRoute), repr(decision)
    if case.kind == "command_only":
        ok = isinstance(decision, router.CommandOnly) and decision.command == case.name
        return ok, repr(decision)
    if not isinstance(decision, router.ActionRoute) or decision.name != case.name:
        return False, repr(decision)
    person_texts = [t.text for t in case.history if t.speaker == "person"] + [case.phrase]
    args = ground(decision.name, decision.args, person_texts)
    wrong = {key: getattr(args, key, None) for key, value in case.expect.items()
             if getattr(args, key, None) != value}
    if case.reads_as:
        # The filters only help if /docs understands them.
        read = parse_document_filters(getattr(args, "filters", ""))
        wrong |= {f"/docs {key}": getattr(read, key, None)
                  for key, value in case.reads_as.items() if getattr(read, key, None) != value}
    return not wrong, f"{decision.name} {args.model_dump()}" + (f" WRONG {wrong}" if wrong else "")


async def main() -> int:
    failed = 0
    for case in CASES:
        ok, said = await _check(case)
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {case.phrase!r}\n      {said}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
