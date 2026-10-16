from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.health import health_router
from app.api.v1.router import api_router
from app.config import Settings, get_settings
from app.container import Container
from app.errors import install_error_handlers
from app.middleware import RequestContextMiddleware

log = logging.getLogger("trace")


async def build_container(settings: Settings) -> Container:
    """Wire production adapters from configuration. Anything unconfigured stays None and the dependent endpoints
    answer 503 dependency_unavailable rather than pretending to work."""
    from app.auth.jwt import SupabaseTokenVerifier
    from app.storage.postgres.pool import open_pool
    from app.storage.secrets import Vault

    c = Container(settings=settings)
    if settings.database_url:
        c.pool = await open_pool(settings.database_url)
    if settings.supabase_jwks_url:
        issuer = f"{settings.supabase_url.rstrip('/')}/auth/v1" if settings.supabase_url else None
        c.verifier = SupabaseTokenVerifier(
            jwks_url=settings.supabase_jwks_url, issuer=issuer, audience=settings.supabase_jwt_audience
        )
    if settings.trace_secret_key:
        c.vault = Vault.from_settings(settings)
    return c


async def close_container(c: Container) -> None:
    if c.pool is not None:
        await c.pool.close()


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    """`container` lets tests inject doubles; production always builds from settings in lifespan."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owned = container is None
        app.state.container = container or await build_container(settings)
        log.info("startup", extra={"capabilities": settings.capability_report(), "env": settings.trace_env})
        try:
            yield
        finally:
            if owned:
                await close_container(app.state.container)

    app = FastAPI(
        title="TRACE API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.trace_env != "production" else None,
        redoc_url=None,
    )
    if container is not None:
        app.state.container = container  # injected (tests); lifespan keeps it and does not close it
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=False,  # bearer tokens in the Authorization header, never cookies
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["authorization", "content-type", "idempotency-key", "x-organization-id", "x-request-id",
                       "last-event-id", "x-user-timezone"],
        expose_headers=["x-request-id"],
    )
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    app.include_router(health_router)
    app.include_router(api_router, prefix="/api/v1")
    return app


def app_factory() -> FastAPI:  # uvicorn --factory app.main:app_factory
    return create_app()
