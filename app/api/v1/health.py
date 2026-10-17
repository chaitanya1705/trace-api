"""Liveness/readiness (A13). Readiness detail is redacted: component status words only."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Response

from app.auth.deps import get_container
from app.container import Container

health_router = APIRouter(tags=["health"])


@health_router.get("/health/live")
async def live() -> dict:
    return {"status": "ok"}


async def _check_db(c: Container) -> str:
    if c.pool is None:
        return "not_configured"
    try:
        async with c.pool.connection(timeout=3) as conn:
            await conn.execute("SELECT 1")
        return "ok"
    except Exception:  # noqa: BLE001 - detail intentionally dropped
        return "unavailable"


async def _check_neo4j(c: Container) -> str:
    driver = c.extras.get("neo4j")
    if driver is None:
        return "not_configured"
    try:
        await asyncio.wait_for(driver.verify_connectivity(), timeout=3)
        return "ok"
    except Exception:  # noqa: BLE001
        return "unavailable"


@health_router.get("/health/ready")
async def ready(response: Response, c: Container = Depends(get_container)) -> dict:
    s = c.settings
    parts = {
        "database": await _check_db(c),
        "graph": await _check_neo4j(c),
        "auth": "ok" if (c.verifier or s.auth_mode == "off") else "not_configured",
        "vault": "ok" if c.vault else "not_configured",
        "model": "configured" if s.openai_api_key else "not_configured",
        "access": "open (no login: anyone who can reach this API has full access)" if s.auth_mode == "off" else "authenticated",
    }
    required = ["database", "auth", "vault"] + (["graph"] if s.trace_env == "production" else [])
    ok = all(parts[k] == "ok" for k in required)
    if not ok:
        response.status_code = 503
    return {"status": "ready" if ok else "not_ready", "components": parts}
