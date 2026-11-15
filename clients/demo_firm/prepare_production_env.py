"""Create a production environment file without exposing inherited secrets."""
from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path

from dotenv import dotenv_values

_INHERITED_KEYS = (
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "DATAJUD_API_KEY",
    "SERPER_API_KEY",
    "LLM_SIMPLE_PROVIDER",
    "LLM_COMPLEX_PROVIDER",
    "LLM_SIMPLE_MODEL",
    "LLM_COMPLEX_MODEL",
    "LLM_FALLBACK_MODEL",
    "LEGAL_MODEL",
    "ROUTER_MODEL",
    "RESEARCH_MODEL",
    "DRAFTING_MODEL",
    "REVIEW_MODEL",
    "OFFICE_TIMEZONE",
    "OFFICE_START_HOUR",
    "OFFICE_START_MINUTE",
    "OFFICE_END_HOUR",
    "OFFICE_END_MINUTE",
)


def _safe_value(value: str) -> str:
    """Reject line breaks because Compose env files are line-oriented."""
    if "\n" in value or "\r" in value:
        raise ValueError("Environment values must not contain line breaks.")
    return value


def build_environment(source: Path, hostname: str) -> dict[str, str]:
    inherited = {
        key: value
        for key, value in dotenv_values(source).items()
        if value is not None
    }
    bot_token = inherited.get("LEGAL_TELEGRAM_BOT_TOKEN") or inherited.get(
        "TELEGRAM_BOT_TOKEN"
    )
    if not bot_token:
        raise ValueError("The source env does not contain a Telegram bot token.")
    if not inherited.get("OPENAI_API_KEY"):
        raise ValueError("The source env does not contain OPENAI_API_KEY.")

    values = {
        "APP_ENV": "production",
        "APP_VERSION": "2.0.0",
        "METRICS_ENABLED": "true",
        "LEGAL_TELEGRAM_BOT_TOKEN": bot_token,
        "TELEGRAM_WEBHOOK_SECRET": secrets.token_hex(32),
        "PUBLIC_BASE_URL": f"https://{hostname}",
        "LEGAL_AGENT_HOST": hostname,
        "TRAEFIK_NETWORK": "edge",
        "DB_NAME": "demo_firm_v2",
        "DB_USER": "demo_firm_agent_v2",
        "DB_PASSWORD": secrets.token_urlsafe(32),
        "DB_SSLMODE": "disable",
        "REDIS_URL": "redis://redis:6379/0",
        "GRAFANA_ADMIN_USER": "admin",
        "GRAFANA_ADMIN_PASSWORD": secrets.token_urlsafe(24),
        "LANGSMITH_TRACING": "false",
        "LANGSMITH_PROJECT": "legal-ai-agent",
        "LANGSMITH_HIDE_INPUTS": "true",
        "LANGSMITH_HIDE_OUTPUTS": "true",
        "LANGSMITH_CAPTURE_CONTENT": "false",
    }
    values.update(
        (key, inherited[key]) for key in _INHERITED_KEYS if inherited.get(key)
    )
    return {key: _safe_value(value) for key, value in values.items()}


def write_environment(destination: Path, values: dict[str, str], *, force: bool) -> None:
    if destination.exists() and not force:
        raise FileExistsError(f"Refusing to overwrite {destination}; pass --force.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(destination)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    values = build_environment(args.source, args.hostname)
    write_environment(args.destination, values, force=args.force)
    optional_missing = [
        key for key in ("GEMINI_API_KEY", "DATAJUD_API_KEY", "SERPER_API_KEY")
        if key not in values
    ]
    print(f"Production environment written to {args.destination} (mode 0600).")
    if optional_missing:
        print("Optional credentials not inherited: " + ", ".join(optional_missing))


if __name__ == "__main__":
    main()
