"""Reading /prazo, and what the answer says around the calculator's count.

The property that matters most is a refusal: a request that does not say
"úteis" or "corridos" is answered with a question, never with a default. The
calculator refuses to guess the regime, and the door in front of it must not
guess on its behalf.
"""
from datetime import date

from core.services import deadline_request
from core.services.deadline_request import DeadlineRequest, Incomplete
from core.services.deadlines import Regime, calcular_prazo

TODAY = date(2026, 10, 6)


def _parse(text: str):
    return deadline_request.parse(text.split(), TODAY)


def test_the_plain_form_is_read():
    assert _parse("06/10/2026 15 úteis") == DeadlineRequest(
        start=date(2026, 10, 6), days=15, regime=Regime.DIAS_UTEIS, doubled=False,
    )


def test_order_and_filler_words_do_not_matter():
    assert _parse("15 dias úteis a partir de 06/10/2026") == _parse("06/10/2026 15 úteis")


def test_corridos_and_accents_are_read():
    assert _parse("6/10 5 corridos").regime is Regime.DIAS_CORRIDOS  # type: ignore[union-attr]
    assert _parse("6/10 5 uteis").regime is Regime.DIAS_UTEIS  # type: ignore[union-attr]
    assert _parse("6/10 5 ÚTEIS").regime is Regime.DIAS_UTEIS  # type: ignore[union-attr]


def test_a_date_without_year_is_this_year_and_hoje_is_today():
    assert _parse("6/10 5 úteis").start == TODAY  # type: ignore[union-attr]
    assert _parse("hoje 5 úteis").start == TODAY  # type: ignore[union-attr]


def test_without_the_regime_it_asks_and_never_defaults():
    answer = _parse("06/10/2026 15")

    assert isinstance(answer, Incomplete)
    assert "úteis ou corridos" in answer.message
    assert "/prazo 06/10/2026 15 úteis" in answer.message


def test_without_date_or_days_it_shows_how():
    for text in ("15 úteis", "06/10/2026 úteis", ""):
        answer = _parse(text)
        assert isinstance(answer, Incomplete)
        assert answer.message == deadline_request.USAGE


def test_an_impossible_date_is_named_back():
    answer = _parse("31/02/2026 15 úteis")

    assert isinstance(answer, Incomplete)
    assert "31/02/2026" in answer.message


def test_days_out_of_range_are_refused():
    assert isinstance(_parse("06/10/2026 0 úteis"), Incomplete)
    assert isinstance(_parse(f"06/10/2026 {deadline_request.MAX_DAYS + 1} úteis"), Incomplete)


def test_dobro_doubles_the_days_counted_and_says_so():
    request = _parse("06/10/2026 15 úteis dobro")
    assert isinstance(request, DeadlineRequest)
    assert deadline_request.counted_days(request) == 30

    prazo = calcular_prazo(request.start, 30, regime=request.regime)
    text = deadline_request.describe(request, prazo, local_days_on_file=True)
    assert "Prazo em dobro: 15 dias contados como 30" in text


def test_the_answer_leads_with_the_due_date_and_its_weekday():
    """Intimated Monday 05/10/2026, five working days: 06, 07, 08, 09, then
    12/10 is Nossa Senhora Aparecida, so the fifth day is Tuesday 13/10."""
    request = _parse("05/10/2026 5 úteis")
    assert isinstance(request, DeadlineRequest)
    prazo = calcular_prazo(request.start, 5, regime=request.regime)

    text = deadline_request.describe(request, prazo, local_days_on_file=True)

    assert text.startswith("📅 Vencimento: 13/10/2026 (terça-feira)")
    assert "Conferência obrigatória" in text


def test_without_local_holidays_on_file_the_answer_says_only_national_counted():
    """A count with national holidays only reads exactly like a complete one."""
    request = _parse("06/10/2026 15 úteis")
    assert isinstance(request, DeadlineRequest)
    prazo = calcular_prazo(request.start, 15, regime=request.regime)

    warned = deadline_request.describe(request, prazo, local_days_on_file=False)
    quiet = deadline_request.describe(request, prazo, local_days_on_file=True)

    assert "Nenhum feriado local do escritório cadastrado para 2026" in warned
    assert "Nenhum feriado local" not in quiet


# ── found in review ──────────────────────────────────────────────────────────


def test_the_retry_it_suggests_keeps_dobro():
    """Following the suggested command must not halve the deadline."""
    answer = _parse("06/10/2026 15 dobro")

    assert isinstance(answer, Incomplete)
    assert "/prazo 06/10/2026 15 úteis dobro" in answer.message


def test_conflicting_words_are_refused_not_settled_by_the_last_one():
    for text in ("06/10/2026 5 úteis corridos", "06/10/2026 07/10/2026 5 úteis",
                 "06/10/2026 5 15 úteis"):
        assert isinstance(_parse(text), Incomplete), text


def test_a_digit_like_character_does_not_crash_the_command():
    assert isinstance(_parse("06/10/2026 5 úteis ❶"), DeadlineRequest)
    assert isinstance(_parse("06/10/2026 ❺ úteis"), Incomplete)


def test_a_far_year_is_refused_instead_of_overflowing_the_calculator():
    answer = _parse("31/12/9999 1 úteis")

    assert isinstance(answer, Incomplete)
    assert "31/12/9999" in answer.message
