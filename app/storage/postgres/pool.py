from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


async def open_pool(dsn: str, *, min_size: int = 1, max_size: int = 8) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        kwargs={"row_factory": dict_row, "autocommit": False},
        open=False,
    )
    await pool.open(wait=True, timeout=10)
    return pool
