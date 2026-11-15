"""The chat's actions: each builds exactly one command from fields it checked.

The command is what the worker runs, so a field that could carry a second
command, or a value the handler would read differently, is refused here.
"""
from datetime import date

import pytest

from core.handlers import legal_message
from core.integrations import datajud
from core.models import StaffMember
from core.services import authorization, deadline_request
from core.services.deadlines import Regime
from core.web import actions
from core.web.actions import Built, build

TODAY = date(2026, 11, 9)
CNJ = "1000000-37.2024.8.26.0100"
DIGITS = "10000003720248260100"
RUI = StaffMember(id=4, chat_id="400", name="Rui", role="estagiario")


@pytest.mark.parametrize(("key", "form", "command", "label"), [
    ("prazo", {"data": "2026-10-06", "dias": "15", "regime": "uteis"},
     "/prazo 06/10/2026 15 úteis", "Calcular prazo · 06/10/2026, 15 dias úteis"),
    ("prazo", {"data": "2026-10-06", "dias": "15", "regime": "corridos", "dobro": "1"},
     "/prazo 06/10/2026 15 corridos dobro",
     "Calcular prazo · 06/10/2026, 15 dias corridos (em dobro)"),
    ("feriados", {}, "/feriados", "Feriados"),
    ("processo", {"cnj": DIGITS}, f"/processo {CNJ}", f"Consultar processo · {CNJ}"),
    ("autos", {"cnj": CNJ}, f"/autos {CNJ}", f"Autos no PJe · {CNJ}"),
    ("intimacoes", {}, "/intimacoes", "Intimações pendentes"),
    ("oab", {"oab": "0123456", "uf": "SP"}, "/oab 123456 SP", "Acompanhar OAB · 123456/SP"),
    ("acompanhar", {"cnj": CNJ}, f"/acompanhar {CNJ}", f"Acompanhar processo · {CNJ}"),
    ("desacompanhar", {"alvo": f"processo:{DIGITS}"}, f"/desacompanhar {CNJ}",
     f"Parar de acompanhar · processo {CNJ}"),
    ("desacompanhar", {"alvo": "oab:123456/SP"}, "/desacompanhar 123456 SP",
     "Parar de acompanhar · OAB 123456/SP"),
])
def test_each_action_builds_its_command_and_label(key, form, command, label):
    assert build(key, form, TODAY) == Built(command, label)


@pytest.mark.parametrize("typed", ["2026-10-06", "06/10/2026", "06/10/26"])
def test_the_date_reads_the_same_typed_or_picked(typed):
    built = build("prazo", {"data": typed, "dias": "15", "regime": "uteis"}, TODAY)
    assert built is not None and built.command == "/prazo 06/10/2026 15 úteis"


_PRAZO = {"data": "2026-10-06", "dias": "15", "regime": "uteis"}


@pytest.mark.parametrize(("key", "form"), [
    ("prazo", {**_PRAZO, "data": "31/02/2026"}),
    ("prazo", {**_PRAZO, "data": "2037-01-01"}),
    ("prazo", {**_PRAZO, "data": ""}),
    ("prazo", {**_PRAZO, "dias": "0"}),
    ("prazo", {**_PRAZO, "dias": "1000"}),
    ("prazo", {**_PRAZO, "dias": "15 úteis"}),
    ("prazo", {**_PRAZO, "dias": "１５"}),
    ("prazo", {**_PRAZO, "regime": "ambos"}),
    ("prazo", {**_PRAZO, "regime": ""}),
    ("prazo", {**_PRAZO, "dobro": "sim"}),
    ("processo", {"cnj": DIGITS[:-1]}),
    ("processo", {"cnj": DIGITS + "1"}),
    ("processo", {"cnj": "1000000-37.2024.0.26.0100"}),
    ("autos", {}),
    ("oab", {"oab": "12345678", "uf": "SP"}),
    ("oab", {"oab": "", "uf": "SP"}),
    ("oab", {"oab": "123456", "uf": "XX"}),
    ("oab", {"oab": "123456", "uf": "sp"}),
    ("desacompanhar", {"alvo": "oab:123/XX"}),
    ("desacompanhar", {"alvo": "nada"}),
    ("desacompanhar", {"alvo": "processo:123"}),
    ("promover", {"id": "3"}),
])
def test_invalid_fields_are_refused(key, form):
    assert build(key, form, TODAY) is None


def test_the_window_edge_is_the_handlers():
    assert build("prazo", {**_PRAZO, "data": "2036-11-09"}, TODAY) is not None
    assert build("prazo", {**_PRAZO, "data": "2037-11-09"}, TODAY) is None


@pytest.mark.parametrize(("key", "form"), [
    ("prazo", {**_PRAZO, "dias": "15\n/promover 3"}),
    ("prazo", {**_PRAZO, "regime": "uteis /promover 3"}),
    ("prazo", {**_PRAZO, "data": "06/10/2026 /esquecer 1"}),
    ("processo", {"cnj": CNJ + " /promover 3"}),
    ("oab", {"oab": "123 /promover", "uf": "SP"}),
    ("oab", {"oab": "123456", "uf": "SP\n/esquecer 1"}),
    ("desacompanhar", {"alvo": "oab:123456/SP\n/esquecer 1"}),
])
def test_no_field_can_carry_a_second_command(key, form):
    assert build(key, form, TODAY) is None


def test_every_built_command_reads_back_as_its_fields():
    built = build("prazo", {"data": "2026-10-06", "dias": "15", "regime": "uteis",
                            "dobro": "1"}, TODAY)
    assert built is not None
    read = deadline_request.parse(built.command.split()[1:], TODAY)
    assert isinstance(read, deadline_request.DeadlineRequest)
    assert (read.start, read.days, read.regime, read.doubled) == (
        date(2026, 10, 6), 15, Regime.DIAS_UTEIS, True)
    for key in ("processo", "autos", "acompanhar"):
        built = build(key, {"cnj": CNJ}, TODAY)
        assert built is not None
        assert datajud.extract_process_numbers(built.command, limit=1) == [DIGITS]
        assert datajud.has_valid_check_digits(DIGITS)
    built = build("oab", {"oab": "123456", "uf": "SP"}, TODAY)
    assert built is not None
    assert legal_message._parse_oab(built.command.split()[1:]) == ("123456", "SP")


def test_allowed_follows_decide(monkeypatch):
    real = authorization.decide

    def deny_pje(staff, action):
        if action == "pje.read":
            return authorization.Decision(allowed=False, action=action, reason="não")
        return real(staff, action)

    monkeypatch.setattr(authorization, "decide", deny_pje)
    keys = [action.key for action in actions.allowed(RUI)]
    assert keys == ["prazo", "feriados", "processo", "oab", "acompanhar", "desacompanhar"]


def test_every_action_names_a_real_permission():
    for action in actions.ACTIONS.values():
        assert authorization.decide(RUI, action.permission).allowed, action.key


@pytest.mark.parametrize(("key", "form"), [
    ("oab", {"oab": "000\n", "uf": "SP"}),
    ("oab", {"oab": "123456\n", "uf": "SP"}),
    ("prazo", {**_PRAZO, "dias": "15\n"}),
    ("prazo", {**_PRAZO, "data": "2026-10-06\n"}),
    ("prazo", {**_PRAZO, "data": "06/10/2026\n"}),
    ("processo", {"cnj": CNJ + "\n"}),
    ("desacompanhar", {"alvo": "oab:123456/SP\n"}),
    ("desacompanhar", {"alvo": f"processo:{DIGITS}\n"}),
])
def test_a_trailing_newline_is_refused(key, form):
    assert build(key, form, TODAY) is None


def test_following_needs_check_digits_the_handler_accepts():
    # /acompanhar refuses a number whose check digits do not match; the form
    # does not offer what the handler then turns down.
    wrong = "1000000-00.2024.8.26.0100"
    assert not datajud.has_valid_check_digits(wrong)
    assert build("acompanhar", {"cnj": wrong}, TODAY) is None
    assert build("acompanhar", {"cnj": CNJ}, TODAY) is not None


def test_each_action_decides_on_its_own_permission():
    assert {key: action.permission for key, action in actions.ACTIONS.items()} == {
        "prazo": "deadline.calculate",
        "feriados": "deadline.calculate",
        "processo": "research.external",
        "autos": "pje.read",
        "intimacoes": "pje.read",
        "oab": "publications.watch",
        "acompanhar": "publications.watch",
        "desacompanhar": "publications.watch",
    }
