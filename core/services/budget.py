"""What the pilot may spend on the model, counted by the calendar the firm uses.

Every other limit in this project caps one thing: a document, a search, one
run's context. None of them caps the sum, so a bad week costs whatever a bad
week costs and nobody finds out until the invoice. This is the meter.

**Reserve first, settle after.** A run reserves its task class's ceiling before
the provider is called, not an estimate of this particular prompt: the project
has no tokenizer and deliberately does not add one, so the honest worst case is
the one the pilot policy already wrote down per task. When the provider reports
what it really used, the difference goes back. A run that dies between the two
leaves its reservation spent, which is the safe direction to fail.

**Atomic because there is more than one worker.** The counters are Redis
integers and every change is an INCRBY, so two workers reserving at once cannot
read the same number and both believe they fit.

**It fails open, and that is a decision.** If Redis cannot be reached the run
proceeds, loudly logged. The alternative, refusing to answer the firm because a
counter is unavailable, costs more than the tokens it saves, and the case
barely exists: the update queue is in that same Redis, so a Redis nobody can
reach is a Redis delivering no messages to spend tokens on.
"""
import logging
import math
from dataclasses import dataclass

import redis.asyncio as redis
from redis.exceptions import RedisError

from core.config import settings
from core.metrics import observe_budget
from core.schedule import local_now

logger = logging.getLogger(__name__)

_client: redis.Redis | None = None

# Kept a few days past the window they measure, so a counter can still be read
# after its window closes without being kept for ever.
_DAY_TTL = 60 * 60 * 24 * 3
_MONTH_TTL = 60 * 60 * 24 * 40


class BudgetExceeded(RuntimeError):
    """The pilot ceiling was reached, so no call was made.

    Carries the window that closed and when it reopens, because "try later" is
    not an answer somebody can plan around.
    """

    def __init__(self, window: str, reopens: str) -> None:
        super().__init__(f"the {window} pilot budget is spent, reopening {reopens}")
        self.window = window
        self.reopens = reopens


@dataclass(frozen=True)
class Reservation:
    """What a run took from the meter, so settle() knows what to give back."""

    day: str
    month: str
    input_tokens: int
    output_tokens: int
    # Transcription, in whole minutes counted as seconds (`reserve_audio`).
    audio_seconds: int = 0


def _redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    return _client


def _windows() -> tuple[str, str]:
    """Today and this month, by the firm's clock rather than the server's."""
    now = local_now()
    return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")


def _key(window: str, stamp: str, kind: str) -> str:
    return f"legal:budget:{window}:{stamp}:{kind}"


def _limits() -> dict[tuple[str, str], int]:
    return {
        ("day", "input"): settings.budget_day_input_tokens,
        ("day", "output"): settings.budget_day_output_tokens,
        ("month", "input"): settings.budget_month_input_tokens,
        ("month", "output"): settings.budget_month_output_tokens,
    }


def _reopens(window: str) -> str:
    if window == "day":
        return "amanhã"
    return "no primeiro dia do mês que vem"


async def reserve(worst_input: int, worst_output: int) -> Reservation | None:
    """Take a run's worst case from every counter, or refuse the run.

    Returns None when the meter is unreachable, which means the run proceeds
    unmetered rather than not at all. Raises BudgetExceeded when a ceiling is
    reached, having already given back whatever this attempt had taken, so a
    refused run leaves the meter exactly as it found it.
    """
    if not settings.budget_enabled:
        return None
    day, month = _windows()
    # window, stamp, kind, amount: the kind matters, or undoing a partial
    # reservation gives the input's amount back to the output's counter.
    taken: list[tuple[str, str, str, int]] = []
    try:
        for window, stamp, ttl in (("day", day, _DAY_TTL), ("month", month, _MONTH_TTL)):
            for kind, amount in (("input", worst_input), ("output", worst_output)):
                key = _key(window, stamp, kind)
                total = await _redis().incrby(key, amount)
                # Set on every write rather than only on creation: a key that
                # somehow lost its expiry would otherwise live for ever.
                await _redis().expire(key, ttl)
                taken.append((window, stamp, kind, amount))
                limit = _limits()[(window, kind)]
                observe_budget(window, kind, total, limit)
                if total > limit:
                    await _give_back(taken)
                    logger.warning(
                        "Pilot budget reached: window=%s kind=%s used=%d limit=%d",
                        window, kind, total, limit,
                    )
                    raise BudgetExceeded(window, _reopens(window))
    except (RedisError, OSError) as exc:
        # Deliberately open. See the module docstring: the queue lives in this
        # same Redis, so an unreachable one is not delivering work to spend on.
        logger.warning("Budget meter unavailable, letting the run through: %s", exc)
        return None
    return Reservation(day=day, month=month,
                       input_tokens=worst_input, output_tokens=worst_output)


async def settle(reservation: Reservation | None, input_tokens: int, output_tokens: int) -> None:
    """Return the difference between what was reserved and what was used.

    Never takes more than was reserved. A run that somehow used past its own
    ceiling has already been counted at the ceiling, and the extra is left for
    the per-run limits to answer for, not for this meter to discover late.
    """
    if reservation is None:
        return
    refunds = [
        ("day", reservation.day, "input", reservation.input_tokens - input_tokens),
        ("day", reservation.day, "output", reservation.output_tokens - output_tokens),
        ("month", reservation.month, "input", reservation.input_tokens - input_tokens),
        ("month", reservation.month, "output", reservation.output_tokens - output_tokens),
    ]
    try:
        for window, stamp, kind, amount in refunds:
            if amount <= 0:
                continue
            total = await _redis().incrby(_key(window, stamp, kind), -amount)
            observe_budget(window, kind, total, _limits()[(window, kind)])
    except (RedisError, OSError) as exc:
        logger.warning("Could not return a budget reservation: %s", exc)


def _audio_limits() -> dict[str, int]:
    return {"day": settings.budget_day_audio_seconds,
            "month": settings.budget_month_audio_seconds}


def billed_seconds(seconds: float) -> int:
    """Audio as the meter counts it: rounded up to the whole minute."""
    return max(1, math.ceil(max(0.0, float(seconds)) / 60)) * 60


async def reserve_audio(seconds: float) -> Reservation | None:
    """Take a transcription's length from the day's and the month's audio
    counters before Whisper is called, or refuse it.

    `seconds` is the duration of our own re-encoded output, never what the
    file claimed, rounded up to the minute. Like `reserve`: None when the
    meter is unreachable (the run proceeds), `BudgetExceeded` with the
    counters left as they were when a ceiling is reached.
    """
    if not settings.budget_enabled:
        return None
    amount = billed_seconds(seconds)
    day, month = _windows()
    taken: list[tuple[str, str, str, int]] = []
    try:
        for window, stamp, ttl in (("day", day, _DAY_TTL), ("month", month, _MONTH_TTL)):
            key = _key(window, stamp, "audio")
            total = await _redis().incrby(key, amount)
            await _redis().expire(key, ttl)
            taken.append((window, stamp, "audio", amount))
            limit = _audio_limits()[window]
            if total > limit:
                await _give_back(taken)
                logger.warning("Audio budget reached: window=%s used=%d limit=%d",
                               window, total, limit)
                raise BudgetExceeded(window, _reopens(window))
    except (RedisError, OSError) as exc:
        logger.warning("Budget meter unavailable, letting the transcription through: %s", exc)
        return None
    return Reservation(day=day, month=month, input_tokens=0, output_tokens=0,
                       audio_seconds=amount)


async def settle_audio(reservation: Reservation | None, seconds: float) -> None:
    """Give back what a transcription did not use. Called only when its
    outcome is known: a timeout, a lost connection or any other uncertain end
    keeps the whole reservation, since Whisper may have billed it."""
    if reservation is None:
        return
    refund = reservation.audio_seconds - (billed_seconds(seconds) if seconds > 0 else 0)
    if refund <= 0:
        return
    try:
        for window, stamp in (("day", reservation.day), ("month", reservation.month)):
            await _redis().incrby(_key(window, stamp, "audio"), -refund)
    except (RedisError, OSError) as exc:
        logger.warning("Could not return an audio reservation: %s", exc)


async def _give_back(taken: list[tuple[str, str, str, int]]) -> None:
    """Undo a partial reservation, so a refusal costs nothing.

    Each counter is given back exactly what it was given, which is why the
    kind is carried along: a refusal that credited the output counter with the
    input's amount would drift further from the truth on every refusal.
    """
    for window, stamp, kind, amount in taken:
        try:
            total = await _redis().incrby(_key(window, stamp, kind), -amount)
            if kind != "audio":
                observe_budget(window, kind, total, _limits()[(window, kind)])
        except (RedisError, OSError):
            logger.exception("Could not undo a budget reservation")


async def used() -> dict[str, dict[str, int]]:
    """What each counter reads now, for /status and for operators."""
    day, month = _windows()
    report: dict[str, dict[str, int]] = {}
    try:
        for window, stamp in (("day", day), ("month", month)):
            report[window] = {}
            for kind in ("input", "output"):
                raw = await _redis().get(_key(window, stamp, kind))
                report[window][kind] = int(raw or 0)
                report[window][f"{kind}_limit"] = _limits()[(window, kind)]
    except (RedisError, OSError) as exc:
        logger.warning("Could not read the budget counters: %s", exc)
        return {}
    return report


def cost_ceiling(input_tokens: int, output_tokens: int) -> float:
    """The most these tokens can have cost, in US$.

    An upper bound, never an estimate: every token is priced as if it went to
    the dearer model, because the counters do not record which one it went to.
    """
    return (
        input_tokens * settings.budget_price_input_per_million
        + output_tokens * settings.budget_price_output_per_million
    ) / 1_000_000


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
