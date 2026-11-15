"""External research is observable without leaking data or consuming health-check quota."""
import asyncio
import inspect
import json
from typing import Any

import pytest

from core.config import Settings
from core.integrations import datajud, exa, serper, web_fetch, websearch
from core.services.external_capabilities import (
    configuration_report,
    run_external_canaries,
    validate_external_research_config,
)
from core.web import app as web_app


class _Settings(Settings):
    """Ignore the machine environment so capability tests are deterministic."""

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings,
        file_secret_settings,
    ):
        return (init_settings,)


def _config(**changes: Any) -> _Settings:
    values: dict[str, Any] = {
        "app_env": "development",
        "serper_api_key": "serper-secret",
        "datajud_api_key": "datajud-secret",
    }
    values.update(changes)
    return _Settings(**values)


def test_configured_capability_report_is_secret_free():
    report = configuration_report(_config())

    assert report == {
        "status": "configured",
        "check": "configuration_only",
        "dependencies": {
            "web_search": {"status": "configured", "provider": "serper"},
            "datajud": {"status": "configured"},
            "web_fetch": {"status": "configured"},
        },
    }
    serialized = json.dumps(report)
    assert "serper-secret" not in serialized
    assert "datajud-secret" not in serialized


def test_one_missing_source_degrades_but_does_not_disable_external_research():
    report = configuration_report(_config(serper_api_key=None))

    assert report["status"] == "degraded"
    assert report["dependencies"]["web_search"] == {
        "status": "unconfigured",
        "reason": "missing_credential",
        "provider": "serper",
    }
    assert report["dependencies"]["datajud"]["status"] == "configured"


def test_no_search_source_makes_only_external_research_unavailable():
    report = configuration_report(_config(serper_api_key=None, datajud_api_key=None))

    assert report["status"] == "unavailable"
    assert report["check"] == "configuration_only"


@pytest.mark.parametrize(
    ("change", "variable"),
    [
        ({"serper_search_url": "http://google.serper.dev/search"}, "SERPER_SEARCH_URL"),
        (
            {
                "web_search_provider": "exa",
                "exa_api_key": "exa-secret",
                "exa_search_url": "http://api.exa.ai/search",
            },
            "EXA_SEARCH_URL",
        ),
        ({"datajud_base_url": "http://datajud.invalid"}, "DATAJUD_BASE_URL"),
        ({"serper_allowed_domains": "https://gov.br"}, "SERPER_ALLOWED_DOMAINS"),
    ],
)
def test_unsafe_external_configuration_fails_early(change, variable):
    with pytest.raises(RuntimeError, match=variable):
        validate_external_research_config(_config(**change))


def test_external_configuration_is_validated_before_the_database_is_opened():
    source = inspect.getsource(web_app.lifespan)

    assert source.index("validate_external_research_config") < source.index("get_pool")


def test_only_the_selected_search_provider_is_required():
    exa_report = configuration_report(_config(
        web_search_provider="exa",
        exa_api_key="exa-secret",
        serper_api_key=None,
    ))
    serper_report = configuration_report(_config(exa_api_key=None))

    assert exa_report["dependencies"]["web_search"] == {
        "status": "configured",
        "provider": "exa",
    }
    assert serper_report["dependencies"]["web_search"] == {
        "status": "configured",
        "provider": "serper",
    }
    assert "exa-secret" not in json.dumps(exa_report)


def test_canaries_call_each_configured_dependency_exactly_once():
    calls: list[str] = []

    def check(name):
        async def run():
            calls.append(name)

        return run

    report = asyncio.run(run_external_canaries(
        _config(),
        checks={name: check(name) for name in ("web_search", "datajud", "web_fetch")},
    ))

    assert report["status"] == "passed"
    assert report["customer_data"] == "not_used"
    assert calls == ["web_search", "datajud", "web_fetch"]
    assert report["dependencies"]["web_search"]["provider"] == "serper"


def test_canaries_skip_unconfigured_dependencies_and_report_partial_failure():
    calls: list[str] = []

    async def passed():
        calls.append("datajud")

    async def failed():
        calls.append("web_fetch")
        raise TimeoutError("must not reach the report")

    report = asyncio.run(run_external_canaries(
        _config(serper_api_key=None),
        checks={"web_search": passed, "datajud": passed, "web_fetch": failed},
    ))

    assert calls == ["datajud", "web_fetch"]
    assert report["status"] == "degraded"
    assert report["dependencies"]["web_search"]["status"] == "skipped"
    assert report["dependencies"]["web_search"]["provider"] == "serper"
    assert report["dependencies"]["web_fetch"]["reason"] == "TimeoutError"
    assert "must not reach" not in json.dumps(report)


def test_serper_canary_uses_one_fixed_non_client_query(monkeypatch):
    calls = []

    async def search_domain(query, domain, key, domains):
        calls.append((query, domain, key, domains))
        return []

    monkeypatch.setattr(serper.settings, "serper_api_key", "test-key")
    monkeypatch.setattr(serper, "allowed_domains", lambda: ("gov.br", "jus.br"))
    monkeypatch.setattr(serper, "_search_domain", search_domain)

    asyncio.run(serper.canary())

    assert calls == [
        (serper.CANARY_QUERY, serper.CANARY_DOMAIN, "test-key", ("gov.br", "jus.br"))
    ]
    assert "processo" not in serper.CANARY_QUERY.casefold()


def test_web_search_canary_calls_only_the_selected_provider(monkeypatch):
    calls = []

    async def serper_canary():
        calls.append("serper")

    async def exa_canary():
        calls.append("exa")

    monkeypatch.setattr(serper, "canary", serper_canary)
    monkeypatch.setattr(exa, "canary", exa_canary)

    asyncio.run(websearch.canary("exa"))
    assert calls == ["exa"]

    calls.clear()
    asyncio.run(websearch.canary("serper"))
    assert calls == ["serper"]


@pytest.mark.parametrize("provider", ["serper", "exa"])
def test_the_scheduled_canary_probes_the_provider_that_would_answer(monkeypatch, provider):
    """No checks are injected, so the canary picks its own probes, as it does in
    production. Both keys are set, so a skipped probe cannot hide a wrong one:
    probing Serper while Exa serves would pass on a supplier nothing is using."""
    calls: list[str] = []

    def record(name):
        async def probe():
            calls.append(name)

        return probe

    monkeypatch.setattr(serper, "canary", record("serper"))
    monkeypatch.setattr(exa, "canary", record("exa"))
    monkeypatch.setattr(datajud, "canary", record("datajud"))
    monkeypatch.setattr(web_fetch, "canary", record("web_fetch"))

    report = asyncio.run(run_external_canaries(_config(
        web_search_provider=provider,
        exa_api_key="exa-secret",
    )))

    assert [name for name in calls if name in ("serper", "exa")] == [provider]
    assert report["dependencies"]["web_search"]["status"] == "passed"
    assert report["dependencies"]["web_search"]["provider"] == provider


def test_web_search_shutdown_closes_both_provider_clients(monkeypatch):
    calls = []

    async def serper_close():
        calls.append("serper")

    async def exa_close():
        calls.append("exa")

    monkeypatch.setattr(serper, "close", serper_close)
    monkeypatch.setattr(exa, "close", exa_close)

    asyncio.run(websearch.close())

    assert calls == ["serper", "exa"]


def test_datajud_canary_uses_only_the_synthetic_process_number(monkeypatch):
    numbers = []

    async def lookup(number):
        numbers.append(number)

    monkeypatch.setattr(datajud, "lookup_process", lookup)
    asyncio.run(datajud.canary())

    assert numbers == [datajud.CANARY_PROCESS_NUMBER]
    assert datajud.normalize_process_number(datajud.CANARY_PROCESS_NUMBER) == (
        "00000000019008260000"
    )


def test_page_canary_uses_a_fixed_or_operator_authorized_domain(monkeypatch):
    urls = []

    async def fetch(url):
        urls.append(url)

    monkeypatch.setattr(web_fetch, "allowed_domains", lambda: ("gov.br",))
    monkeypatch.setattr(web_fetch, "fetch_page", fetch)

    asyncio.run(web_fetch.canary())

    assert urls == [web_fetch.CANARY_URL]
