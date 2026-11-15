"""One model call that decides what a sentence asks for, given the
conversation so far.

It answers with one of four things: a research/drafting/review task (what
the orchestrator decided before), an action from the catalogue with its
arguments, a question to ask, or the command to use for something only a
command does. Its answer is never trusted as it comes: anything outside the
catalogue, arguments that do not fit, an unknown task, or output that is not
JSON make `route` return None, and the message takes today's path. Nothing
runs on a malformed answer.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from typing import get_args

from pydantic import BaseModel, ValidationError

from core.config import settings
from core.integrations.llm import complete_json
from core.repositories.conversation import Turn
from core.repositories.pending_actions import PendingAction

from . import prompts
from .actions import CATALOGUE, COMMANDS_ONLY
from .state import Task

logger = logging.getLogger(__name__)

_TASKS = frozenset(get_args(Task))
_MAX_QUESTION = 500
_WEEKDAYS = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
             "sexta-feira", "sábado", "domingo")


@dataclass(frozen=True)
class TaskRoute:
    task: Task
    query: str


@dataclass(frozen=True)
class ActionRoute:
    name: str
    args: BaseModel


@dataclass(frozen=True)
class Clarify:
    question: str


@dataclass(frozen=True)
class CommandOnly:
    command: str


Routed = TaskRoute | ActionRoute | Clarify | CommandOnly


def _catalogue_text() -> str:
    lines = ["ACTIONS (name: what it does; arguments):"]
    for name, entry in CATALOGUE.items():
        fields = "; ".join(
            f"{field} ({info.description or 'sem descrição'})"
            for field, info in entry.arguments.model_fields.items()
        ) or "no arguments"
        lines.append(f"- {name}: {entry.description} Arguments: {fields}")
    lines.append("\nCOMMANDS ONLY (topic: command):")
    lines.extend(f"- {topic}: {command}" for topic, command in COMMANDS_ONLY.items())
    return "\n".join(lines)


def system_prompt() -> str:
    # The output format is the last word: with the orchestrator's query rules
    # last, the real model answered {"kind": "query"} (seen 2026-10-08).
    return (prompts.CONVERSATION_ROUTER + "\n\n" + _catalogue_text() + "\n\n"
            + prompts.CONVERSATION_ROUTER_OUTPUT)


def _user_prompt(text: str, history: list[Turn], pending: PendingAction | None,
                 today: date) -> str:
    parts = [f"Today: {today:%d/%m/%Y} ({_WEEKDAYS[today.weekday()]})"]
    if history:
        parts.append("Conversation (oldest first):\n" + "\n".join(
            f"{'Pessoa' if turn.speaker == 'person' else 'Bot'}: {turn.text}"
            for turn in history))
    if pending is not None and pending.kind == "action" and pending.action:
        parts.append(f"Pending step: {pending.action} with "
                     f"{json.dumps(pending.args or {}, ensure_ascii=False)}")
    parts.append(f"Message:\n{text}")
    return "\n\n".join(parts)


def _read(answer, text: str) -> Routed | None:
    if not isinstance(answer, dict):
        return None
    kind = answer.get("kind")
    if kind == "task":
        task = answer.get("task")
        if not isinstance(task, str) or task not in _TASKS:
            return None
        query = answer.get("query")
        if query is not None and not isinstance(query, str):
            return None
        query = (query or "").strip() or text
        return TaskRoute(task=task, query=query)  # type: ignore[arg-type]
    if kind == "action":
        entry = CATALOGUE.get(str(answer.get("action")))
        arguments = answer.get("arguments", {})
        if entry is None or not isinstance(arguments, dict):
            return None
        try:
            return ActionRoute(name=entry.name, args=entry.arguments(**arguments))
        except (ValidationError, TypeError):
            return None
    if kind == "clarify":
        question = answer.get("question")
        if not isinstance(question, str):
            return None
        question = question.strip()
        return Clarify(question) if 0 < len(question) <= _MAX_QUESTION else None
    if kind == "command_only":
        command = answer.get("command")
        if not isinstance(command, str) or command not in COMMANDS_ONLY.values():
            return None
        return CommandOnly(command)
    return None


async def route(text: str, history: list[Turn], pending: PendingAction | None,
                today: date) -> Routed | None:
    """What the message asks for, or None for "take today's path"."""
    try:
        answer = await complete_json(
            system_prompt(), _user_prompt(text, history, pending, today),
            task="json", model=settings.router_model, max_tokens=512,
        )
    except Exception as exc:
        # By kind only: a provider error can carry the request it failed on.
        logger.warning("Conversation routing failed: %s", type(exc).__name__)
        return None
    decision = _read(answer, text)
    if decision is None:
        logger.info("Conversation routing gave no usable answer; taking today's path")
    else:
        logger.info("Conversation routed to %s", type(decision).__name__)
    return decision


__all__ = [
    "ActionRoute", "Clarify", "CommandOnly", "Routed", "TaskRoute", "route", "system_prompt",
]
