"""What set_webhook.py registers with Telegram: the configuration that decides
which update kinds are even attempted, before any handler code runs, and the
command menu a person sees when they type "/".

Telegram silently drops an update kind that is not in ``allowed_updates``: not
a 4xx, not a retry, nothing in this application's logs. A button that renders
fine and does nothing when pressed is exactly what that produces, and it is
invisible to every test that exercises the handler directly, because the
handler is never reached. This is the one place that class of bug is checked.
"""
import re

from core.services.telegram_registration import webhook_payload as _webhook_payload
from core.handlers.legal_message import ROLE_OWNER, _commands_for, command_menu


def test_button_presses_are_registered_for_delivery():
    payload = _webhook_payload("https://example.com/telegram/webhook", "secret")

    assert "callback_query" in payload["allowed_updates"], (
        "without this, Telegram accepts a button press and never forwards it — "
        "the button renders, the tap does nothing, and no log explains why"
    )


def test_text_and_edited_messages_stay_registered():
    payload = _webhook_payload("https://example.com/telegram/webhook", "secret")

    assert "message" in payload["allowed_updates"]
    assert "edited_message" in payload["allowed_updates"]


def test_the_secret_and_url_are_carried_through_unchanged():
    payload = _webhook_payload("https://example.com/telegram/webhook", "s3cr3t")

    assert payload["url"] == "https://example.com/telegram/webhook"
    assert payload["secret_token"] == "s3cr3t"


def test_pending_updates_are_not_dropped_on_reconfiguration():
    """A webhook update must not cost whatever Telegram already queued."""
    payload = _webhook_payload("https://example.com/telegram/webhook", "secret")

    assert payload["drop_pending_updates"] is False


# ── the menu Telegram offers when somebody types "/" ────────────────────────


class _Owner:
    """The one role that sees every command, so the menu can be checked whole."""

    role = ROLE_OWNER


def test_the_menu_only_offers_commands_the_bot_really_answers():
    """The menu is an invitation, so offering a command that does not exist
    would have the firm tap something and be told it is unknown."""
    offered = {f"/{entry['command']}" for entry in command_menu()}
    real = set(_commands_for(_Owner()))

    assert offered <= real, f"menu offers what nothing answers: {offered - real}"


def test_every_command_in_the_help_reaches_the_menu():
    """The other direction: a command documented for the firm but missing from
    the menu is one they can only find by reading the help, which is the thing
    the menu exists to save them from."""
    assert {f"/{entry['command']}" for entry in command_menu()} == set(
        _commands_for(_Owner())
    )


def test_the_menu_fits_what_telegram_accepts():
    """Telegram rejects the whole call if one entry is malformed, which would
    take the webhook registration down with it."""
    menu = command_menu()

    assert 0 < len(menu) <= 100
    for entry in menu:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", entry["command"]), entry
        assert 1 <= len(entry["description"]) <= 256, entry
        assert "\n" not in entry["description"], entry


def test_a_description_is_the_first_clause_not_the_whole_help_line():
    """/docs documents its filters over the following lines. The menu shows one
    line per command, so it takes the clause that says what the command is."""
    docs = next(e for e in command_menu() if e["command"] == "docs")

    assert docs["description"] == "lista o acervo"
