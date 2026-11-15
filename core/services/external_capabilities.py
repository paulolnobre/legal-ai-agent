"""Safe capability reporting and explicit canaries for external research.

Readiness is intentionally split in two layers: Redis and PostgreSQL decide
whether the application can receive work, while this module reports whether
external research is fully configured. Live probes are never run from an HTTP
health endpoint because they consume third-party quota and can be slow.
"""
from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from core.config import Settings, settings
from core.integrations import datajud, web_fetch, websearch

_DEPENDENCIES = ("web_search", "datajud", "web_fetch")
_DOMAIN = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9]"
    r"(?:[a-z0-9-]{0,61}[a-z0-9])?$",
    re.IGNORECASE,
)


def _dependency(
    status: str,
    reason: str | None = None,
    *,
    provider: str | None = None,
) -> dict[str, str]:
    result = {"status": status}
    if reason:
        result["reason"] = reason
    if provider:
        result["provider"] = provider
    return result


def _valid_https_endpoint(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and port in {None, 443}
    )


def _valid_allowed_domains(value: str) -> bool:
    raw_domains = [part.strip().lower().lstrip(".") for part in value.split(",")]
    domains = [domain for domain in raw_domains if domain]
    return bool(domains) and all(_DOMAIN.fullmatch(domain) for domain in domains)


def configuration_report(config: Settings | None = None) -> dict[str, Any]:
    """Return a secret-free, network-free view of external research capability."""
    config = config or settings
    domains_valid = _valid_allowed_domains(config.serper_allowed_domains)
    provider = config.web_search_provider

    # Report the provider that would actually answer. Reporting Serper while
    # Exa is the one serving would call the capability healthy on the strength
    # of a credential nothing is using.
    if provider == "exa":
        search_key = config.exa_api_key
        search_endpoint = config.exa_search_url
    else:
        search_key = config.serper_api_key
        search_endpoint = config.serper_search_url

    if not (search_key or "").strip():
        search_state = _dependency(
            "unconfigured", "missing_credential", provider=provider,
        )
    elif not _valid_https_endpoint(search_endpoint):
        search_state = _dependency(
            "misconfigured", "invalid_https_endpoint", provider=provider,
        )
    elif not domains_valid:
        search_state = _dependency(
            "misconfigured", "invalid_allowed_domains", provider=provider,
        )
    else:
        search_state = _dependency("configured", provider=provider)

    if not (config.datajud_api_key or "").strip():
        datajud_state = _dependency("unconfigured", "missing_credential")
    elif not _valid_https_endpoint(config.datajud_base_url):
        datajud_state = _dependency("misconfigured", "invalid_https_endpoint")
    else:
        datajud_state = _dependency("configured")

    if not config.fetch_enabled:
        fetch_state = _dependency("disabled", "disabled_by_operator")
    elif not domains_valid:
        fetch_state = _dependency("misconfigured", "invalid_allowed_domains")
    else:
        fetch_state = _dependency("configured")

    dependencies = {
        "web_search": search_state,
        "datajud": datajud_state,
        "web_fetch": fetch_state,
    }
    configured_sources = sum(
        dependencies[name]["status"] == "configured"
        for name in ("web_search", "datajud")
    )
    if configured_sources == 0:
        status = "unavailable"
    elif all(item["status"] == "configured" for item in dependencies.values()):
        status = "configured"
    else:
        status = "degraded"

    return {
        "status": status,
        "check": "configuration_only",
        "dependencies": dependencies,
    }


def configuration_problems(config: Settings | None = None) -> list[str]:
    """List malformed external settings that should stop startup.

    Missing optional credentials degrade external research but do not prevent
    local commands and corpus research from starting. A supplied credential
    paired with an unsafe endpoint or allowlist is an operator error and fails
    early instead of surfacing only on the first legal query.
    """
    report = configuration_report(config)
    problems: list[str] = []
    for name, state in report["dependencies"].items():
        if state["status"] != "misconfigured":
            continue
        if name == "web_search":
            variable = (
                f"{state['provider'].upper()}_SEARCH_URL"
                if state["reason"] == "invalid_https_endpoint"
                else "SERPER_ALLOWED_DOMAINS"
            )
        elif name == "datajud":
            variable = "DATAJUD_BASE_URL"
        else:
            variable = "SERPER_ALLOWED_DOMAINS"
        problems.append(f"{variable}: {state['reason']}")
    return problems


def validate_external_research_config(config: Settings | None = None) -> None:
    problems = configuration_problems(config)
    if problems:
        raise RuntimeError(
            "Incompatible external research configuration:\n  - "
            + "\n  - ".join(problems)
        )


async def _web_search_canary(provider: str) -> object:
    return await websearch.canary(provider)


async def _datajud_canary() -> object:
    return await datajud.canary()


async def _web_fetch_canary() -> object:
    return await web_fetch.canary()


async def run_external_canaries(
    config: Settings | None = None,
    checks: Mapping[str, Callable[[], Awaitable[object]]] | None = None,
) -> dict[str, Any]:
    """Run fixed, non-client probes once and return only operational metadata."""
    selected_config = config or settings
    config_report = configuration_report(selected_config)
    selected_checks = checks or {
        "web_search": lambda: _web_search_canary(
            selected_config.web_search_provider,
        ),
        "datajud": _datajud_canary,
        "web_fetch": _web_fetch_canary,
    }
    results: dict[str, dict[str, Any]] = {}

    for name in _DEPENDENCIES:
        identity = (
            {"provider": selected_config.web_search_provider}
            if name == "web_search"
            else {}
        )
        configured = config_report["dependencies"][name]["status"] == "configured"
        if not configured:
            results[name] = {
                "status": "skipped",
                "reason": config_report["dependencies"][name]["status"],
                **identity,
            }
            continue

        started = time.perf_counter()
        try:
            await selected_checks[name]()
        except Exception as exc:
            results[name] = {
                "status": "failed",
                "reason": type(exc).__name__,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                **identity,
            }
        else:
            results[name] = {
                "status": "passed",
                "latency_ms": round((time.perf_counter() - started) * 1000),
                **identity,
            }

    passed = sum(result["status"] == "passed" for result in results.values())
    if passed == len(_DEPENDENCIES):
        canary_status = "passed"
    elif passed:
        canary_status = "degraded"
    else:
        canary_status = "failed"
    return {
        "status": canary_status,
        "customer_data": "not_used",
        "dependencies": results,
    }
