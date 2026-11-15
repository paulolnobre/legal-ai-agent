import json
import logging
import sys
from datetime import UTC, datetime

from core.observability import correlation_id, update_id


class JsonFormatter(logging.Formatter):
    """Single-line JSON formatter with safe correlation fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": correlation_id(),
            "update_id": update_id(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


# httpx logs every request at INFO with its full URL, and a Telegram API URL
# carries the bot token. Today nothing prints those records only because the
# root logger sits at WARNING with no handler; pinning these loggers keeps a
# later change to the root level from turning the token into a log line.
TOKEN_BEARING_LOGGERS = ("httpx", "httpcore")


def quiet_token_bearing_loggers() -> None:
    for name in TOKEN_BEARING_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def setup_logging() -> None:
    """Configure application loggers without replacing Uvicorn's own handlers."""
    quiet_token_bearing_loggers()
    app_logger = logging.getLogger("core")
    if any(getattr(handler, "_legal_json", False) for handler in app_logger.handlers):
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler._legal_json = True  # type: ignore[attr-defined]
    app_logger.handlers = [handler]
    app_logger.setLevel(logging.INFO)
    app_logger.propagate = False
