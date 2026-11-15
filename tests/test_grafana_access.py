"""Grafana at /grafana/ on the pages' host, opened by the pages' sign-in.

Grafana believes one header about who is calling. The whole property is that
only the pages decide that header's value: Traefik drops the client's copy,
asks the pages, copies their answer, and Grafana accepts it from Traefik's
subnet alone. Each link of that chain is configuration that only fails on the
VPS, so each is asserted here; the decision itself runs on real Postgres.
"""
import asyncio
import contextlib
import ipaddress
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.config import settings
from core.repositories import web_access
from core.services import authorization
from core.web import observability, staff_web

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = yaml.safe_load((ROOT / "infra/vps/runtime-compose.yml").read_text(encoding="utf-8"))
EDGE = yaml.safe_load((ROOT / "infra/edge/edge-compose.yml").read_text(encoding="utf-8"))
GRAFANA = RUNTIME["services"]["grafana"]
LABELS = GRAFANA["labels"]
ENV = GRAFANA["environment"]
ROUTER = "traefik.http.routers.demo-firm-grafana"
MIDDLEWARES = "traefik.http.middlewares"
GRAFANA_NETWORK = "demo-firm-grafana"

PAULO, RUI, ANA = 1, 4, 5  # owner, advogado, estagiario in the test seed


# ── the wiring ───────────────────────────────────────────────────────────────

def test_grafana_is_routed_only_under_its_prefix_on_the_pages_host():
    rule = LABELS[f"{ROUTER}.rule"]

    assert "LEGAL_WEB_HOST" in rule and "LEGAL_AGENT_HOST" not in rule
    assert "PathPrefix(`/grafana/`)" in rule
    assert "`/web" not in rule and "`/pje" not in rule and "PathPrefix(`/`)" not in rule
    assert LABELS[f"{ROUTER}.tls"] == "true"
    assert ENV["GF_SERVER_ROOT_URL"].endswith("/grafana/")
    assert "LEGAL_WEB_HOST" in ENV["GF_SERVER_ROOT_URL"]


def test_the_clients_header_is_dropped_before_the_pages_are_asked():
    """Traefik runs middlewares in the order listed. Reversed, the drop would
    erase the pages' answer instead of the client's forgery."""
    chain = LABELS[f"{ROUTER}.middlewares"].split(",")

    assert chain == ["demo-firm-grafana-drop-user", "demo-firm-grafana-auth"]
    header = ENV["GF_AUTH_PROXY_HEADER_NAME"]
    assert header == observability.GRAFANA_USER_HEADER
    drop = f"{MIDDLEWARES}.demo-firm-grafana-drop-user.headers.customrequestheaders.{header}"
    assert LABELS[drop] == ""
    copied = LABELS[f"{MIDDLEWARES}.demo-firm-grafana-auth.forwardauth.authResponseHeaders"]
    assert copied == header


def test_traefik_asks_the_endpoint_that_exists_on_a_name_that_resolves():
    address = LABELS[f"{MIDDLEWARES}.demo-firm-grafana-auth.forwardauth.address"]
    host_port, path = address.removeprefix("http://").split("/", 1)
    host, port = host_port.split(":")

    assert "/" + path == observability.GRAFANA_AUTH_PATH
    assert port == "8000"
    # The sign-in redirect is relative; without this Traefik rewrites it onto
    # the internal host above, which no browser can reach.
    auth = f"{MIDDLEWARES}.demo-firm-grafana-auth.forwardauth"
    assert LABELS[f"{auth}.preserveLocationHeader"] == "true"
    bot_networks = RUNTIME["services"]["legal-bot"]["networks"]
    assert host in bot_networks["traefik"]["aliases"]


def test_the_question_traefik_asks_has_no_public_route():
    """Every public router names its prefixes; the endpoint sits outside all."""
    path = observability.GRAFANA_AUTH_PATH
    rules = [value for key, value in RUNTIME["services"]["legal-bot"]["labels"].items()
             if key.endswith(".rule")] + [LABELS[f"{ROUTER}.rule"]]
    for prefix in ("/web/", "/pje/", "/grafana/", "/telegram/", "/health"):
        assert not path.startswith(prefix)
    for rule in rules:
        assert f"`{path}" not in rule and "/internal" not in rule


def test_grafana_believes_the_header_only_from_the_subnet_traefik_alone_shares():
    whitelist = ipaddress.ip_network(ENV["GF_AUTH_PROXY_WHITELIST"])
    edge_network = EDGE["networks"]["grafana"]
    subnets = [ipaddress.ip_network(entry["subnet"])
               for entry in edge_network["ipam"]["config"]]

    assert edge_network["name"] == GRAFANA_NETWORK
    assert subnets == [whitelist]
    assert RUNTIME["networks"]["grafana"] == {"external": True, "name": GRAFANA_NETWORK}
    assert LABELS["traefik.docker.network"] == GRAFANA_NETWORK
    assert "grafana" in EDGE["services"]["traefik"]["networks"]
    joined = [name for name, service in RUNTIME["services"].items()
              if "grafana" in (service.get("networks") or [])]
    assert joined == ["grafana"], f"{joined} could send Grafana the login header"


def test_grafana_has_no_other_way_in():
    assert "ports" not in GRAFANA, "a published port skips the pages' sign-in"
    assert ENV["GF_AUTH_PROXY_ENABLED"] == "true"
    assert ENV["GF_AUTH_DISABLE_LOGIN_FORM"] == "true"
    assert ENV["GF_AUTH_BASIC_ENABLED"] == "false"
    assert ENV["GF_AUTH_ANONYMOUS_ENABLED"] == "false"
    assert ENV["GF_USERS_ALLOW_SIGN_UP"] == "false"


def test_opening_grafana_is_the_owners_alone():
    assert authorization.required_rank(observability.ACTION) == "owner"


def test_the_application_serves_the_question():
    from core.web.app import app

    paths = {getattr(route, "path", None) for route in app.routes}
    assert observability.GRAFANA_AUTH_PATH in paths


# ── the decision, on real Postgres ───────────────────────────────────────────

@pytest.fixture
def site(db, monkeypatch):
    async def build():
        conn = await db.connect()
        try:
            await db.build(conn)
            await conn.commit()
        finally:
            await conn.close()

    asyncio.run(build())

    class _Pool:
        def connection(self):
            @contextlib.asynccontextmanager
            async def opened():
                conn = await db.connect()
                try:
                    yield conn
                    await conn.commit()
                finally:
                    await conn.close()
            return opened()

    async def get_pool():
        return _Pool()

    monkeypatch.setattr(settings, "web_enabled", True)
    monkeypatch.setattr(observability, "get_pool", get_pool)
    app = FastAPI()
    app.include_router(observability.router)
    client = TestClient(app, base_url="https://testserver", follow_redirects=False)

    def run(fn):
        async def main():
            conn = await db.connect()
            try:
                result = await fn(conn)
                await conn.commit()
                return result
            finally:
                await conn.close()
        return asyncio.run(main())

    try:
        yield type("Site", (), {"client": client, "run": staticmethod(run)})
    finally:
        client.close()
        asyncio.run(db.drop())


def _ask(site, who: int | None, *, navigate: bool = False, forged: str | None = None):
    headers = {}
    if who is not None:
        token = site.run(lambda conn: web_access.open_session(conn, who, idle_seconds=3600))
        headers["cookie"] = f"{staff_web.COOKIE}={token}"
    if navigate:
        headers["sec-fetch-mode"] = "navigate"
    if forged is not None:
        headers[observability.GRAFANA_USER_HEADER] = forged
    return site.client.get(observability.GRAFANA_AUTH_PATH, headers=headers)


def test_the_owner_is_let_in_under_their_staff_id(site):
    answer = _ask(site, PAULO)

    assert answer.status_code == 204
    assert answer.headers[observability.GRAFANA_USER_HEADER] == "staff-1"


@pytest.mark.parametrize("who", [RUI, ANA])
def test_every_other_rank_is_refused_and_named_nothing(site, who):
    answer = _ask(site, who, forged="staff-1")

    assert answer.status_code == 403
    assert observability.GRAFANA_USER_HEADER not in answer.headers


def test_nobody_signed_in_is_refused_even_naming_the_owner(site):
    answer = _ask(site, None, forged="staff-1")

    assert answer.status_code == 401
    assert observability.GRAFANA_USER_HEADER not in answer.headers


def test_a_page_load_without_a_session_is_sent_to_sign_in(site):
    answer = _ask(site, None, navigate=True)

    assert answer.status_code == 303
    assert answer.headers["location"] == "/web/"
    assert observability.GRAFANA_USER_HEADER not in answer.headers


def test_a_forged_cookie_is_nobody(site):
    answer = site.client.get(observability.GRAFANA_AUTH_PATH,
                             headers={"cookie": f"{staff_web.COOKIE}=not-a-session"})

    assert answer.status_code == 401


def test_a_deactivated_owner_is_refused_on_the_next_request(site):
    """The session outlives the staff row's change; the access does not."""
    token = site.run(lambda conn: web_access.open_session(conn, PAULO, idle_seconds=3600))
    cookie = {"cookie": f"{staff_web.COOKIE}={token}"}
    assert site.client.get(observability.GRAFANA_AUTH_PATH, headers=cookie).status_code == 204

    async def deactivate(conn):
        async with conn.cursor() as cur:
            await cur.execute("UPDATE staff SET active = FALSE WHERE id = %s", (PAULO,))
    site.run(deactivate)

    answer = site.client.get(observability.GRAFANA_AUTH_PATH, headers=cookie)
    assert answer.status_code == 401
    assert observability.GRAFANA_USER_HEADER not in answer.headers


def test_a_demoted_owner_is_refused_on_the_next_request(site):
    token = site.run(lambda conn: web_access.open_session(conn, PAULO, idle_seconds=3600))
    cookie = {"cookie": f"{staff_web.COOKIE}={token}"}

    async def demote(conn):
        async with conn.cursor() as cur:
            await cur.execute("UPDATE staff SET role = 'admin' WHERE id = %s", (PAULO,))
    site.run(demote)

    assert site.client.get(observability.GRAFANA_AUTH_PATH, headers=cookie).status_code == 403


def test_nothing_is_answered_while_the_pages_are_off(site, monkeypatch):
    monkeypatch.setattr(settings, "web_enabled", False)

    assert _ask(site, PAULO).status_code == 404
