"""Durable Redis queues for Telegram updates.

The webhook acknowledges Telegram only after an update is stored. A processing
list keeps reserved items recoverable if the worker process is interrupted.
"""
import json
import logging

import redis.asyncio as redis
from redis.exceptions import RedisError

from core.config import settings
from core.metrics import QUEUE_EVENTS

logger = logging.getLogger(__name__)

_client: redis.Redis | None = None

_ENQUEUE_SCRIPT = """
if redis.call('SET', KEYS[1], '1', 'EX', ARGV[1], 'NX') then
  redis.call('LPUSH', KEYS[2], ARGV[2])
  return 1
end
return 0
"""


def _redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=max(10, settings.queue_block_seconds + 5),
            health_check_interval=30,
        )
    return _client


def _queue_name(deferred: bool) -> str:
    return settings.queue_deferred_name if deferred else settings.queue_active_name


async def enqueue_update(update: dict, *, deferred: bool) -> bool:
    """Persist an update exactly once. Returns False for a Telegram retry."""
    update_id = update.get("update_id")
    if update_id is None:
        raise ValueError("Telegram update without update_id")
    payload = json.dumps(update, ensure_ascii=False, separators=(",", ":"))
    dedupe_key = f"legal:update:{update_id}"
    try:
        inserted = await _redis().eval(
            _ENQUEUE_SCRIPT,
            2,
            dedupe_key,
            _queue_name(deferred),
            settings.queue_dedupe_ttl,
            payload,
        )
    except (RedisError, OSError) as exc:
        logger.error("Could not durably enqueue Telegram update: %s", exc)
        raise RuntimeError("Update queue unavailable") from exc
    QUEUE_EVENTS.labels("enqueued" if inserted else "duplicate").inc()
    return bool(inserted)


async def recover_processing() -> int:
    """Return work reserved by a replica that stopped before acknowledging it."""
    recovered = 0
    client = _redis()
    while True:
        payload = await client.rpoplpush(
            settings.queue_processing_name,
            settings.queue_active_name,
        )
        if payload is None:
            break
        recovered += 1
    if recovered:
        QUEUE_EVENTS.labels("recovered").inc(recovered)
        logger.warning("Recovered %d interrupted Telegram updates", recovered)
    return recovered


async def reserve_update(*, deferred: bool) -> str | None:
    """Atomically reserve one item so it survives a worker interruption."""
    payload = await _redis().brpoplpush(
        _queue_name(deferred),
        settings.queue_processing_name,
        timeout=settings.queue_block_seconds,
    )
    if payload is None:
        return None
    if isinstance(payload, bytes):
        return payload.decode("utf-8")
    return str(payload)


async def acknowledge_update(payload: str) -> None:
    await _redis().lrem(settings.queue_processing_name, 1, payload)
    QUEUE_EVENTS.labels("acknowledged").inc()


async def retry_update(payload: str, update_id: int | str | None) -> bool:
    """Requeue a failed item, or dead-letter it after the configured limit."""
    client = _redis()
    attempts_key = f"legal:update-attempts:{update_id or 'unknown'}"
    async with client.pipeline(transaction=True) as pipe:
        pipe.incr(attempts_key)
        pipe.expire(attempts_key, settings.queue_dedupe_ttl)
        attempts, _ = await pipe.execute()

    destination = (
        settings.queue_dead_letter_name
        if int(attempts) >= settings.queue_max_attempts
        else settings.queue_active_name
    )
    async with client.pipeline(transaction=True) as pipe:
        pipe.lrem(settings.queue_processing_name, 1, payload)
        pipe.lpush(destination, payload)
        await pipe.execute()
    QUEUE_EVENTS.labels(
        "retried" if destination == settings.queue_active_name else "dead_lettered"
    ).inc()
    return destination != settings.queue_dead_letter_name


def decode_update(payload: str) -> dict:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("Queued Telegram update is not an object")
    return value


async def ping() -> bool:
    return bool(await _redis().ping())


async def queue_depths() -> dict[str, int]:
    """Return bounded operational queue sizes for the Prometheus scrape."""
    names = {
        "active": settings.queue_active_name,
        "deferred": settings.queue_deferred_name,
        "processing": settings.queue_processing_name,
        "dead_letter": settings.queue_dead_letter_name,
    }
    async with _redis().pipeline(transaction=False) as pipe:
        for redis_name in names.values():
            pipe.llen(redis_name)
        values = await pipe.execute()
    return {label: int(value) for label, value in zip(names, values, strict=True)}


async def close() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except (RedisError, OSError):
            pass
        _client = None
