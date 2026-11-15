"""The actions offered in the assistant's composer, as commands built from fields.

Each action is a command the chat already runs. The form only spares the
person its name and argument order: the fields are checked here, the command
is put together from the checked parts alone, and it then goes the way a typed
message goes, where the worker decides again whether this person may run it.
"""
import html
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Literal

from core.integrations import datajud
from core.repositories.publications import OAB_UFS
from core.schedule import local_now
from core.services import authorization, deadline_request

ActionKey = Literal["prazo", "feriados", "processo", "autos", "intimacoes", "oab",
                    "acompanhar", "desacompanhar"]
Group = Literal["prazos", "acompanhamentos"]


@dataclass(frozen=True)
class Action:
    key: ActionKey
    group: Group
    label: str
    permission: authorization.Action


@dataclass(frozen=True)
class Built:
    command: str
    label: str


# In the order the list shows them.
ACTIONS: dict[str, Action] = {action.key: action for action in (
    Action("prazo", "prazos", "Calcular prazo", "deadline.calculate"),
    Action("feriados", "prazos", "Feriados", "deadline.calculate"),
    # /processo checks no permission of its own (public data, every rank);
    # this is the name the list decides on, and it allows the same ranks.
    Action("processo", "prazos", "Consultar processo", "research.external"),
    Action("autos", "prazos", "Autos no PJe", "pje.read"),
    Action("intimacoes", "prazos", "Intimações pendentes", "pje.read"),
    Action("oab", "acompanhamentos", "Acompanhar minha OAB", "publications.watch"),
    Action("acompanhar", "acompanhamentos", "Acompanhar processo", "publications.watch"),
    Action("desacompanhar", "acompanhamentos", "Meus acompanhamentos", "publications.watch"),
)}

_ISO_DATE = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})$")
_TYPED_DATE = re.compile(r"^[0-9]{1,2}/[0-9]{1,2}/(?:[0-9]{2}|[0-9]{4})$")
_DAYS = re.compile(r"^[0-9]{1,3}$")
_CNJ = re.compile(r"^[0-9.\-]{20,25}$")
_OAB = re.compile(r"^[0-9]{1,7}$")
_TARGET_OAB = re.compile(r"^oab:([0-9]{1,7})/([A-Z]{2})$")
_TARGET_PROCESS = re.compile(r"^processo:([0-9]{20})$")
_REGIMES = {"uteis": "úteis", "corridos": "corridos"}


def _date(raw: str, today: date) -> str | None:
    """dd/mm/aaaa, inside the window /prazo itself accepts."""
    iso = _ISO_DATE.fullmatch(raw)
    word = f"{iso[3]}/{iso[2]}/{iso[1]}" if iso else raw
    if not _TYPED_DATE.fullmatch(word):
        return None
    found = deadline_request.parse_date(word, today)
    return f"{found:%d/%m/%Y}" if found else None


def _cnj(raw: str) -> str | None:
    """The number as cited, only when the handler would read it back the same."""
    if not _CNJ.fullmatch(raw):
        return None
    try:
        digits = datajud.normalize_process_number(raw)
    except ValueError:
        return None
    formatted = datajud.format_process_number(digits)
    if datajud.extract_process_numbers(formatted, limit=1) != [digits]:
        return None
    return formatted


def _oab(number: str, uf: str) -> tuple[str, str] | None:
    if not _OAB.fullmatch(number) or uf not in OAB_UFS:
        return None
    return number.lstrip("0") or "0", uf


def _prazo(form: Mapping[str, str], today: date) -> Built | None:
    start = _date(form.get("data", ""), today)
    days = form.get("dias", "")
    regime = _REGIMES.get(form.get("regime", ""))
    doubled = form.get("dobro")
    if start is None or not _DAYS.fullmatch(days) or regime is None:
        return None
    if doubled not in (None, "1") or not 1 <= int(days) <= 999:
        return None
    count = int(days)
    return Built(f"/prazo {start} {count} {regime}{' dobro' if doubled else ''}",
                 f"Calcular prazo · {start}, {count} dias {regime}"
                 f"{' (em dobro)' if doubled else ''}")


def _plain(command: str, label: str) -> Callable[[Mapping[str, str], date], Built]:
    return lambda _form, _today: Built(command, label)


def _with_cnj(command: str, label: str, *, checked: bool = False,
              ) -> Callable[[Mapping[str, str], date], Built | None]:
    """`checked`: the handler also refuses a number whose check digits do not
    match (/acompanhar keeps the number, so a typo would follow someone else's
    case), and the form does not offer what it would then turn down."""
    def built(form: Mapping[str, str], _today: date) -> Built | None:
        number = _cnj(form.get("cnj", ""))
        if number is None or (checked and not datajud.has_valid_check_digits(number)):
            return None
        return Built(f"{command} {number}", f"{label} · {number}")
    return built


def _watch_oab(form: Mapping[str, str], _today: date) -> Built | None:
    found = _oab(form.get("oab", ""), form.get("uf", ""))
    if found is None:
        return None
    number, uf = found
    return Built(f"/oab {number} {uf}", f"Acompanhar OAB · {number}/{uf}")


def _unwatch(form: Mapping[str, str], _today: date) -> Built | None:
    target = form.get("alvo", "")
    if (match := _TARGET_PROCESS.fullmatch(target)) and (number := _cnj(match[1])):
        return Built(f"/desacompanhar {number}", f"Parar de acompanhar · processo {number}")
    if (match := _TARGET_OAB.fullmatch(target)) and (found := _oab(match[1], match[2])):
        number, uf = found
        return Built(f"/desacompanhar {number} {uf}",
                     f"Parar de acompanhar · OAB {number}/{uf}")
    return None


_BUILDERS: dict[str, Callable[[Mapping[str, str], date], Built | None]] = {
    "prazo": _prazo,
    "feriados": _plain("/feriados", "Feriados"),
    "processo": _with_cnj("/processo", "Consultar processo"),
    "autos": _with_cnj("/autos", "Autos no PJe"),
    "intimacoes": _plain("/intimacoes", "Intimações pendentes"),
    "oab": _watch_oab,
    "acompanhar": _with_cnj("/acompanhar", "Acompanhar processo", checked=True),
    "desacompanhar": _unwatch,
}


def build(key: str, form: Mapping[str, str], today: date) -> Built | None:
    """The command and its label, or None for an unknown action or a bad field."""
    builder = _BUILDERS.get(key)
    return builder(form, today) if builder else None


def allowed(staff) -> list[Action]:
    """The actions this person may run, in the list's order."""
    return [action for action in ACTIONS.values()
            if authorization.decide(staff, action.permission).allowed]


# ── The list, as the page shows it ─────────────────────────────────────────

ADDRESS = "/web/assistente/acao"
_GROUPS: dict[Group, str] = {"prazos": "Prazos e processos",
                             "acompanhamentos": "Acompanhamentos"}
_SUBMIT = {"prazo": "Calcular", "processo": "Consultar", "autos": "Abrir",
           "oab": "Acompanhar", "acompanhar": "Acompanhar"}
_CNJ_INPUT = ("<label>Número CNJ<input name=cnj required inputmode=numeric "
              "autocomplete=off maxlength=25 pattern='[0-9.\\-]{20,25}' "
              "placeholder='0000000-00.0000.0.00.0000'></label>")


def _e(value: str) -> str:
    return html.escape(value, quote=True)


def _form(csrf: str, key: str, inner: str, button: str, *, busy: bool,
          css: str = "quiet") -> str:
    off = " disabled" if busy else ""
    return (f"<form method=post action='{ADDRESS}'>"
            f"<input type=hidden name=csrf value='{_e(csrf)}'>"
            f"<input type=hidden name=acao value='{_e(key)}'>{inner}"
            f"<button type=submit class={css}{off}>{_e(button)}</button></form>")


def _fields(key: str, today: date) -> str:
    if key == "prazo":
        low = date(today.year - deadline_request.YEARS_AROUND_TODAY, 1, 1)
        high = date(today.year + deadline_request.YEARS_AROUND_TODAY, 12, 31)
        return (f"<label>Data inicial<input type=date name=data required "
                f"min={low.isoformat()} max={high.isoformat()}></label>"
                "<label>Dias<input type=number name=dias required min=1 max=999 "
                "inputmode=numeric></label>"
                "<fieldset><legend>Contagem</legend>"
                "<label><input type=radio name=regime value=uteis required>úteis</label>"
                "<label><input type=radio name=regime value=corridos>corridos</label>"
                "</fieldset>"
                "<label><input type=checkbox name=dobro value=1>em dobro</label>")
    if key == "oab":
        options = "".join(f"<option>{uf}</option>" for uf in sorted(OAB_UFS))
        return ("<label>Número da OAB<input name=oab required inputmode=numeric "
                "autocomplete=off maxlength=7 pattern='[0-9]{1,7}'></label>"
                "<label>UF<select name=uf required><option value=''>Escolha</option>"
                f"{options}</select></label>")
    return _CNJ_INPUT


def action_form(key: ActionKey, csrf: str, *, today: date | None = None,
                busy: bool = False) -> str:
    """One action's form, alone, for a page other than the assistant's: it
    posts to the assistant, where the answer then arrives."""
    today = today or local_now().date()
    return _form(csrf, key, _fields(key, today), _SUBMIT[key], busy=busy, css="primary")


def follows(csrf: str, oabs: list[str], processes: list[str], *, busy: bool = False) -> str:
    """This person's follows, each with its own button to stop it."""
    items = [(f"oab:{oab}", f"OAB {oab}") for oab in oabs]
    items += [(f"processo:{number}", f"Processo {datajud.format_process_number(number)}")
              for number in processes]
    if not items:
        return "<div data-follows><p class=hint>Você ainda não acompanha nada.</p></div>"
    rows = "".join(
        f"<li><span>{_e(label)}</span>"
        + _form(csrf, "desacompanhar",
                f"<input type=hidden name=alvo value='{_e(target)}'>", "Parar", busy=busy)
        + "</li>"
        for target, label in items)
    return f"<div data-follows><ul class=follows>{rows}</ul></div>"


def _entry(action: Action, csrf: str, *, busy: bool, today: date,
           oabs: list[str], processes: list[str]) -> str:
    if action.key in ("feriados", "intimacoes"):
        return f"<div class=action>{_form(csrf, action.key, '', action.label, busy=busy)}</div>"
    if action.key == "desacompanhar":
        inner = follows(csrf, oabs, processes, busy=busy)
    else:
        inner = _form(csrf, action.key, _fields(action.key, today), _SUBMIT[action.key],
                      busy=busy, css="primary")
    return (f"<details class=action data-action={action.key}>"
            f"<summary>{_e(action.label)}</summary>{inner}</details>")


# Three lines: the same mark on the pill and on the composer's button.
ICON = ("<svg class=nav-icon viewBox='0 0 24 24' fill=none stroke=currentColor "
        "stroke-width=1.8 aria-hidden=true><path d='M4 6h16M4 12h16M4 18h10'/></svg>")


def menu(staff, csrf: str, *, busy: bool, oabs: list[str], processes: list[str],
         today: date | None = None) -> str:
    """The list of actions this person may run, outside the composer's form:
    a form inside that form would be dropped by the browser."""
    today = today or local_now().date()
    shown = allowed(staff)
    if not shown:
        return ""
    groups = "".join(
        f"<section><h2>{_GROUPS[group]}</h2>"
        + "".join(_entry(action, csrf, busy=busy, today=today, oabs=oabs,
                         processes=processes)
                  for action in shown if action.group == group)
        + "</section>"
        for group in _GROUPS if any(action.group == group for action in shown))
    return (f"<details class=actions data-actions id=acoes><summary class=tool>{ICON}"
            f"<span>Ações</span></summary><div class=action-list>{groups}</div></details>")
