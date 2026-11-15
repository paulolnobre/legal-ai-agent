"""Low-cardinality Prometheus metrics for the web, worker, and agent paths."""
from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter(
    "legal_http_requests_total",
    "HTTP requests handled by the application.",
    ("method", "path", "status"),
)
HTTP_REQUEST_SECONDS = Histogram(
    "legal_http_request_duration_seconds",
    "HTTP request latency.",
    ("method", "path"),
)

QUEUE_EVENTS = Counter(
    "legal_queue_events_total",
    "Durable queue lifecycle events.",
    ("event",),
)
QUEUE_DEPTH = Gauge(
    "legal_queue_depth",
    "Number of Telegram updates in each Redis queue.",
    ("queue",),
)
UPDATE_SECONDS = Histogram(
    "legal_update_processing_duration_seconds",
    "Time spent processing one Telegram update.",
    ("result",),
)

GRAPH_RUNS = Counter(
    "legal_graph_runs_total",
    "Completed LangGraph runs.",
    ("task", "result", "cached"),
)
GRAPH_SECONDS = Histogram(
    "legal_graph_run_duration_seconds",
    "End-to-end LangGraph execution latency.",
    ("task",),
)
RAG_HITS = Histogram(
    "legal_rag_hits",
    "Number of passages returned by a corpus retrieval.",
    buckets=(0, 1, 2, 4, 8, 16),
)
CACHE_REQUESTS = Counter(
    "legal_cache_requests_total",
    "Cache lookup outcomes.",
    ("namespace", "result"),
)

LLM_CALLS = Counter(
    "legal_llm_calls_total",
    "LLM call outcomes.",
    ("provider", "model", "result"),
)
LLM_SECONDS = Histogram(
    "legal_llm_call_duration_seconds",
    "LLM provider latency.",
    ("provider", "model"),
)
LLM_TOKENS = Counter(
    "legal_llm_tokens_total",
    "Tokens reported by LLM providers.",
    ("provider", "model", "direction"),
)

# What transcription is billed for: the audio sent to Whisper (web and
# Telegram), so the cost dashboard can price it. A counter: it survives a deploy
# through `increase()`, which the Redis meter and the gauges do not.
AUDIO_SECONDS = Counter(
    "legal_audio_seconds_total",
    "Seconds of audio sent to the transcription provider.",
)

DEPENDENCY_READY = Gauge(
    "legal_dependency_ready",
    "Whether a required dependency passed its latest readiness check.",
    ("dependency",),
)
EXTERNAL_CALLS = Counter(
    "legal_external_calls_total",
    "External dependency call outcomes.",
    ("dependency", "result"),
)
# How often an answer was held back for citing something it had not read.
# A rate that climbs is a model or retrieval problem worth seeing early, and a
# rate that is flat at zero forever is a guard worth re-testing.
# The only measurement here that says whether an answer was any good. Every
# other one says what happened, which does not separate a good answer from a
# confident wrong one.
ANSWER_FEEDBACK = Counter(
    "legal_answer_feedback_total",
    "Verdicts given on answers by the person who asked.",
    ("verdict", "kind"),
)
CITATION_CHECKS = Counter(
    "legal_citation_checks_total",
    "Outcome of the deterministic citation check on a produced answer.",
    ("result",),
)
EXTERNAL_SECONDS = Histogram(
    "legal_external_call_duration_seconds",
    "External dependency latency.",
    ("dependency",),
)


def observe_llm(
    provider: str,
    model: str,
    result: str,
    seconds: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    """Record one provider call without logging prompts or user identifiers."""
    LLM_CALLS.labels(provider, model, result).inc()
    LLM_SECONDS.labels(provider, model).observe(seconds)
    if input_tokens:
        LLM_TOKENS.labels(provider, model, "input").inc(input_tokens)
    if output_tokens:
        LLM_TOKENS.labels(provider, model, "output").inc(output_tokens)


# The pilot budget, as a fraction rather than a count, so one alert rule works
# whichever ceiling somebody changes. A gauge and not a counter: it goes down
# when a reservation is returned, and it resets when the window rolls over.
BUDGET_USED = Gauge(
    "legal_budget_used_ratio",
    "Share of the pilot token budget spent in the current window.",
    ("window", "kind"),
)
BUDGET_TOKENS = Gauge(
    "legal_budget_used_tokens",
    "Tokens counted against the pilot budget in the current window.",
    ("window", "kind"),
)


def observe_budget(window: str, kind: str, used: int, limit: int) -> None:
    """Where one counter stands, after a reservation or after its return."""
    BUDGET_TOKENS.labels(window, kind).set(used)
    if limit > 0:
        BUDGET_USED.labels(window, kind).set(used / limit)


def observe_external(dependency: str, result: str, seconds: float) -> None:
    EXTERNAL_CALLS.labels(dependency, result).inc()
    EXTERNAL_SECONDS.labels(dependency).observe(seconds)


def observe_feedback(verdict: str, changed: bool) -> None:
    """A lawyer said whether an answer helped."""
    ANSWER_FEEDBACK.labels(verdict, "changed" if changed else "new").inc()


def observe_citation_check(result: str) -> None:
    """One of: supported, unsupported, malformed, not_applicable."""
    CITATION_CHECKS.labels(result).inc()
