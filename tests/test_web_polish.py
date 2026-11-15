"""The pages' polish: fluid without a script, and empty lists that say why.

Smoothness comes from the browser, not from code shipped to it: controls ease
between states with CSS transitions, which stop for anyone who asked their
system for less motion. A page change does not fade: on the phone the fade
froze the screen a moment on every tab, which read as the app hanging. An empty list says what will appear and how, and an
empty filtered list offers the way back, so neither reads as broken.
"""
from core.web import files, pages


def test_page_changes_do_not_fade():
    assert "view-transition" not in pages.STYLE
    assert "script" not in pages.HEADERS["Content-Security-Policy"]


def test_motion_stops_for_people_who_asked_for_less():
    reduced = pages.STYLE[pages.STYLE.index("@media (prefers-reduced-motion:reduce)"):]

    assert "transition:none" in reduced


def test_controls_ease_between_states():
    assert "transition:background-color" in pages.STYLE


def test_corners_are_two_tokens():
    """Rounder than the firm's site on purpose: 8px for controls, 12px for containers."""
    assert "--radius:8px" in pages.STYLE
    assert "--radius-lg:12px" in pages.STYLE
    assert "--radius:2px" not in pages.STYLE
    assert "border-radius:2px" not in pages.STYLE


def test_an_empty_folder_says_what_will_appear_and_how():
    listing = files._listing([], filtered=False)

    assert "class=empty" in listing
    assert "Nenhum documento ainda" in listing
    assert "Telegram" in listing


def test_an_empty_filter_offers_the_way_back():
    listing = files._listing([], filtered=True)

    assert "class=empty" in listing
    assert "href='/web/arquivos/'" in listing
    assert "Limpar filtros" in listing


def test_a_field_in_focus_shows_one_edge_not_two():
    """The border and the outline 2px outside it read as a double ring."""
    rule = "input:focus-visible,select:focus-visible,textarea:focus-visible{outline:none;"
    assert rule in pages.STYLE
    after = pages.STYLE[pages.STYLE.index(rule):]
    assert "box-shadow:inset 0 0 0 1px var(--accent)" in after.split("}", 1)[0]
