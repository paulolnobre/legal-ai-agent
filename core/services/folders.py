"""Moving a document between folders: one place decides, for every door.

The bot's /compartilhar and the panel's explorer both move documents, and a
rule kept twice drifts. So the rule lives here, returns its verdict with the
reason in the words the person reads, and the move itself is written to the
access log in the same transaction: a move with no record, or a record of a
move that did not happen, is not possible.

The rules, as the firm decided them on 2026-09-22:
- the person who sent a document moves it; lawyers and above move any, except
  out of the firm's shelf, which is the sender's or an admin's;
- only the sender may make it personal again;
- moving into a team needs membership, except for admins and above.
And underneath them, the one this module adds: nobody moves a document they
cannot see, whatever the destination.
"""
from dataclasses import dataclass
from typing import Literal

from psycopg import pq

from core.repositories.documents import (
    MAX_DOC_TYPE_CHARS,
    MAX_MATTER_CHARS,
    MAX_TITLE_CHARS,
    folder_in_scope,
    log_access,
    move_document,
    set_fields,
)
from core.legislation import SPHERES, collapse
from core.repositories.staff import ROLE_ADMIN, ROLE_LAWYER, has_rank
from core.repositories.teams import Scope, find_team, teams_of
from core.services import authorization

FIRM_WORDS = frozenset({"escritorio", "escritório", "firma", "todos", "geral"})
PRIVATE_WORDS = frozenset({"privado", "particular", "pessoal", "so eu", "somente eu"})


@dataclass(frozen=True)
class Destination:
    kind: Literal["private", "team", "firm"]
    team_id: int | None
    label: str


FIRM = Destination("firm", None, "escritório")
PRIVATE = Destination("private", None, "pessoal")


def team(team_id: int, name: str) -> Destination:
    return Destination("team", team_id, f"equipe {name}")


async def resolve_destination(conn, words: str) -> Destination | None:
    """The folder a person named, as the bot hears it; None for an unknown team."""
    word = words.strip().casefold()
    if word in FIRM_WORDS:
        return FIRM
    if word in PRIVATE_WORDS:
        return PRIVATE
    found = await find_team(conn, words)
    return team(*found) if found else None


@dataclass(frozen=True)
class MoveDecision:
    allowed: bool
    reason: str = ""
    # An admin moving into a team they are not in: team folders are members'
    # only, whatever the rank, so the document leaves their view. The panel
    # says so before the move rather than letting a card vanish.
    hides_from_mover: bool = False
    title: str = ""
    sender: bool = False


def _not_found(document_id) -> str:
    return f"Não encontrei o documento {document_id} no seu acervo."


async def check_source(conn, staff, scope: Scope, document_id: int, *,
                       lock: bool = False) -> MoveDecision:
    """Whether this person may move this document anywhere at all.

    Asked before the destination, so a document the person cannot see is
    "not found" whatever they name as the destination.
    """
    permitted = authorization.decide(staff, "document.share")
    if not permitted.allowed:
        return MoveDecision(False, permitted.reason)

    folder = await folder_in_scope(conn, document_id, scope, lock=lock)
    if folder is None:
        return MoveDecision(False, _not_found(document_id))

    sender = folder.uploaded_by == staff.id
    if folder.visibility == "firm":
        # The firm's shelf is everybody's: a law, a model pleading, a
        # precedent the whole office relies on. Taking something off it is a
        # decision about what the office keeps, not about one's own papers,
        # so it is the sender's or an admin's, never any lawyer's. A document
        # nobody sent (the shared inbox, the corpus) has no sender: admins.
        if not sender and not has_rank(staff.role, ROLE_ADMIN):
            return MoveDecision(
                False,
                "Este documento está na pasta do escritório. Só quem o enviou, ou "
                "um admin, pode tirá-lo de lá.",
            )
    elif not sender and not has_rank(staff.role, ROLE_LAWYER):
        return MoveDecision(
            False,
            "Só quem enviou o documento pode movê-lo de pasta. Fale com quem o enviou, "
            "ou peça a um advogado.",
        )
    return MoveDecision(True, title=folder.title, sender=sender)


async def decide_move(conn, staff, scope: Scope, document_id: int,
                      destination: Destination, *, lock: bool = False) -> MoveDecision:
    """Whether this person may move this document there, and why not."""
    source = await check_source(conn, staff, scope, document_id, lock=lock)
    if not source.allowed:
        return source
    if destination.kind == "private" and not source.sender:
        return MoveDecision(False, "Só quem enviou o documento pode torná-lo pessoal de novo.")

    hides = False
    if destination.kind == "team":
        mine = {team_id for team_id, _ in await teams_of(conn, staff.id)}
        if destination.team_id not in mine:
            if not has_rank(staff.role, ROLE_ADMIN):
                return MoveDecision(
                    False,
                    f"Você não está na {destination.label}, então não pode mover para ela.",
                )
            hides = True
    return MoveDecision(True, hides_from_mover=hides, title=source.title,
                        sender=source.sender)


@dataclass(frozen=True)
class MoveResult:
    moved: bool
    reason: str = ""
    title: str = ""
    label: str = ""


async def move(conn, staff, scope: Scope, document_id: int, destination: Destination,
               *, chat_id=None) -> MoveResult:
    """Decide, move and record, committed together or not at all.

    The decision is taken again here, with the row locked, rather than
    trusted from whatever screen offered the destination: a CSRF token proves
    where a request came from, not that it is still allowed.

    A database failure is not turned into a refusal: the transaction rolls
    back and the error goes up, so the bot's worker retries the update.
    """
    # Whatever the caller did on this connection is committed first, as every
    # repository write here does by default, so the transaction below is this
    # move's alone: its rollback cannot discard the caller's work, and its
    # commit is not left to a caller who might not make one.
    if conn.info.transaction_status != pq.TransactionStatus.IDLE:
        await conn.commit()
    async with conn.transaction():
        decision = await decide_move(conn, staff, scope, document_id, destination,
                                     lock=True)
        if not decision.allowed:
            return MoveResult(False, decision.reason)
        moved = await move_document(
            conn, document_id, visibility=destination.kind,
            team_id=destination.team_id, scope=scope, commit=False,
        )
        if not moved:
            return MoveResult(False, f"Não consegui mover o documento {document_id}.")
        await log_access(
            conn, action="share", staff_id=staff.id, chat_id=chat_id,
            query=destination.label, document_ids=[document_id], commit=False,
        )
    return MoveResult(True, title=decision.title, label=destination.label)


@dataclass(frozen=True)
class EditResult:
    changed: bool
    reason: str = ""
    conflict: bool = False


_LIMITS = {"title": MAX_TITLE_CHARS, "doc_type": MAX_DOC_TYPE_CHARS,
           "matter": MAX_MATTER_CHARS, "sphere": max(len(s) for s in SPHERES)}
_WHAT = {"title": "O nome", "doc_type": "O tipo", "matter": "O cliente ou caso",
         "sphere": "A esfera"}
_EMPTY = {"doc_type": "(sem tipo)", "matter": "(sem cliente)", "sphere": "(sem esfera)"}


def tidy(text: str) -> str | None:
    """A field as typed, its spacing tidied; empty is None. Tidied because the
    explorer files by exact name: "Acme " and "Acme" would otherwise be two
    folders that look like one."""
    # The same spacing rule as the one that decides what is legislation, so
    # a type tidied on saving is filed where the page showed it would be.
    return collapse(text) or None


async def edit_document(conn, staff, scope: Scope, document_id: int,
                        changes: dict[str, str | None], *,
                        expected: dict[str, str | None] | None = None,
                        chat_id=None) -> EditResult:
    """Change how a document is described: its name, its type, the client or
    case it is filed under. Only the fields in `changes` are written; None
    empties a type or a client. Recorded in the same transaction.

    `expected`, what those fields held when the person started editing, is
    checked by the write itself, under the row lock: if a colleague changed
    one of them since, nothing is written and the person is told.

    The rule is `/editar`'s: lawyers and above, on any document they can see.
    It is the label, not the folder; it changes who can read nothing, so the
    shelf rules that moving follows do not apply.
    """
    permitted = authorization.decide(staff, "document.edit")
    if not permitted.allowed:
        return EditResult(False, permitted.reason)
    if not changes:
        return EditResult(False, "Nada mudou.")
    if "title" in changes and not changes["title"]:
        return EditResult(False, "O documento precisa de um nome.")
    if changes.get("sphere") is not None and changes["sphere"] not in SPHERES:
        return EditResult(False, "A esfera é federal, estadual ou municipal.")
    for field, value in changes.items():
        if value is not None and len(value) > _LIMITS[field]:
            # Refused rather than cut: a name saved shorter than it was typed
            # is a label nobody can find again by that name.
            return EditResult(False, f"{_WHAT[field]} tem {len(value)} caracteres e o "
                                     f"limite é {_LIMITS[field]}.")
    if conn.info.transaction_status != pq.TransactionStatus.IDLE:
        await conn.commit()
    async with conn.transaction():
        if await folder_in_scope(conn, document_id, scope, lock=True) is None:
            return EditResult(False, _not_found(document_id))
        if not await set_fields(conn, document_id, changes, scope=scope,
                                expected=expected, commit=False):
            if expected:
                return EditResult(False, "Outra pessoa alterou este documento enquanto você "
                                         "editava. Abra a edição de novo para ver como ele "
                                         "está agora.", conflict=True)
            return EditResult(False, f"Não consegui alterar o documento {document_id}.")
        # The same shape as /editar's entry, so the history reads one way.
        await log_access(
            conn, action="edit_document", staff_id=staff.id, chat_id=chat_id,
            query="; ".join(f"{field}={value if value is not None else _EMPTY[field]}"
                            for field, value in changes.items())[:500],
            document_ids=[document_id], commit=False,
        )
    return EditResult(True)


__all__ = [
    "Destination", "EditResult", "FIRM", "FIRM_WORDS", "MoveDecision", "MoveResult",
    "PRIVATE", "PRIVATE_WORDS", "check_source", "decide_move", "edit_document",
    "move", "resolve_destination", "team", "tidy",
]
