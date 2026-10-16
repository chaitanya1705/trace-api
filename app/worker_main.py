"""Worker process entry: python -m app.worker_main  (runs beside the API; shares code and the Postgres queue)."""
from __future__ import annotations

import asyncio
import logging

import app.jobs.handlers  # noqa: F401  (registers job handlers)
import app.jobs.handlers.deployments  # noqa: F401
import app.jobs.handlers.schema  # noqa: F401
from app.config import load_settings
from app.jobs.worker import run_forever
from app.main import build_container, close_container


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    s = load_settings()
    if not s.database_url:
        raise SystemExit("DATABASE_URL is not set")
    c = await build_container(s)
    try:
        await run_forever(c)
    finally:
        await close_container(c)


if __name__ == "__main__":
    asyncio.run(main())
