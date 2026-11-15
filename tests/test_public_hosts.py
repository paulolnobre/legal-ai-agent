"""Two public hosts, each carrying only its own paths.

The webhook's host answers Telegram and the health check. The pages' host
serves /web and /pje. Keeping them apart is what lets the pages move to the
firm's own domain without touching the bot: PUBLIC_BASE_URL is where
set_webhook points Telegram, so if the page links were built from it, moving
the pages would one day move the webhook to a host that does not route it,
and the bot would go quiet.
"""
import re
from pathlib import Path

import pytest

from core import config
from core.config import settings

_COMPOSE = Path("infra/vps/runtime-compose.yml").read_text(encoding="utf-8")


def _rule(router: str) -> str:
    found = re.search(
        rf"traefik\.http\.routers\.{router}\.rule: (.+)", _COMPOSE,
    )
    assert found, f"no rule for router {router}"
    return found.group(1)


def test_the_webhook_host_routes_only_the_webhook_and_health():
    rule = _rule("demo-firm-agent")

    assert "LEGAL_AGENT_HOST" in rule
    assert "Path(`/telegram/webhook`)" in rule and "Path(`/health`)" in rule
    # With the backtick: "/telegram/webhook" itself contains "/web".
    assert "`/web" not in rule and "`/pje" not in rule


def test_the_pages_host_routes_only_the_pages():
    rule = _rule("demo-firm-web")

    assert "LEGAL_WEB_HOST" in rule
    assert "PathPrefix(`/web/`)" in rule and "PathPrefix(`/pje/`)" in rule
    assert "`/telegram" not in rule and "`/health" not in rule
    assert "PathPrefix(`/`)" not in rule, "the pages host would expose everything"


def test_the_pages_host_is_served_over_tls():
    assert 'traefik.http.routers.demo-firm-web.tls: "true"' in _COMPOSE
    assert "traefik.http.routers.demo-firm-web.tls.certresolver:" in _COMPOSE


@pytest.fixture
def urls(monkeypatch):
    def set_urls(public, web):
        monkeypatch.setattr(settings, "public_base_url", public)
        monkeypatch.setattr(settings, "web_base_url", web)
    return set_urls


def test_links_use_the_pages_host_when_it_is_set(urls):
    urls("https://legal.exemplo.br", "https://lex.exemplo.br/")

    assert config.pages_base_url() == "https://lex.exemplo.br"


def test_a_single_host_install_falls_back_to_the_webhook_host(urls):
    urls("https://legal.exemplo.br/", None)

    assert config.pages_base_url() == "https://legal.exemplo.br"


def test_no_address_at_all_is_none_not_an_empty_link(urls):
    urls(None, "")

    assert config.pages_base_url() is None


def test_setting_the_pages_host_never_moves_the_webhook(urls):
    """The webhook registration reads PUBLIC_BASE_URL alone."""
    source = Path("core/services/telegram_registration.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))

    assert "web_base_url" not in code and "pages_base_url" not in code
    assert "public_base_url" in code
