"""Abuse-prevention primitives: rate limiting and input sanitization."""
import re
import time
from collections import deque


class RateLimiter:
    """In-memory sliding-window rate limiter (per process/event loop).

    check() returns (allowed, should_notify): should_notify is True only on
    the first blocked hit, so the bot warns once instead of echoing every
    flooded message (which would itself be an amplification vector).
    """

    _PRUNE_THRESHOLD = 10_000  # keys kept before pruning idle entries

    def __init__(self, max_events: int, window_seconds: float, clock=time.monotonic):
        self.max_events = max_events
        self.window = float(window_seconds)
        self._clock = clock
        self._events: dict = {}
        self._notified: set = set()

    def check(self, key) -> tuple[bool, bool]:
        now = self._clock()
        dq = self._events.setdefault(key, deque())
        while dq and now - dq[0] > self.window:
            dq.popleft()

        if len(self._events) > self._PRUNE_THRESHOLD:
            self._prune(now)

        if len(dq) < self.max_events:
            dq.append(now)
            self._notified.discard(key)
            return True, False

        first_block = key not in self._notified
        self._notified.add(key)
        return False, first_block

    def _prune(self, now: float):
        idle = [k for k, dq in self._events.items() if not dq or now - dq[-1] > self.window]
        for k in idle:
            self._events.pop(k, None)
            self._notified.discard(k)


_MD_SPECIALS = re.compile(r"([_*\[\]()`~])")


def escape_markdown(text) -> str:
    """Escape Telegram legacy-Markdown metacharacters in untrusted text."""
    return _MD_SPECIALS.sub(r"\\\1", str(text))


def parse_int_id(raw, max_digits: int = 9) -> int | None:
    """Parse a positive integer id, bounded so it cannot overflow Postgres INTEGER."""
    raw = str(raw).strip()
    if not raw.isdigit() or len(raw) > max_digits:
        return None
    value = int(raw)
    return value if value > 0 else None
