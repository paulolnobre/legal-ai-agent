"""What the agent may do from a sentence, and the command each one becomes.

The router reads a sentence and names an entry here with its arguments. The
entry turns them into the command a person would type, and that command goes
through the same handler, the same authorization and the same reply as if it
had been typed. There is no second path doing the same thing, which is where
differences and permission gaps are born.

Shaped like tool definitions (a name, a description, an argument schema), so
a tool-calling agent could one day replace the router without this file
changing. It is deliberately small: a tool that uses AI, with limited
functions that widen over time.

The router is not trusted with one thing: the deadline regime. Civil and
labour deadlines run in working days, criminal ones in calendar days, and a
wrong guess is a missed deadline. So `ground` keeps "úteis"/"corridos" (and
"dobro") only when the router quotes words the person really wrote.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.services.deadline_request import _CALENDAR, _WORKING, MAX_DAYS, REGIME_QUESTION


class _Arguments(BaseModel):
    # The router may not add fields of its own, and a value of the wrong JSON
    # type is refused rather than coerced: true is not document 1.
    model_config = ConfigDict(extra="forbid", strict=True)


def _whole_number(value):
    """Digits written as text are that number: the real model sent
    {"document_id": "12"}. Only ASCII digits, so "١٢" or "15 dias" are still
    refused, and so is true (strict mode keeps bool out of int)."""
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        return int(value.strip())
    return value


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


class DeadlineArguments(_Arguments):
    start: date | None = Field(None, description="Data da intimação ou publicação, dd/mm/aaaa.")
    days: int | None = Field(None, ge=1, le=MAX_DAYS, description="Quantos dias de prazo.")
    regime: Literal["úteis", "corridos"] | None = Field(
        None, description="Só se a pessoa disse: úteis ou corridos.")
    regime_quote: str | None = Field(
        None, description="As palavras exatas da pessoa que dizem o regime.")
    doubled: bool = Field(False, description="Prazo em dobro, só se a pessoa pediu.")
    doubled_quote: str | None = Field(
        None, description="As palavras exatas da pessoa que pedem o dobro.")

    _days_as_number = field_validator("days", mode="before")(_whole_number)

    @field_validator("start", mode="before")
    @classmethod
    def _read_date(cls, value):
        # Strict mode reads no string as a date, so both written forms are
        # parsed here; anything else is refused.
        if isinstance(value, str):
            text = value.strip()
            if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", text):
                return datetime.strptime(text, "%d/%m/%Y").date()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                return date.fromisoformat(text)
        return value


class DocumentListArguments(_Arguments):
    filters: str = Field("", description=(
        "O recorte pedido, como a pessoa diria ao /docs: tipo, cliente, período. "
        "Vazio para listar tudo."))


class DocumentDetailsArguments(_Arguments):
    document_id: int | None = Field(None, ge=1, description="O número do documento no acervo.")

    _id_as_number = field_validator("document_id", mode="before")(_whole_number)


class LawsuitArguments(_Arguments):
    number: str | None = Field(None, description="O número CNJ do processo.")


class NoArguments(_Arguments):
    pass


@dataclass(frozen=True)
class CatalogueAction:
    name: str
    command: str
    authorization: str | None
    needs_confirmation: bool
    description: str
    arguments: type[BaseModel]
    to_command: Callable[[BaseModel], str]
    missing: Callable[[BaseModel], str | None]


def _deadline_command(args) -> str:
    return (f"/prazo {args.start:%d/%m/%Y} {args.days} {args.regime}"
            + (" dobro" if args.doubled else ""))


def _deadline_missing(args) -> str | None:
    if args.start is None:
        return ("A partir de que data? Diga a data da intimação ou da publicação, "
                "como 05/10/2026.")
    if args.days is None:
        return "Quantos dias de prazo?"
    if args.regime is None:
        return REGIME_QUESTION
    return None


def _never_missing(_args) -> str | None:
    return None


CATALOGUE: dict[str, CatalogueAction] = {
    entry.name: entry for entry in (
        CatalogueAction(
            name="deadline.calculate", command="/prazo", authorization="deadline.calculate",
            needs_confirmation=False,
            description="Calcula o vencimento de um prazo processual a partir de uma data.",
            arguments=DeadlineArguments, to_command=_deadline_command,
            missing=_deadline_missing,
        ),
        CatalogueAction(
            name="documents.list", command="/docs", authorization="document.browse",
            needs_confirmation=False,
            description="Lista os documentos do acervo, com ou sem recorte.",
            arguments=DocumentListArguments,
            to_command=lambda args: _one_line(f"/docs {args.filters}"),  # type: ignore[attr-defined]
            missing=_never_missing,
        ),
        CatalogueAction(
            name="document.details", command="/detalhes",
            authorization="document.view_details", needs_confirmation=False,
            description="Mostra tipo, cliente e data de um documento do acervo.",
            arguments=DocumentDetailsArguments,
            to_command=lambda args: f"/detalhes {args.document_id}",  # type: ignore[attr-defined]
            missing=lambda args: (None if args.document_id is not None  # type: ignore[attr-defined]
                                  else "Qual é o número do documento?"),
        ),
        CatalogueAction(
            name="lawsuit.status", command="/processo", authorization=None,
            needs_confirmation=False,
            description="Situação e movimentações de um processo pelo número CNJ.",
            arguments=LawsuitArguments,
            to_command=lambda args: _one_line(f"/processo {args.number}"),  # type: ignore[attr-defined]
            missing=lambda args: (None if args.number  # type: ignore[attr-defined]
                                  else "Qual é o número CNJ do processo?"),
        ),
        CatalogueAction(
            name="holidays.list", command="/feriados", authorization="deadline.calculate",
            needs_confirmation=False,
            description="Lista os feriados e suspensões locais cadastrados pelo escritório.",
            arguments=NoArguments, to_command=lambda _args: "/feriados",
            missing=_never_missing,
        ),
    )
}

# Asked for in a sentence, these are answered with the command and never run:
# access, accounts, sign-in, and what deletes or takes over. Topic -> command.
COMMANDS_ONLY: dict[str, str] = {
    "autorizar um pedido de acesso": "/autorizar",
    "recusar um pedido de acesso": "/recusar",
    "ver pedidos de acesso pendentes": "/pendentes",
    "listar usuários e papéis": "/usuarios",
    "promover alguém": "/promover",
    "rebaixar alguém": "/rebaixar",
    "revogar o acesso de um usuário": "/excluir_usuario",
    "apagar os dados de uma pessoa (LGPD)": "/esquecer",
    "link de acesso às páginas web": "/web",
    "autenticador e MFA": "/mfa",
    "cadastrar ou remover o acesso ao PJe": "/pje",
    "conectar a conta Microsoft": "/microsoft",
    "excluir um documento do acervo": "/excluir_documento",
    "assumir o documento de outra pessoa": "/assumir",
    "ingerir uma peça por URL oficial": "/ingerir",
    "importar do balde de documentos": "/importar",
}


def _fold(text: str) -> str:
    folded = unicodedata.normalize("NFKD", str(text).casefold())
    return " ".join("".join(c for c in folded if not unicodedata.combining(c)).split())


# The words that state each claim, as /prazo itself reads them (folded).
_SAYS = {
    "úteis": _WORKING,
    "corridos": _CALENDAR,
    "dobro": frozenset({"dobro", "dobrado", "dobrada"}),
}


def grounded(quote: str | None, person_texts: list[str]) -> bool:
    """The quote, folded for case and accents, appears in what the person wrote."""
    wanted = _fold(quote or "")
    return bool(wanted) and any(wanted in _fold(text) for text in person_texts)


def _states(quote: str | None, claim: str) -> bool:
    """The quote itself says the claim: "dias úteis" says úteis, "prazo"
    says nothing, and "corridos" never says úteis."""
    words = set(re.findall(r"\w+", _fold(quote or "")))
    return bool(words & _SAYS[claim])


def ground(action: str, args: BaseModel, person_texts: list[str]) -> BaseModel:
    """Drop what the router claims the person said and they did not.

    Two tests, both required: the quote is in the person's own words, and the
    quote states that very claim. A quote that is merely present ("prazo")
    proves nothing.
    """
    if action != "deadline.calculate":
        return args
    regime, regime_quote = args.regime, args.regime_quote  # type: ignore[attr-defined]
    doubled_quote = args.doubled_quote  # type: ignore[attr-defined]
    changes: dict = {}
    if regime is not None and not (grounded(regime_quote, person_texts)
                                   and _states(regime_quote, regime)):
        changes["regime"] = None
    if not (grounded(doubled_quote, person_texts) and _states(doubled_quote, "dobro")):
        changes["doubled"] = False
    return args.model_copy(update=changes)


__all__ = [
    "CATALOGUE", "COMMANDS_ONLY", "CatalogueAction", "DeadlineArguments", "ground",
    "grounded",
]
