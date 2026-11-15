"""Database connections: async pool for the bots, plain connections for tools."""
import logging

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg_pool import AsyncConnectionPool

from core.config import settings

logger = logging.getLogger(__name__)

_pool: AsyncConnectionPool | None = None


def _conninfo() -> str:
    if settings.database_url:
        return make_conninfo(settings.database_url)
    return make_conninfo(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        sslmode=settings.db_sslmode,
    )


async def get_pool() -> AsyncConnectionPool:
    """Shared async connection pool. Each update handler borrows a connection,
    which gives it one transaction per update (committed on clean exit,
    rolled back on failure)."""
    global _pool
    if _pool is None:
        logger.info(
            "Opening connection pool to %s@%s:%s/%s",
            settings.db_user, settings.db_host, settings.db_port, settings.db_name,
        )
        _pool = AsyncConnectionPool(
            _conninfo(),
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            open=False,
        )
        await _pool.open()
    return _pool


async def connect_async() -> psycopg.AsyncConnection:
    """Standalone async connection (CLI and one-off scripts)."""
    return await psycopg.AsyncConnection.connect(_conninfo())


async def close_pool() -> None:
    """Close the process-wide pool during application shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def connect_db() -> psycopg.Connection:
    """Synchronous connection for smoke tests and one-off scripts."""
    logger.info(
        "Connecting to database %s@%s:%s/%s",
        settings.db_user, settings.db_host, settings.db_port, settings.db_name,
    )
    return psycopg.connect(_conninfo())
