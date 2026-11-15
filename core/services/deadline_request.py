"""Reading "/prazo 06/10/2026 15 úteis" into something the calculator takes.

The calculator (`core/services/deadlines.py`) refuses to guess the counting
regime, because civil and labour deadlines run in working days (CPC art. 219)
and criminal ones in calendar days (CPP art. 798), and a wrong guess is a
missed deadline. This reader keeps that promise at the door: a request
without "úteis" or "corridos" is not completed with a default, it is answered
with a question.

Order does not matter and filler words are ignored, so "/prazo 15 dias úteis a
partir de 06/10" reads the same as "/prazo 06/10 15 úteis". A doubled
deadline (Fazenda Pública, Defensoria, litisconsortes with different
lawyers: CPC arts. 180, 183, 186 and 229) is asked for with "dobro"; it is
never inferred, because whether it applies is a fact about the case.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

from core.services.deadlines import Prazo, Regime

MAX_DAYS = 3650

# ASCII digits only: str.isdigit and \d also accept characters such as "❶" or
# Arabic-Indic digits, which then either crash int() or read as a number
# nobody typed on purpose.
_DATE = re.compile(r"^([0-9]{1,2})[/.-]([0-9]{1,2})(?:[/.-]([0-9]{2}|[0-9]{4}))?$")
_DAYS = re.compile(r"^[0-9]{1,4}$")
# A date outside this window is a typo, and near date.max it would also
# overflow the calculator's look-ahead.
YEARS_AROUND_TODAY = 10
_WORKING = {"uteis", "util", "du"}
_CALENDAR = {"corridos", "corrido", "dc"}


@dataclass(frozen=True)
class DeadlineRequest:
    start: date
    days: int
    regime: Regime
    doubled: bool


@dataclass(frozen=True)
class Incomplete:
    """What is missing or wrong, in words the person can act on."""

    message: str


def _plain(word: str) -> str:
    folded = unicodedata.normalize("NFKD", word.casefold())
    return "".join(c for c in folded if not unicodedata.combining(c)).strip(".,;:")


def parse_date(word: str, today: date) -> date | None:
    """A dd/mm/aaaa, dd/mm (this year) or "hoje"; None when it is not one."""
    word = _plain(word)
    if word == "hoje":
        return today
    match = _DATE.match(word)
    if not match:
        return None
    day, month, year = match.groups()
    full_year = today.year if year is None else int(year) + (2000 if len(year) == 2 else 0)
    if abs(full_year - today.year) > YEARS_AROUND_TODAY:
        return None
    try:
        return date(full_year, int(month), int(day))
    except ValueError:
        return None


# The refusal to guess the regime, in the words both doors use: the command,
# and a sentence the agent read (core/services/legal_assistant/actions.py).
REGIME_QUESTION = (
    "São dias úteis ou corridos? Não escolho por você: no processo civil e "
    "no trabalhista o prazo corre em dias úteis (CPC art. 219); no penal e "
    "em boa parte dos prazos de direito material, em dias corridos."
)


USAGE = (
    "Use assim: /prazo <data> <dias> <úteis|corridos>\n"
    "  /prazo 06/10/2026 15 úteis\n"
    "  /prazo hoje 5 corridos\n"
    "  /prazo 06/10 15 úteis dobro  (prazo em dobro)\n"
    "A data é a da intimação ou publicação: ela não conta, a contagem começa "
    "no dia seguinte (CPC art. 224)."
)


def parse(arguments: list[str], today: date) -> DeadlineRequest | Incomplete:
    """One request, or what it still needs.

    Two dates, two day counts or both regimes in one request are refused
    rather than settled by whichever came last: "5 úteis corridos" has no
    reading that is safer than asking.
    """
    starts: list[date] = []
    day_counts: list[int] = []
    regimes: set[Regime] = set()
    doubled = False
    bad_date: str | None = None

    for raw in arguments:
        word = _plain(raw)
        if not word:
            continue
        if word in _WORKING:
            regimes.add(Regime.DIAS_UTEIS)
        elif word in _CALENDAR:
            regimes.add(Regime.DIAS_CORRIDOS)
        elif word == "dobro":
            doubled = True
        elif _DAYS.match(word):
            day_counts.append(int(word))
        elif (found := parse_date(word, today)) is not None:
            starts.append(found)
        elif _DATE.match(word):
            bad_date = raw

    if bad_date is not None:
        return Incomplete(
            f"Não entendi a data {bad_date}. Use dia/mês/ano, como 06/10/2026, "
            f"dentro de {YEARS_AROUND_TODAY} anos de hoje."
        )
    if len(regimes) > 1:
        return Incomplete("Você escreveu úteis e corridos. Qual dos dois? " + USAGE)
    if len(starts) > 1 or len(day_counts) > 1:
        return Incomplete("Encontrei mais de uma data ou mais de um número de dias. " + USAGE)
    start = starts[0] if starts else None
    days = day_counts[0] if day_counts else None
    regime = next(iter(regimes), None)
    if start is None or days is None:
        return Incomplete(USAGE)
    if not 1 <= days <= MAX_DAYS:
        return Incomplete(f"O prazo precisa ter entre 1 e {MAX_DAYS} dias.")
    if regime is None:
        return Incomplete(
            REGIME_QUESTION + "\n\n"
            f"Repita com a palavra: /prazo {start:%d/%m/%Y} {days} úteis"
            f"{' dobro' if doubled else ''} (ou corridos)"
        )
    return DeadlineRequest(start=start, days=days, regime=regime, doubled=doubled)


def counted_days(request: DeadlineRequest) -> int:
    return request.days * 2 if request.doubled else request.days


def describe(request: DeadlineRequest, prazo: Prazo, *, local_days_on_file: bool) -> str:
    """The answer: the due date first, then the calculator's own trace.

    `local_days_on_file` says whether the firm has registered any local
    holiday for the years this deadline spans. When it has not, the answer
    says so in plain words, because a deadline computed with national
    holidays only reads exactly like a complete one.
    """
    weekday = _WEEKDAYS[prazo.vencimento.weekday()]
    lines = [f"📅 Vencimento: {prazo.vencimento:%d/%m/%Y} ({weekday})", ""]
    if request.doubled:
        lines.append(
            f"Prazo em dobro: {request.days} dias contados como {prazo.dias} "
            "(CPC arts. 180, 183, 186 ou 229, conforme o caso)."
        )
    lines.append(prazo.descrever())
    if not local_days_on_file:
        years = sorted({prazo.termo_inicial.year, prazo.vencimento.year})
        span = " e ".join(str(year) for year in years)
        lines.append(
            f"\n⚠️ Nenhum feriado local do escritório cadastrado para {span}: "
            "só os nacionais entraram na conta. Veja com /feriados; um admin "
            "cadastra com /feriado <data> <nome>."
        )
    return "\n".join(lines)


_WEEKDAYS = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
             "sexta-feira", "sábado", "domingo")


__all__ = [
    "DeadlineRequest", "Incomplete", "MAX_DAYS", "REGIME_QUESTION", "USAGE", "counted_days", "describe",
    "parse", "parse_date",
]
