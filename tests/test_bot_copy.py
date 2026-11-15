"""What the bot says, as the firm reads it: found while recording the tutorial.

Three texts read wrong on screen (2026-10-09): an intern was offered summaries
of the corpus, a feature removed months before; an authorization and the
welcome assumed every new person is a man; and a lawsuit's dates came out as
the DataJud's raw timestamps.
"""
from datetime import datetime

import pytest

from core.integrations import datajud
from core.services import authorization
from core.repositories.staff import ROLE_LAWYER


def test_the_lawyer_only_refusal_offers_what_an_intern_really_has():
    refusal = authorization._REFUSAL_BY_RANK[ROLE_LAWYER]

    assert "resumo" not in refusal.casefold()
    assert "consultar o acervo" in refusal and "prazos" in refusal
    assert "estagiário(a)" in refusal


def test_no_refusal_assumes_a_gender():
    for refusal in authorization._REFUSAL_BY_RANK.values():
        assert "autorizado" not in refusal, refusal


@pytest.mark.parametrize("phrase", [
    "foi autorizado como estagiário", "Bem-vindo ao", "for autorizado", "estar autorizado",
])
def test_nothing_said_to_a_new_person_assumes_a_gender(phrase):
    for path in ("core/handlers/legal_message.py", "core/services/authorization.py"):
        assert phrase not in open(path, encoding="utf-8").read(), (path, phrase)


def _lookup(record: dict) -> datajud.DataJudLookup:
    return datajud.DataJudLookup(
        process_number="08012341620258140301", court_alias="tjpa", endpoint="x",
        records=(record,), consulted_at=datetime(2026, 10, 5, 9, 30))


@pytest.mark.parametrize("raw, shown", [
    ("2025-03-18T10:12:00.000Z", "18/03/2025"),
    ("20250318101200", "18/03/2025"),
    ("2025-03-18", "18/03/2025"),
])
def test_a_lawsuits_dates_read_as_the_firm_writes_them(raw, shown):
    text = datajud.format_for_staff(_lookup({
        "dataAjuizamento": raw, "dataHoraUltimaAtualizacao": raw,
        "movimentos": [{"nome": "Conclusão", "dataHora": raw}],
    }))

    assert f"Ajuizamento: {shown}" in text
    assert f"Última atualização: {shown}" in text
    assert f"{shown}: Conclusão" in text
    assert "T10:12" not in text and "2025-03-18" not in text


def test_a_date_it_cannot_read_is_shown_as_it_came():
    text = datajud.format_for_staff(_lookup({"dataAjuizamento": "em algum momento"}))

    assert "Ajuizamento: em algum momento" in text


@pytest.mark.parametrize("raw", [
    1742292720000, "1742292720000", "20250318.5", "2025-13-40", "20251340000000", None,
])
def test_what_is_not_a_complete_date_is_never_turned_into_one(raw):
    assert datajud._date(raw) == ("-" if raw is None else str(raw))


def test_the_help_says_a_team_is_named_not_typed_as_the_word_equipe():
    """"/compartilhar 21 equipe" answers "Não conheço a equipe “equipe”":
    the command takes the team's name (seen recording the tutorial)."""
    from core.handlers.legal_message import _HELP

    line = next(line for line in _HELP.splitlines() if line.startswith("/compartilhar"))
    assert "<nome da equipe|escritorio|privado>" in line
