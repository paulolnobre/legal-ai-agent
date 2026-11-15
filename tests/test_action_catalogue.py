"""The catalogue of what the agent may do from a sentence.

Each entry becomes the command a person would type, so the same handler, the
same authorization and the same reply serve both. What is tested here is that
mapping, and the one rule the router cannot be trusted with alone: the
deadline regime and "dobro" count only when the person said them.
"""
from datetime import date

import pytest
from pydantic import ValidationError

from core.handlers.legal_message import ROLE_OWNER, _commands_for
from core.services import authorization
from core.services.legal_assistant import actions
from core.services.legal_assistant.actions import CATALOGUE, COMMANDS_ONLY


class _Owner:
    role = ROLE_OWNER


_REAL = set(_commands_for(_Owner()))


def _deadline(**fields) -> actions.DeadlineArguments:
    assert CATALOGUE["deadline.calculate"].arguments is actions.DeadlineArguments
    return actions.DeadlineArguments(**fields)


def test_the_first_slice_is_exactly_these_reads():
    assert set(CATALOGUE) == {
        "deadline.calculate", "documents.list", "document.details",
        "lawsuit.status", "holidays.list",
    }
    assert not any(entry.needs_confirmation for entry in CATALOGUE.values())


def test_every_action_names_a_real_command_and_a_known_rule():
    for name, entry in CATALOGUE.items():
        assert entry.command in _REAL, name
        assert entry.authorization is None or entry.authorization in authorization.known_actions()


def test_every_commands_only_entry_is_a_real_command():
    assert set(COMMANDS_ONLY.values()) <= _REAL
    assert set(COMMANDS_ONLY.values()).isdisjoint(e.command for e in CATALOGUE.values())


def test_a_deadline_becomes_the_command_a_person_would_type():
    args = _deadline(start="05/10/2026", days=15, regime="úteis", regime_quote="úteis",
                     doubled=True, doubled_quote="em dobro")

    assert CATALOGUE["deadline.calculate"].to_command(args) == "/prazo 05/10/2026 15 úteis dobro"


def test_a_date_written_either_way_is_read():
    assert _deadline(start="05/10/2026").start == date(2026, 10, 5)
    assert _deadline(start="2026-10-05").start == date(2026, 10, 5)


@pytest.mark.parametrize("fields", [
    {"days": "quinze"}, {"days": 0}, {"start": "31/02/2026"}, {"regime": "talvez"},
    {"unexpected": 1},
])
def test_arguments_that_do_not_fit_are_refused(fields):
    with pytest.raises(ValidationError):
        _deadline(**fields)


def test_a_regime_the_person_never_wrote_is_dropped():
    args = _deadline(start="05/10/2026", days=15, regime="úteis", regime_quote="úteis")

    grounded = actions.ground("deadline.calculate", args, ["qual o prazo de 15 dias?"])

    assert grounded.regime is None  # type: ignore[attr-defined]


def test_a_regime_the_person_wrote_is_kept_whatever_the_accents():
    args = _deadline(regime="úteis", regime_quote="Uteis")

    grounded = actions.ground("deadline.calculate", args, ["15 dias ÚTEIS da intimação"])

    assert grounded.regime == "úteis"  # type: ignore[attr-defined]


def test_a_regime_with_no_quote_is_dropped():
    args = _deadline(regime="corridos")

    assert actions.ground("deadline.calculate", args, ["corridos"]).regime is None  # type: ignore[attr-defined]


def test_dobro_needs_its_own_quote():
    args = _deadline(regime="úteis", regime_quote="úteis", doubled=True, doubled_quote="em dobro")

    grounded = actions.ground("deadline.calculate", args, ["15 dias úteis"])

    assert grounded.regime == "úteis" and grounded.doubled is False  # type: ignore[attr-defined]


def test_the_missing_pieces_are_asked_for_one_at_a_time():
    entry = CATALOGUE["deadline.calculate"]

    assert "data" in entry.missing(_deadline())  # type: ignore[operator]
    assert "dias" in entry.missing(_deadline(start="05/10/2026"))  # type: ignore[operator]
    regime_question = entry.missing(_deadline(start="05/10/2026", days=15))
    assert regime_question is not None
    assert "úteis ou corridos" in regime_question and "Não escolho por você" in regime_question
    assert entry.missing(_deadline(start="05/10/2026", days=15, regime="úteis")) is None


def test_the_regime_question_is_the_calculators_own():
    """One wording for the same refusal, whichever door the person used."""
    from core.services import deadline_request

    question = CATALOGUE["deadline.calculate"].missing(_deadline(start="05/10/2026", days=15))
    assert question == deadline_request.REGIME_QUESTION


@pytest.mark.parametrize("name, fields, command", [
    ("documents.list", {"filters": "contratos da Acme"}, "/docs contratos da Acme"),
    ("documents.list", {}, "/docs"),
    ("document.details", {"document_id": 12}, "/detalhes 12"),
    ("lawsuit.status", {"number": "1000000-00.2024.8.26.0100"},
     "/processo 1000000-00.2024.8.26.0100"),
    ("holidays.list", {}, "/feriados"),
])
def test_each_read_becomes_its_command(name, fields, command):
    entry = CATALOGUE[name]

    assert entry.to_command(entry.arguments(**fields)) == command


def test_free_text_cannot_smuggle_a_second_command():
    """The command name is fixed by the entry; whatever the router put in a
    text field stays on the same line, as that command's argument."""
    entry = CATALOGUE["documents.list"]

    command = entry.to_command(entry.arguments(filters="acme\n/excluir_documento 3"))

    assert "\n" not in command and command.split()[0] == "/docs"


@pytest.mark.parametrize("name, fields", [
    ("document.details", {}), ("lawsuit.status", {}),
])
def test_a_read_without_its_identifier_asks_for_it(name, fields):
    entry = CATALOGUE[name]

    assert entry.missing(entry.arguments(**fields))


def test_the_phrasing_check_only_expects_what_exists():
    """A case naming an action or command that is not there would fail for
    the wrong reason the day it is run on the server."""
    from clients.demo_firm.eval_conversation import CASES

    for case in CASES:
        if case.kind == "action":
            assert case.name in CATALOGUE, case.phrase
            fields = CATALOGUE[case.name].arguments.model_fields
            assert set(case.expect) <= set(fields), case.phrase
        if case.kind == "command_only":
            assert case.name in COMMANDS_ONLY.values(), case.phrase


# ── found in the final review ────────────────────────────────────────────────


@pytest.mark.parametrize("fields, person", [
    ({"regime": "úteis", "regime_quote": "prazo"}, "qual o prazo de 15 dias?"),
    ({"regime": "úteis", "regime_quote": "corridos"}, "15 dias corridos"),
    ({"regime": "corridos", "regime_quote": "úteis"}, "15 dias úteis"),
])
def test_a_quote_counts_only_when_it_states_that_regime(fields, person):
    """Any substring of the message is not evidence: it has to say the regime,
    and the same regime the router claims."""
    grounded = actions.ground("deadline.calculate", _deadline(**fields), [person])

    assert grounded.regime is None  # type: ignore[attr-defined]


def test_a_quote_counts_for_dobro_only_when_it_asks_for_it():
    args = _deadline(doubled=True, doubled_quote="dias")

    assert actions.ground("deadline.calculate", args, ["15 dias"]).doubled is False  # type: ignore[attr-defined]


@pytest.mark.parametrize("quote, person, regime", [
    ("dias úteis", "15 dias úteis", "úteis"),
    ("DU", "15 DU", "úteis"),
    ("corridos", "10 dias corridos", "corridos"),
])
def test_a_quote_that_states_the_regime_still_counts(quote, person, regime):
    grounded = actions.ground("deadline.calculate",
                              _deadline(regime=regime, regime_quote=quote), [person])

    assert grounded.regime == regime  # type: ignore[attr-defined]


@pytest.mark.parametrize("name, fields", [
    ("document.details", {"document_id": True}),
    ("deadline.calculate", {"days": True}),
    ("deadline.calculate", {"days": "quinze"}),
    ("deadline.calculate", {"days": "15 dias"}),
    ("document.details", {"document_id": "١٢"}),
    ("deadline.calculate", {"doubled": "true"}),
    ("lawsuit.status", {"number": 1000000}),
    ("documents.list", {"filters": ["acme"]}),
])
def test_a_value_of_the_wrong_json_type_is_refused_not_coerced(name, fields):
    with pytest.raises(ValidationError):
        CATALOGUE[name].arguments(**fields)



@pytest.mark.parametrize("name, field, raw, value", [
    ("document.details", "document_id", "12", 12),
    ("deadline.calculate", "days", "15", 15),
    ("deadline.calculate", "days", " 15 ", 15),
])
def test_a_number_written_as_text_is_still_that_number(name, field, raw, value):
    """The real model sent {"document_id": "12"} (seen 2026-10-08). Digits in a
    string are unambiguous; true is not, and stays refused above."""
    args = CATALOGUE[name].arguments(**{field: raw})

    assert getattr(args, field) == value
