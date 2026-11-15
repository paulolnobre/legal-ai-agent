"""The pages' theme switch, and the layout rule whose absence broke the filters.

The switch is a link and a cookie. Pages with the sidebar also run
navigation.js, which saves the next theme with `para` and repaints in place;
without it the link still works. What must hold is that the chosen theme
reaches the page, that the link cycles through "follow the device", day and
night, and that "go back where I was" can only go back to one of these pages,
never to another site.
"""
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.config import settings
from core.web import files, pages, staff_web


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "web_enabled", True)
    app = FastAPI()
    app.include_router(staff_web.router)
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


def _painted(client, cookie: str | None) -> str:
    """A page rendered without touching the database: the pages-off answer."""
    settings.web_enabled = False
    try:
        headers = {"cookie": f"{pages.THEME_COOKIE}={cookie}"} if cookie else {}
        return client.get("/web/", headers=headers).text
    finally:
        settings.web_enabled = True


def test_without_a_choice_the_page_follows_the_device(client):
    assert "<html lang=pt-BR><head>" in _painted(client, None)


@pytest.mark.parametrize("cookie, attribute", [
    ("claro", "data-theme=light"), ("escuro", "data-theme=dark"),
])
def test_a_chosen_theme_reaches_the_page(client, cookie, attribute):
    assert f"<html lang=pt-BR {attribute}>" in _painted(client, cookie)


def test_an_unknown_cookie_value_is_ignored(client):
    assert "<html lang=pt-BR><head>" in _painted(client, "'><script>")


@pytest.mark.parametrize("current, cookie_after", [
    (None, "claro"), ("claro", "escuro"), ("escuro", None),
])
def test_the_link_cycles_device_day_night(client, current, cookie_after):
    headers = {"cookie": f"{pages.THEME_COOKIE}={current}"} if current else {}

    response = client.get("/web/tema?volta=/web/arquivos/", headers=headers)

    assert response.status_code == 303
    assert response.headers["location"] == "/web/arquivos/"
    set_cookie = response.headers.get("set-cookie", "")
    if cookie_after is None:
        assert f"{pages.THEME_COOKIE}=" in set_cookie and "Max-Age=0" in set_cookie
    else:
        assert f"{pages.THEME_COOKIE}={cookie_after}" in set_cookie
        assert "Secure" in set_cookie and "HttpOnly" in set_cookie


@pytest.mark.parametrize("target", [
    "https://evil.example/", "//evil.example/web/", "/web//evil.example",
    "/\\evil.example", "/web/\r\nSet-Cookie: x=1", "javascript:alert(1)", "/admin",
    "/web/../admin", "/web/./../admin", "/web/%2e%2e/admin", "/web/%2E%2E/admin", "/web/..",
])
def test_going_back_never_leaves_these_pages(client, target):
    response = client.get("/web/tema", params={"volta": target})

    assert response.headers["location"] == "/web/"


def test_the_link_in_the_bar_points_back_to_the_current_page():
    token = pages._here.set("/web/arquivos/?q=contrato")
    try:
        link = staff_web.theme_link()
    finally:
        pages._here.reset(token)

    assert "/web/tema?volta=%2Fweb%2Farquivos%2F%3Fq%3Dcontrato" in link
    assert "Tema: automático" in link


def test_the_explicit_choice_overrides_the_device_in_the_stylesheet():
    """The attribute alone does nothing: these rules are what make it win."""
    assert ":root:not([data-theme=light]){color-scheme:dark" in pages.STYLE
    assert ":root[data-theme=dark]{color-scheme:dark" in pages.STYLE
    # Day is the bare default, so data-theme=light needs no rule of its own:
    # the media query above is the only thing that could paint night, and it
    # excludes data-theme=light.
    assert pages.STYLE.index("@media (prefers-color-scheme:dark)") < pages.STYLE.index(
        ":root[data-theme=dark]")


def test_every_box_includes_its_padding_and_border():
    """Without it a 100%-wide field is wider than its column by its padding
    and border, and the filter fields overlapped (seen 2026-10-06)."""
    assert "*,*::before,*::after{box-sizing:border-box}" in pages.STYLE


def test_the_filter_fields_do_not_offer_the_browsers_form_history():
    """Chrome offered unrelated saved entries under the title filter."""
    form = files._filters("", "", "", "", [])

    assert form.count("autocomplete=off") == 2


def test_a_tampered_cookie_does_not_break_the_switch(client):
    """An unknown value reads as "follow the device", so the next one is day,
    instead of a lookup error on the person's own cookie."""
    response = client.get("/web/tema", headers={"cookie": f"{pages.THEME_COOKIE}=roxo"})

    assert response.status_code == 303
    assert f"{pages.THEME_COOKIE}=claro" in response.headers.get("set-cookie", "")


@pytest.mark.parametrize("current, para, cookie_after", [
    (None, "claro", "claro"), ("claro", "escuro", "escuro"), ("escuro", "auto", None),
    # The script saves what it painted, whatever the cookie says by then.
    ("escuro", "claro", "claro"),
])
def test_the_script_saves_the_theme_without_a_reload(client, current, para, cookie_after):
    headers = {"cookie": f"{pages.THEME_COOKIE}={current}"} if current else {}

    response = client.get("/web/tema", params={"para": para}, headers=headers)

    assert response.status_code == 204
    assert "location" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    set_cookie = response.headers.get("set-cookie", "")
    if cookie_after is None:
        assert f"{pages.THEME_COOKIE}=" in set_cookie and "Max-Age=0" in set_cookie
    else:
        assert f"{pages.THEME_COOKIE}={cookie_after}" in set_cookie
        assert "Secure" in set_cookie and "HttpOnly" in set_cookie


@pytest.mark.parametrize("para", ["roxo", "", "'><script>"])
def test_an_unknown_theme_is_refused_and_nothing_saved(client, para):
    response = client.get("/web/tema", params={"para": para})

    assert response.status_code == 400
    assert "set-cookie" not in response.headers


def test_the_script_names_only_themes_the_server_takes():
    """navigation.js asks for these values; one the server refuses would
    send every click to the reloading link."""
    script = (Path(staff_web.__file__).parent / "static" / "navigation.js").read_text()

    for value in staff_web._THEME_TARGET:
        assert f'"{value}"' in script
    assert "/web/tema?para=" in script
    assert pages.DAY_GROUND in script and pages.NIGHT_GROUND in script
