"""Phone or computer: which rule a passkey session gets.

A passkey is shared across devices (iCloud), so the credential cannot say
where it is being used. The browser can, roughly, and the answer is read
once, when a session opens, and stored on it: a later request with another
User-Agent changes nothing.

The default is the phone, the stricter rule. Only a request that shows none
of the phone's signs, and whose page reported its touch points, is a
computer. None of this is authenticated; the spec's "What the lock does and
does not cover" says why that is accepted.
"""
from __future__ import annotations

from fastapi import Request

_PHONE_NAMES = ("iPhone", "iPod", "iPad", "Android")


def looks_like_phone(user_agent: str, mobile_hint: str | None) -> bool:
    """A phone by what every request carries: the mobile hint, or a
    User-Agent naming one. An iPhone asking for the desktop site sends a
    Mac's User-Agent and passes for a computer here; `is_phone` catches it."""
    if mobile_hint == "?1":
        return True
    return any(name in user_agent for name in _PHONE_NAMES)


def is_phone(user_agent: str, mobile_hint: str | None, touch_points: int | None) -> bool:
    """A phone, for a passkey sign-in, whose page reports
    `navigator.maxTouchPoints`. No Mac has a touch screen, so a Mac
    User-Agent with touch points is an iPhone or iPad in desktop mode. A
    missing report counts as a phone."""
    if looks_like_phone(user_agent, mobile_hint) or touch_points is None:
        return True
    return "Macintosh" in user_agent and touch_points > 1


def request_looks_like_phone(request: Request) -> bool:
    return looks_like_phone(request.headers.get("user-agent", ""),
                            request.headers.get("sec-ch-ua-mobile"))


__all__ = ["is_phone", "looks_like_phone", "request_looks_like_phone"]
