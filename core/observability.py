"""Correlation context shared by logs, metrics, and LangSmith metadata."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import os
from typing import Callable
from uuid import uuid4

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")
_update_id: ContextVar[str] = ContextVar("update_id", default="-")


def correlation_id() -> str:
    return _correlation_id.get()


def update_id() -> str:
    return _update_id.get()


def new_correlation_id(prefix: str = "req") -> str:
    return f"{prefix}-{uuid4().hex[:16]}"


def traced(**trace_options):
    """Apply LangSmith's decorator only when tracing was enabled at startup."""
    enabled = os.getenv("LANGSMITH_TRACING", "false").casefold() in {"1", "true", "yes"}

    def decorator(function: Callable) -> Callable:
        if not enabled:
            return function
        from langsmith import traceable

        return traceable(**trace_options)(function)

    return decorator


@contextmanager
def bind_context(*, correlation: str | None = None, update: object | None = None):
    """Bind safe identifiers for the current async execution context."""
    correlation_token = _correlation_id.set(correlation or new_correlation_id())
    update_token = _update_id.set(str(update) if update is not None else "-")
    try:
        yield
    finally:
        _update_id.reset(update_token)
        _correlation_id.reset(correlation_token)
