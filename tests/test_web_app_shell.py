"""What makes the panel installable on a phone: the manifest, its icons, and
the policy that lets the browser read them.

The manifest is fetched by the browser without the session cookie, so it is
public and carries nothing about anyone. Under the default policy
(`default-src 'none'`) the browser refuses it unless `manifest-src` names this
origin, so the exact policy is pinned here: one word more would be a loosening
nobody asked for.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.config import settings
from core.web import assistant, explorer, pages, staff_web


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "web_enabled", True)
    app = FastAPI()
    app.include_router(staff_web.router)
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


def test_manifest_is_public_and_typed(client):
    response = client.get("/web/manifest.webmanifest")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")
    assert response.headers["cache-control"] == "public, max-age=3600"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_manifest_fields(client):
    manifest = client.get("/web/manifest.webmanifest").json()

    assert manifest["id"] == "/web/"
    assert manifest["scope"] == "/web/"
    assert manifest["start_url"] == "/web/?app=1"
    assert manifest["display"] == "standalone"
    assert manifest["name"] == manifest["short_name"] == "Demo & Associados"
    assert manifest["lang"] == "pt-BR"
    assert manifest["background_color"] == manifest["theme_color"] == "#080808"
    assert [(i["src"], i["sizes"], i.get("purpose")) for i in manifest["icons"]] == [
        ("/web/icon-192.png", "192x192", None),
        ("/web/icon-512.png", "512x512", None),
        ("/web/icon-maskable-512.png", "512x512", "maskable"),
    ]
    assert all(i["type"] == "image/png" for i in manifest["icons"])


def test_the_manifest_works_with_the_pages_off(client):
    """Like the icons: it holds nothing, and installing is not a login."""
    settings.web_enabled = False
    try:
        assert client.get("/web/manifest.webmanifest").status_code == 200
    finally:
        settings.web_enabled = True


def test_every_page_links_the_manifest():
    head = bytes(pages.page("x", "y").body).decode()

    assert "<link rel=manifest href=/web/manifest.webmanifest>" in head
    assert "<meta name=theme-color" in head


@pytest.mark.parametrize("cookie, expected", [
    ("claro", ["<meta name=theme-color content=#f6f5f2>"]),
    ("escuro", ["<meta name=theme-color content=#080808>"]),
    (None, ["<meta name=theme-color media='(prefers-color-scheme: light)' content=#f6f5f2>",
            "<meta name=theme-color media='(prefers-color-scheme: dark)' content=#080808>"]),
])
def test_the_title_bar_follows_the_theme(cookie, expected):
    """The installed app's title bar on a computer is painted with this
    colour: fixed at night, it stayed black in the day theme (Paulo,
    2026-10-27)."""
    token = pages._theme.set(cookie)
    try:
        head = bytes(pages.page("x", "y").body).decode()
    finally:
        pages._theme.reset(token)

    assert head.count("name=theme-color") == len(expected)
    for meta in expected:
        assert meta in head


def test_default_policy_admits_the_manifest_and_nothing_else_new():
    policy = pages.page("x", "y").headers["content-security-policy"]

    assert policy == (
        "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "manifest-src 'self'"
    )


def test_script_pages_name_worker_src():
    for policy in (assistant.POLICY, explorer.POLICY):
        assert policy.endswith("; script-src 'self'; connect-src 'self'; worker-src 'self'")
        assert "manifest-src 'self'" in policy


# ── The service worker ──────────────────────────────────────────────────────

_STATIC = Path(staff_web.__file__).parent / "static"
_WORKER = (_STATIC / "sw.js").read_text()


def test_worker_is_served_with_its_own_policy(client):
    response = client.get("/web/sw.js")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["content-security-policy"] == "default-src 'none'; connect-src 'self'"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.text == _WORKER


def test_worker_works_with_the_pages_off(client):
    """It holds nothing, and an installed app must still be able to update it."""
    settings.web_enabled = False
    try:
        assert client.get("/web/sw.js").status_code == 200
    finally:
        settings.web_enabled = True


def test_worker_stores_nothing():
    """The browser keeps the worker's code; the worker keeps no data (rule 13:
    these are the calls that would store it, by name)."""
    assert _WORKER
    for call in ("caches.open", "cache.put", "cache.add", "cache.addAll", "indexedDB"):
        assert call not in _WORKER


def test_worker_touches_only_get_navigations():
    """Every other request, a form POST included, is left to the browser. The
    Chromium check proves it end to end; this pins the guard's text."""
    assert 'if (request.mode !== "navigate" || request.method !== "GET") return;' in _WORKER


def test_worker_has_its_three_handlers():
    for event in ("install", "activate", "fetch"):
        assert f'addEventListener("{event}"' in _WORKER
    assert "skipWaiting()" in _WORKER
    assert "clients.claim()" in _WORKER


def test_offline_answer_is_a_503_page_with_its_own_policy():
    assert "status: 503" in _WORKER
    assert "default-src 'none'; style-src 'unsafe-inline'" in _WORKER
    assert "Sem conexão." in _WORKER
    assert "Abra de novo quando a internet voltar." in _WORKER


def test_offline_page_fetches_nothing():
    """It is drawn from one string: no link, script, image, font or import."""
    page = _WORKER.split("`")[1]
    for fetched in ("<link", "<script", "<img", "src=", "url(", "@import", "http"):
        assert fetched not in page


def test_both_scripts_register_the_worker_at_web_scope():
    for name in ("assistant.js", "explorer.js"):
        source = (_STATIC / name).read_text()
        assert 'register("/web/sw.js", { scope: "/web/" })' in source
        assert '"serviceWorker" in navigator' in source


# ── the notices, run ────────────────────────────────────────────────────────
# sw.js runs under Node (tests/fixtures/worker_harness.mjs) with a stand-in
# for the registration and the window clients. What is asserted is what the
# worker shows and where a tap goes, not the source text. The Chromium check
# (a real push to the registered worker) is in the task's report.

NODE = shutil.which("node")
HARNESS = Path(__file__).parent / "fixtures" / "worker_harness.mjs"
needs_node = pytest.mark.skipif(
    NODE is None, reason="runs the service worker under Node, which is not installed here")
ORIGIN = "https://painel.example"
HOME = "/web/?app=1"


def _worker(*events, origin: str = ORIGIN) -> dict:
    assert NODE
    done = subprocess.run(
        [NODE, str(HARNESS), str(_STATIC / "sw.js"),
         json.dumps({"origin": origin, "events": list(events)})],
        capture_output=True, text=True, timeout=30, check=True)
    state = json.loads(done.stdout)
    assert state["errors"] == []
    return state


@needs_node
@pytest.mark.parametrize(("kind", "title"), [
    ("answer", "Resposta pronta"),
    ("djen", "Nova publicação no DJEN"),
    ("access", "Novo pedido de acesso"),
    ("deadline", "Prazo próximo"),
])
def test_a_push_shows_its_kind_s_fixed_title(kind, title):
    shown = _worker({"type": "push", "json": {"kind": kind, "url": HOME}})["shown"]

    assert shown == [{"title": title, "options": {"data": {"url": HOME}, "tag": kind}}]


@needs_node
@pytest.mark.parametrize("event", [
    {"json": {"kind": "constructor", "url": HOME}},
    {"json": {"kind": "toString"}},
    {"json": {"kind": "outro", "title": "Processo 123 de Fulano", "body": "segredo"}},
    {"json": {"kind": ["answer"]}},
    {"json": None},
    {"json": "answer"},
    {"raw": "not json"},
    {"none": True},
])
def test_any_other_push_still_shows_a_notice_and_nothing_it_carried(event):
    """WebKit revokes a subscription whose pushes show nothing; and no text
    from a push ever reaches the screen."""
    shown = _worker({"type": "push", **event})["shown"]

    assert len(shown) == 1
    assert shown[0]["title"] == "Novo aviso"
    assert set(shown[0]["options"]) == {"data", "tag"}
    assert "segredo" not in json.dumps(shown) and "Fulano" not in json.dumps(shown)


@needs_node
@pytest.mark.parametrize("url", [
    "https://evil/web/",
    "//evil/web/",
    "//painel.example/web/",
    "/web/../x",
    "/web/%2e%2e/x",
    "javascript:alert(1)",
    "/\\evil/web/",
    "/\tevil/web/",
    "web/",
    "/outra/",
    "/webx/",
    "",
    None,
    42,
])
def test_a_tap_goes_only_to_a_path_of_the_app(url):
    """Same origin alone is not enough: `//painel.example/web/` is this host,
    refused by the explicit `//` rule."""
    state = _worker({"type": "click", "data": {"url": url}})

    assert state["opened"] == [HOME]
    assert state["closed"] == 1


@needs_node
@pytest.mark.parametrize(("url", "landing"), [
    ("/web/?app=1", "/web/?app=1"),
    ("/web/assistente/#escrever", "/web/assistente/#escrever"),
    ("/web/", "/web/"),
    ("/web/x/../assistente/", "/web/assistente/"),
])
def test_a_tap_opens_a_path_of_the_app(url, landing):
    """What opens is the path as checked, resolved, not the text it came as."""
    assert _worker({"type": "click", "data": {"url": url}})["opened"] == [landing]


@needs_node
def test_a_tap_without_data_opens_the_app():
    assert _worker({"type": "click", "data": None})["opened"] == [HOME]


@needs_node
def test_a_tap_with_the_app_open_navigates_it_then_focuses_it():
    """Navigated, not only focused: the tap makes a request, which meets the
    session rules (Face ID when locked)."""
    state = _worker({"type": "click", "data": {"url": "//evil/"},
                     "windows": [{"url": ORIGIN + "/web/assistente/"}]})

    assert state["navigated"] == [HOME]
    assert state["focused"] == [ORIGIN + HOME]
    assert state["opened"] == []
    assert state["match_options"] == {"type": "window"}


@needs_node
def test_a_window_that_cannot_be_navigated_gives_way_to_a_new_one():
    state = _worker({"type": "click", "data": {"url": HOME},
                     "windows": [{"url": ORIGIN + "/web/", "navigate": "throw"},
                                 {"url": ORIGIN + "/web/x", "navigate": "null"}]})

    assert state["navigated"] == [HOME]
    assert state["focused"] == []
    assert state["opened"] == [HOME]


@needs_node
def test_a_refused_focus_still_counts_as_handled():
    state = _worker({"type": "click", "data": {"url": HOME},
                     "windows": [{"url": ORIGIN + "/web/", "focus": "throw"}]})

    assert state["navigated"] == [HOME]
    assert state["opened"] == []


def test_the_notices_store_nothing_either():
    """The two new handlers are in the file the storage test reads."""
    assert 'addEventListener("push"' in _WORKER
    assert 'addEventListener("notificationclick"' in _WORKER
