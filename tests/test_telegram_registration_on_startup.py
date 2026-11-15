"""The bot tells Telegram its webhook and its "/" menu each time it starts.

Republishing the menu used to be a step somebody ran by hand after a deploy
that added a command, and it was forgotten. Starting the bot now does it, so
the menu is always the one the running code answers. A refusal or an outage
must not stop the bot from starting, since the webhook already registered
keeps working; and the bot token, which is part of every Telegram API URL,
must never reach a log line.
"""
import ast
import asyncio
import json
import logging

import httpx
import pytest
from pathlib import Path

from core.config import settings
from core.handlers.legal_message import command_menu
from core.logger import quiet_token_bearing_loggers, setup_logging
from core.services import telegram_registration as registration

_TOKEN = "123456:SECRET-TOKEN-NEVER-LOGGED"


@pytest.fixture
def production(monkeypatch):
    # What the application's own logging setup does; without it httpx logs
    # each request URL, token included, at INFO.
    quiet_token_bearing_loggers()
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "legal_telegram_bot_token", _TOKEN)
    monkeypatch.setattr(settings, "telegram_webhook_secret", "hook-secret")
    monkeypatch.setattr(settings, "public_base_url", "https://legal.example.com/")
    monkeypatch.setattr(registration, "_api_base", lambda: f"https://api.telegram.org/bot{_TOKEN}")


_OURS = "https://legal.example.com/telegram/webhook"


def _telegram(answer, *, current: str = _OURS):
    """Telegram, answering getWebhookInfo with `current` and the rest with
    `answer`. `calls` leaves out getWebhookInfo unless a test asks."""
    calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getWebhookInfo":
            return httpx.Response(200, json={"ok": True, "result": {"url": current}})
        calls.append((method, json.loads(request.content)))
        return answer(method)

    return calls, httpx.MockTransport(handler)


def _run(transport, *, move: bool = False) -> int:
    async def go():
        async with httpx.AsyncClient(transport=transport) as client:
            return await registration.register(client, move=move)
    return asyncio.run(go())


def test_it_registers_the_webhook_then_the_whole_menu(production):
    calls, transport = _telegram(lambda _m: httpx.Response(200, json={"ok": True}))

    count = _run(transport)

    assert [method for method, _ in calls] == ["setWebhook", "setMyCommands"]
    webhook = calls[0][1]
    assert webhook["url"] == _OURS
    assert webhook["secret_token"] == "hook-secret"
    assert "callback_query" in webhook["allowed_updates"]
    assert webhook["drop_pending_updates"] is False
    assert calls[1][1] == {"commands": command_menu()}
    assert count == len(command_menu())


@pytest.mark.parametrize("answer", [
    lambda _m: httpx.Response(200, json={"ok": False, "description": "nope"}),
    lambda _m: httpx.Response(401, json={"ok": False}),
    lambda m: httpx.Response(200 if m == "setWebhook" else 400, json={"ok": m == "setWebhook"}),
])
def test_a_refusal_is_an_error(production, answer):
    _calls, transport = _telegram(answer)

    with pytest.raises(registration.RegistrationFailed):
        _run(transport)


def test_a_refused_webhook_does_not_go_on_to_the_menu(production):
    calls, transport = _telegram(lambda _m: httpx.Response(200, json={"ok": False}))

    with pytest.raises(registration.RegistrationFailed):
        _run(transport)

    assert [method for method, _ in calls] == ["setWebhook"]


def test_only_an_https_webhook_is_registered(production, monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "http://legal.example.com")
    calls, transport = _telegram(lambda _m: httpx.Response(200, json={"ok": True}))

    with pytest.raises(registration.RegistrationFailed):
        _run(transport)

    assert calls == []


def _at_startup(transport, waits: list | None = None) -> bool:
    async def no_wait(seconds):
        if waits is not None:
            waits.append(seconds)

    async def go():
        return await registration.register_at_startup(
            client_factory=lambda: httpx.AsyncClient(transport=transport), sleep=no_wait)
    return asyncio.run(go())


@pytest.mark.parametrize("answer", [
    lambda _m: httpx.Response(500, json={"ok": False}),
    lambda _m: (_ for _ in ()).throw(httpx.ConnectError(
        f"cannot reach https://api.telegram.org/bot{_TOKEN}/setWebhook")),
])
def test_at_startup_a_failure_is_logged_never_raised_and_never_names_the_token(
        production, answer, caplog):
    _calls, transport = _telegram(answer)

    with caplog.at_level(logging.DEBUG):
        assert _at_startup(transport) is False

    assert "not registered" in caplog.text
    assert _TOKEN not in caplog.text
    assert "SECRET" not in caplog.text


def test_at_startup_success_is_logged_with_the_count(production, caplog):
    _calls, transport = _telegram(lambda _m: httpx.Response(200, json={"ok": True}))

    with caplog.at_level(logging.INFO):
        assert _at_startup(transport) is True

    assert f"{len(command_menu())} commands" in caplog.text
    assert _TOKEN not in caplog.text


@pytest.mark.parametrize("change", [
    {"app_env": "development"},
    {"legal_telegram_bot_token": None},
    {"telegram_webhook_secret": None},
    {"public_base_url": None},
])
def test_only_a_configured_production_bot_registers_itself(production, monkeypatch, change):
    """A development run must never repoint production's webhook at itself."""
    assert registration.should_register_at_startup() is True
    for name, value in change.items():
        monkeypatch.setattr(settings, name, value)

    assert registration.should_register_at_startup() is False


def test_the_application_logging_never_lets_httpx_print_a_request_url():
    """httpx's INFO request line is the URL, and Telegram's has the token."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.NOTSET)

    setup_logging()

    # Pinned on the logger itself, not inherited from a root level that a
    # later change could lower.
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).level == logging.WARNING, name


def test_outside_production_nothing_is_sent(production, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "development")
    sent = []

    async def register_at_startup(**_kwargs):
        sent.append(True)
        return True

    monkeypatch.setattr(registration, "register_at_startup", register_at_startup)

    assert asyncio.run(registration.register_if_production()) is False
    assert sent == []


def test_the_application_starts_the_registration():
    """Read from the source, as tests/test_app_workers.py does: starting the
    real application needs a database."""
    source = Path("core/web/app.py").read_text(encoding="utf-8")

    assert "telegram_registration.register_if_production()" in source


def test_an_unexpected_error_is_logged_by_its_kind_only(production, caplog):
    """Not every failure is httpx's: whatever it is, startup goes on and its
    message, which could hold the URL, stays out of the log."""
    def broken_client():
        raise RuntimeError(f"https://api.telegram.org/bot{_TOKEN}/setWebhook")

    async def go():
        return await registration.register_at_startup(client_factory=broken_client)

    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(go()) is False

    assert "RuntimeError" in caplog.text
    assert _TOKEN not in caplog.text


# ── whose webhook it is ─────────────────────────────────────────────────────


@pytest.mark.parametrize("current", ["", _OURS])
def test_a_webhook_unset_or_already_ours_is_registered(production, current):
    calls, transport = _telegram(lambda _m: httpx.Response(200, json={"ok": True}),
                                 current=current)

    _run(transport)

    assert [method for method, _ in calls] == ["setWebhook", "setMyCommands"]


def test_starting_never_takes_a_webhook_another_instance_holds(production):
    """A copy of production's .env on a second machine, started: it must not
    take the firm's messages away from the real bot, nor change its menu."""
    calls, transport = _telegram(lambda _m: httpx.Response(200, json={"ok": True}),
                                 current="https://elsewhere.example.com/telegram/webhook")

    with pytest.raises(registration.RegistrationFailed, match="another address"):
        _run(transport)

    assert calls == []


def test_the_manual_script_moves_it_on_purpose(production):
    calls, transport = _telegram(lambda _m: httpx.Response(200, json={"ok": True}),
                                 current="https://elsewhere.example.com/telegram/webhook")

    _run(transport, move=True)

    assert calls[0] == ("setWebhook", calls[0][1]) and calls[0][1]["url"] == _OURS
    source = Path("clients/demo_firm/set_webhook.py").read_text(encoding="utf-8")
    assert "register(client, move=True)" in source


# ── trying again ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("failure", [
    lambda: httpx.Response(502, json={"ok": False}),
    lambda: httpx.Response(429, json={"ok": False}),
    lambda: (_ for _ in ()).throw(httpx.ConnectTimeout("slow")),
])
def test_a_passing_failure_is_tried_again_until_it_works(production, failure):
    """After a secret rotation, Telegram keeps sending the old secret until
    this lands: one try and a shrug would leave the bot deaf."""
    attempts = {"n": 0}

    def answer(_method):
        attempts["n"] += 1
        return failure() if attempts["n"] <= 2 else httpx.Response(200, json={"ok": True})

    waits: list = []
    _calls, transport = _telegram(answer)

    assert _at_startup(transport, waits) is True
    assert waits == list(registration.RETRY_DELAYS[:2])


def test_trying_again_stops(production):
    waits: list = []
    _calls, transport = _telegram(lambda _m: httpx.Response(503, json={"ok": False}))

    assert _at_startup(transport, waits) is False
    assert waits == list(registration.RETRY_DELAYS)


@pytest.mark.parametrize("current, answer", [
    ("https://elsewhere.example.com/telegram/webhook",
     lambda _m: httpx.Response(200, json={"ok": True})),
    (_OURS, lambda _m: httpx.Response(401, json={"ok": False})),
])
def test_what_waiting_will_not_fix_is_not_retried(production, current, answer):
    waits: list = []
    _calls, transport = _telegram(answer, current=current)

    assert _at_startup(transport, waits) is False
    assert waits == []


def test_the_application_runs_it_in_the_background():
    """A Telegram that hangs or retries for minutes must not hold readiness:
    the call is the argument of create_task, never awaited in the lifespan."""
    tree = ast.parse(Path("core/web/app.py").read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and ast.unparse(node.func).endswith("register_if_production")]
    tasks = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and ast.unparse(node.func) == "asyncio.create_task"]

    assert len(calls) == 1
    assert any(task.args and task.args[0] is calls[0] for task in tasks)
