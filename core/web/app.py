"""FastAPI webhook for the internal Telegram legal assistant."""
import asyncio
import contextlib
import hmac
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from core.config import deployed_commit, settings, validate_startup_config
from core.integrations import (
    cache,
    datajud,
    djen,
    embeddings,
    llm,
    microsoft,
    telegram,
    update_queue,
    web_fetch,
    websearch,
)
from core.handlers.legal_message import cancel_microsoft_sign_ins
from core.legal_agent import run_update_worker
from core.logger import setup_logging
from core.metrics import (
    DEPENDENCY_READY,
    HTTP_REQUEST_SECONDS,
    HTTP_REQUESTS,
    QUEUE_DEPTH,
)
from core.observability import bind_context, new_correlation_id
from core.repositories.connection import close_pool, get_pool
from core.web.pje_enrolment import router as pje_router
from core.web.pje_panel import router as pje_panel_router
from core.web import staff_web
from core.web.staff_web import router as staff_web_router
from core.web.explorer import router as explorer_router
from core.web.assistant import router as assistant_router
from core.web.editor import router as editor_router
from core.web.passkeys import router as passkeys_router
from core.web.files import router as files_router
from core.web.integrations import router as integrations_router
from core.web.processes import router as processes_router
from core.web.observability import router as observability_router
from core.repositories.documents import ensure_document_storage_schema
from core.repositories.callback_actions import ensure_callback_actions_schema
from core.repositories.inbox import ensure_inbox_schema
from core.repositories.publications import ensure_publication_schema
from core.repositories.answer_feedback import ensure_feedback_schema
from core.repositories.web_access import ensure_web_access_schema
from core.repositories.budget_history import ensure_budget_history_schema
from core.repositories.firm_holidays import ensure_firm_holidays_schema
from core.repositories.processes import ensure_process_schema
from core.repositories.conversation import ensure_conversation_schema
from core.repositories.panel import ensure_panel_schema
from core.services import telegram_registration
from core.repositories.pje_credentials import ensure_pje_schema
from core.repositories.teams import ensure_team_schema
from core.repositories.microsoft_accounts import ensure_microsoft_schema
from core.repositories.pending_actions import ensure_pending_actions_schema
from core.repositories.staff import ensure_access_schema
from core.schedule import OUT_OF_HOURS_MESSAGE, is_office_hours
from core.services import work_hours
from core.services import audio
from core.services.external_capabilities import (
    configuration_report,
    validate_external_research_config,
)
from core.services.inbox_sweep import run_inbox_sweep_worker
from core.services.law_refresh import run_law_refresh_worker
from core.services.deadline_alerts import run_deadline_alerts_worker
from core.services.publication_watch import run_publication_watch_worker
from core.services.maintenance import run_maintenance_worker

setup_logging()
logger = logging.getLogger(__name__)


def _chat_id(update: dict):
    """The chat an update belongs to, whatever shape it arrives in.

    A button press carries its chat one level deeper, inside the message the
    button is attached to. Missing it here would make the out-of-hours gate
    treat every press as an unknown chat and defer the owner's own taps.
    """
    message = update.get("message") or update.get("edited_message") or {}
    if not message:
        callback = update.get("callback_query") or {}
        message = callback.get("message") or {}
    return (message.get("chat") or {}).get("id")


def _is_button_press(update: dict) -> bool:
    """Whether this update is a tap on a button this bot already sent.

    A press is not new work arriving at the firm, it is the second half of an
    exchange the bot itself started, and Telegram gives it seconds to be
    answered before the button stops spinning. Deferring one until the morning
    therefore cannot be right: the person is looking at the screen now, the
    callback token expires meanwhile, and the queue would replay a decision
    hours after the moment that asked for it.
    """
    return bool(update.get("callback_query"))


def _valid_secret(received: str | None) -> bool:
    expected = settings.telegram_webhook_secret
    if not expected:
        return settings.app_env == "development"
    # As bytes: a header arrives decoded as latin-1, and compare_digest raises
    # on a str with anything outside ASCII, which made a refusal a 500.
    return bool(received) and hmac.compare_digest(received.encode(), expected.encode())


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Before anything else: a model pointed at the wrong provider is silently
    # swapped at request time, so the firm would run a model it did not choose.
    validate_startup_config()
    validate_external_research_config()
    external_research = configuration_report()
    if external_research["status"] != "configured":
        logger.warning(
            "External research capability is %s "
            "(web_search=%s provider=%s datajud=%s web_fetch=%s)",
            external_research["status"],
            external_research["dependencies"]["web_search"]["status"],
            external_research["dependencies"]["web_search"]["provider"],
            external_research["dependencies"]["datajud"]["status"],
            external_research["dependencies"]["web_fetch"]["status"],
        )
    pool = await get_pool()
    async with pool.connection() as conn:
        await ensure_document_storage_schema(conn)
        await ensure_access_schema(conn)
        await ensure_pending_actions_schema(conn)
        await ensure_callback_actions_schema(conn)
        await ensure_inbox_schema(conn)
        await ensure_publication_schema(conn)
        await ensure_team_schema(conn)
        await ensure_microsoft_schema(conn)
        await ensure_budget_history_schema(conn)
        await ensure_pje_schema(conn)
        await ensure_feedback_schema(conn)
        await ensure_web_access_schema(conn)
        await ensure_firm_holidays_schema(conn)
        await ensure_conversation_schema(conn)
        await ensure_panel_schema(conn)
        await ensure_process_schema(conn)
    stop_event = asyncio.Event()
    worker = asyncio.create_task(run_update_worker(stop_event), name="telegram-update-worker")
    housekeeping = asyncio.create_task(
        run_maintenance_worker(stop_event), name="retention-worker"
    )
    # Returns immediately when the sweep is off, so the task exists either way
    # and shutdown has one shape rather than two.
    sweeping = asyncio.create_task(
        run_inbox_sweep_worker(stop_event), name="inbox-sweep-worker"
    )
    refreshing = asyncio.create_task(
        run_law_refresh_worker(stop_event), name="law-refresh-worker"
    )
    watching = asyncio.create_task(
        run_publication_watch_worker(stop_event), name="publication-watch-worker"
    )
    alerting = asyncio.create_task(
        run_deadline_alerts_worker(stop_event), name="deadline-alerts-worker"
    )
    # The webhook and the "/" menu, told to Telegram at every production
    # start so a new command shows up without anybody remembering to run
    # set_webhook.py. In the background, because waiting on Telegram must not
    # delay readiness, and it never raises: the webhook already registered
    # keeps working through a Telegram outage.
    registering = asyncio.create_task(
        telegram_registration.register_if_production(), name="telegram-registration"
    )
    try:
        yield
    finally:
        stop_event.set()
        for task in (worker, housekeeping, sweeping, refreshing, watching, alerting,
                     registering):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await cancel_microsoft_sign_ins()
        await close_pool()
        await update_queue.close()
        await cache.close()
        await datajud.close()
        await djen.close()
        await embeddings.close()
        await llm.close()
        await websearch.close()
        await web_fetch.close()
        await microsoft.close()
        await telegram.close()
        await audio.close()


app = FastAPI(
    title="Demo & Associados Legal Agent",
    version=settings.app_version,
    docs_url=None if settings.app_env == "production" else "/docs",
    redoc_url=None,
    lifespan=lifespan,
)

app.include_router(pje_router)
app.include_router(staff_web_router)
app.include_router(files_router)
app.include_router(explorer_router)
app.include_router(assistant_router)
app.include_router(editor_router)
app.include_router(pje_panel_router)
app.include_router(integrations_router)
app.include_router(processes_router)
app.include_router(passkeys_router)
# Asked by Traefik before each request to Grafana; no public route reaches it.
app.include_router(observability_router)
# The lock and the app entry, raised by current_staff from any page above.
staff_web.install(app)


@app.middleware("http")
async def observe_http(request: Request, call_next):
    correlation = request.headers.get("x-request-id") or new_correlation_id()
    path = request.url.path
    started = time.perf_counter()
    response = None
    status_code = 500
    with bind_context(correlation=correlation):
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = correlation
            return response
        finally:
            HTTP_REQUESTS.labels(request.method, path, str(status_code)).inc()
            HTTP_REQUEST_SECONDS.labels(request.method, path).observe(
                time.perf_counter() - started
            )


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "environment": settings.app_env,
        "version": settings.app_version,
        "commit": deployed_commit(),
        "office_open": is_office_hours(),
    }


@app.get("/ready")
async def ready() -> dict:
    unavailable: list[str] = []
    dependencies: dict[str, str] = {}
    try:
        await update_queue.ping()
        DEPENDENCY_READY.labels("redis").set(1)
        dependencies["redis"] = "ready"
    except Exception:
        DEPENDENCY_READY.labels("redis").set(0)
        dependencies["redis"] = "unavailable"
        unavailable.append("queue")
        logger.exception("Redis readiness check failed")

    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute("SELECT 1")
        DEPENDENCY_READY.labels("postgres").set(1)
        dependencies["postgres"] = "ready"
    except Exception:
        DEPENDENCY_READY.labels("postgres").set(0)
        dependencies["postgres"] = "unavailable"
        unavailable.append("database")
        logger.exception("PostgreSQL readiness check failed")

    if unavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"unavailable dependencies: {', '.join(unavailable)}",
        )
    return {
        "status": "ready",
        "dependencies": dependencies,
        "capabilities": {"external_research": configuration_report()},
    }


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    if not settings.metrics_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        for queue, depth in (await update_queue.queue_depths()).items():
            QUEUE_DEPTH.labels(queue).set(depth)
    except Exception:
        DEPENDENCY_READY.labels("redis").set(0)
        logger.exception("Could not collect Redis queue depths")
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    if not _valid_secret(x_telegram_bot_api_secret_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid webhook")

    try:
        update = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid JSON") from exc
    if not isinstance(update, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid update")

    # Only the panel's own updates, built by the server, carry these: one
    # arriving from outside never passes for a panel request.
    update.pop("channel", None)
    update.pop("panel_request", None)
    message = update.get("message")
    if isinstance(message, dict):
        message.pop("recorded", None)

    # Out of hours, work waits for the morning (work_hours says when not).
    outside_hours = not await work_hours.may_work_now(
        _chat_id(update), button_press=_is_button_press(update),
    )

    try:
        inserted = await update_queue.enqueue_update(update, deferred=outside_hours)
    except (RuntimeError, ValueError) as exc:
        # A non-2xx makes Telegram retry; returning 200 here would lose the update.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="could not persist update",
        ) from exc

    if inserted and outside_hours:
        chat_id = _chat_id(update)
        if chat_id is not None:
            await telegram.send_message(chat_id, OUT_OF_HOURS_MESSAGE, parse_mode=None)

    return {"ok": True, "queued": inserted, "deferred": outside_hours}
