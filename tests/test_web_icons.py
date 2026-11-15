"""The firm's icon on the browser tab, the same files as the firm's site.

The proxy forwards only /web/ and /pje/ to this host, so the browser's own
guess (/favicon.ico at the root) never reaches it: every page has to name the
icons under /web/, and the stylesheet-only policy has to admit an image from
this same origin and nowhere else.
"""
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from core.config import settings
from core.web import pages, staff_web


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "web_enabled", True)
    app = FastAPI()
    app.include_router(staff_web.router)
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


@pytest.mark.parametrize("path, media_type, magic", [
    ("/web/favicon.ico", "image/x-icon", b"\x00\x00\x01\x00"),
    ("/web/favicon-32x32.png", "image/png", b"\x89PNG"),
    ("/web/apple-touch-icon.png", "image/png", b"\x89PNG"),
])
def test_each_icon_is_served_as_the_image_it_is(client, path, media_type, magic):
    response = client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"] == media_type
    assert response.content.startswith(magic)
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("path, side", [
    ("/web/icon-192.png", 192),
    ("/web/icon-512.png", 512),
    ("/web/icon-maskable-512.png", 512),
])
def test_each_app_icon_is_a_png_of_the_size_the_manifest_says(client, path, side):
    response = client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")
    assert Image.open(io.BytesIO(response.content)).size == (side, side)


def test_every_page_names_the_icons_under_web():
    head = bytes(pages.page("Teste", "<p>x</p>").body).decode()

    assert "<link rel=icon href=/web/favicon.ico sizes=32x32>" in head
    assert "<link rel=icon href=/web/favicon-32x32.png type=image/png sizes=32x32>" in head
    assert "<link rel=apple-touch-icon href=/web/apple-touch-icon.png>" in head


def test_the_policy_a_page_carries_admits_images_from_this_origin_only():
    """Read off the response, not the constant: a page that stopped sending
    the header would still have the constant."""
    policy = pages.page("Teste", "<p>x</p>").headers["content-security-policy"]
    directives = {part.split()[0]: part.split()[1:]
                  for part in (p.strip() for p in policy.split(";")) if part}

    assert directives["img-src"] == ["'self'"]
    assert directives["default-src"] == ["'none'"]
    assert "script-src" not in directives


def test_the_icon_works_with_the_pages_off(client):
    """The pages-off answer is still a page with a tab."""
    settings.web_enabled = False
    try:
        assert client.get("/web/favicon.ico").status_code == 200
    finally:
        settings.web_enabled = True
