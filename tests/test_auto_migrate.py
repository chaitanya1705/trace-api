import psycopg
import pytest

from app.config import Settings
from app.main import build_container, close_container
from app.storage.postgres.migrate import discover

pytestmark = pytest.mark.db


async def test_startup_creates_schema_on_a_fresh_database_and_is_idempotent(pg_dsn):
    admin = await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True)
    await admin.execute("DROP DATABASE IF EXISTS fresh_trace")
    await admin.execute("CREATE DATABASE fresh_trace")
    fresh = pg_dsn.rsplit("/", 1)[0] + "/fresh_trace"
    try:
        for _ in range(2):  # second start has nothing to apply and must not fail
            c = await build_container(Settings(trace_env="test", database_url=fresh))
            async with c.pool.connection() as conn:
                n = (await (await conn.execute("SELECT count(*) AS n FROM schema_migrations")).fetchone())["n"]
                await (await conn.execute("SELECT 1 FROM projects LIMIT 1")).fetchall()
            await close_container(c)
            assert n == len(discover())  # every shipped migration, however many there are
        off = await build_container(Settings(trace_env="test", database_url=fresh, auto_migrate=False))
        await close_container(off)
    finally:
        await admin.execute("DROP DATABASE IF EXISTS fresh_trace WITH (FORCE)")
        await admin.close()
