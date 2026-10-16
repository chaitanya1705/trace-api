from __future__ import annotations

from datetime import UTC

from psycopg.rows import dict_row
from psycopg.types.datetime import TimestamptzLoader
from psycopg_pool import AsyncConnectionPool


class _UtcTimestamptz(TimestamptzLoader):
    """Always return UTC (spec: stored/served UTC) regardless of the database session's timezone."""

    def load(self, data):
        return super().load(data).astimezone(UTC)


async def _configure(conn) -> None:
    conn.adapters.register_loader("timestamptz", _UtcTimestamptz)


async def open_pool(dsn: str, *, min_size: int = 1, max_size: int = 8) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        # prepare_threshold=None: no server-side prepared statements, so Supabase/pgbouncer transaction pooling also works
        kwargs={"row_factory": dict_row, "autocommit": False, "prepare_threshold": None},
        configure=_configure,
        open=False,
    )
    await pool.open(wait=True, timeout=10)
    return pool
