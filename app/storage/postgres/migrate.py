"""Tiny forward-only SQL migration runner.

Files: migrations/postgres/NNNN_name.sql, applied in order inside one transaction each and recorded in
schema_migrations with a checksum. A changed already-applied file is an error, never silently re-run.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from psycopg import AsyncConnection

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations" / "postgres"
LOCK_KEY = 7_262_001  # arbitrary app-wide advisory lock id


class MigrationError(RuntimeError):
    pass


def discover(directory: Path = MIGRATIONS_DIR) -> list[tuple[str, str, str]]:
    out = []
    for p in sorted(directory.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        sql = p.read_text(encoding="utf-8")
        out.append((p.stem, sql, hashlib.sha256(sql.encode()).hexdigest()))
    return out


async def migrate(dsn: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    applied_now: list[str] = []
    async with await AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            cur = await conn.execute("SELECT version, checksum FROM schema_migrations")
            done = {r[0]: r[1] for r in await cur.fetchall()}
            for version, sql, checksum in discover(directory):
                if version in done:
                    if done[version] != checksum:
                        raise MigrationError(f"migration {version} changed after being applied")
                    continue
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations(version, checksum) VALUES (%s, %s)", (version, checksum)
                    )
                applied_now.append(version)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    return applied_now


if __name__ == "__main__":  # python -m app.storage.postgres.migrate
    import asyncio

    from app.config import load_settings

    s = load_settings()
    if not s.database_url:
        raise SystemExit("DATABASE_URL is not set")
    print("applied:", asyncio.run(migrate(s.database_url)) or "nothing to do")
