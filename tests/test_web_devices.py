"""Phone or computer, read from what the browser says about itself.

The rule a passkey session gets follows the device it opens on, and the
default is the phone: only a request that is none of the phone's signs, and
whose page reported its touch points, is a computer.
"""
import pytest
from starlette.requests import Request

from core.web.devices import is_phone, looks_like_phone, request_looks_like_phone

_IPHONE_SAFARI = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
                  "Mobile/15E148 Safari/604.1")
_IPHONE_APP = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
               "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148")
_MAC_SAFARI = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
               "(KHTML, like Gecko) Version/17.5 Safari/605.1.15")
_MAC_CHROME = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_ANDROID = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36")
_WINDOWS = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


@pytest.mark.parametrize(("user_agent", "hint", "touch", "phone"), [
    pytest.param(_IPHONE_SAFARI, None, 5, True, id="iphone-safari"),
    pytest.param(_IPHONE_APP, None, 5, True, id="iphone-app"),
    # An iPhone or iPad asking for the desktop site sends a Mac's
    # User-Agent; no Mac has a touch screen.
    pytest.param(_MAC_SAFARI, None, 5, True, id="iphone-desktop-mode"),
    pytest.param(_MAC_SAFARI, None, 5, True, id="ipad-desktop-mode"),
    pytest.param(_ANDROID, "?1", 5, True, id="android-with-hint"),
    pytest.param(_ANDROID, None, 5, True, id="android-without-hint"),
    pytest.param("", "?1", 0, True, id="the-hint-alone"),
    pytest.param(_MAC_SAFARI, None, 0, False, id="mac-safari"),
    pytest.param(_MAC_CHROME, "?0", 0, False, id="mac-chrome"),
    # A touch-screen Windows laptop is a computer: its User-Agent is not a Mac's.
    pytest.param(_WINDOWS, "?0", 10, False, id="windows-touch"),
    # A missing or unreadable report counts as a phone.
    pytest.param(_MAC_SAFARI, None, None, True, id="mac-without-a-report"),
    pytest.param(_WINDOWS, "?0", None, True, id="windows-without-a-report"),
    pytest.param("", None, 0, False, id="empty-user-agent"),
])
def test_device_table(user_agent, hint, touch, phone):
    assert is_phone(user_agent, hint, touch) is phone


def test_the_user_agent_alone_cannot_see_an_iphone_in_desktop_mode():
    """Without the page's touch report a Mac User-Agent reads as a computer:
    which is why the app entry, which has no report, reads only this."""
    assert looks_like_phone(_IPHONE_SAFARI, None) is True
    assert looks_like_phone(_MAC_SAFARI, None) is False
    assert looks_like_phone(_WINDOWS, "?1") is True
    assert looks_like_phone(_WINDOWS, "?0") is False


def _request(headers: dict[str, str]) -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/web/", "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    })


def test_a_request_is_read_from_its_user_agent_and_its_mobile_hint():
    assert request_looks_like_phone(_request({"User-Agent": _IPHONE_APP})) is True
    assert request_looks_like_phone(
        _request({"User-Agent": _WINDOWS, "Sec-CH-UA-Mobile": "?1"})) is True
    assert request_looks_like_phone(
        _request({"User-Agent": _WINDOWS, "Sec-CH-UA-Mobile": "?0"})) is False
    assert request_looks_like_phone(_request({})) is False
