"""Connections (R-016): lifecycle, discovery, jobs. Only GitHub and Vercel (plus generic) in this build."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from psycopg import AsyncConnection
from pydantic import BaseModel, Field

from app.audit import audit
from app.auth.authz import Cap
from app.auth.deps import (
    OrgContext,
    ProjectContext,
    get_conn,
    get_container,
    load_project_context,
    org_context,
    project_access,
)
from app.connectors.base import ProviderError
from app.container import Container
from app.domain.connections import service as svc
from app.envelope import wrap
from app.errors import Conflict, DependencyUnavailable, Forbidden, NotFound, Unprocessable
from app.jobs import queue
from app.jobs.triggers import request_graph_sync

router = APIRouter(tags=["connections"])


class ConnectionCreate(BaseModel):
    provider: str
    auth_method: str
    config: dict[str, Any] = Field(default_factory=dict)
    secret: str | None = Field(None, min_length=8, max_length=4096, repr=False)
    secret_expires_at: datetime | None = None


class RotateIn(BaseModel):
    secret: str = Field(min_length=8, max_length=4096, repr=False)
    secret_expires_at: datetime | None = None


_CONFIG_KEYS = {"github": {"installation_id"}, "vercel": {"team_id"}, "generic": set(), "postgres": set()}


def clean_config(provider: str, config: dict) -> dict:
    """Only known, non-secret provider ids are accepted; anything else is rejected, not stored."""
    extra = set(config) - _CONFIG_KEYS.get(provider, set())
    if extra:
        raise Unprocessable("Unsupported config keys: " + ", ".join(sorted(extra)))
    out = {k: (str(v) if k == "installation_id" else v) for k, v in config.items() if v not in (None, "")}
    if "installation_id" in out and not out["installation_id"].isdigit():
        raise Unprocessable("installation_id must be numeric")
    if "team_id" in out and not str(out["team_id"]).startswith("team_"):
        raise Unprocessable("team_id looks like team_xxxxxxxx (Vercel Team Settings > General)")
    return out


def provider_http_error(e: ProviderError):
    if e.retryable:
        return DependencyUnavailable(e.message, code=e.code)
    return Conflict(e.message, code=e.code)


async def authorize_connection(conn: AsyncConnection, org: OrgContext, row: dict, cap: Cap) -> None:
    """Project-owned: project capability. Draft-owned: only the draft's creator (or an org admin)."""
    if row["project_id"]:
        (await load_project_context(conn, org, row["project_id"])).require(cap)
        return
    d = await (await conn.execute("SELECT created_by FROM project_drafts WHERE id=%s", (row["draft_id"],))).fetchone()
    if d is None or (d["created_by"] != org.principal.user_id and not org.is_org_admin):
        raise NotFound("Connection not found")


async def load_authorized(conn: AsyncConnection, org: OrgContext, connection_id: UUID, cap: Cap) -> dict:
    row = await svc.get_connection_row(conn, org.organization_id, connection_id)
    try:
        await authorize_connection(conn, org, row, cap)
    except Forbidden:
        raise
    return row


@router.get("/providers")
async def providers(request: Request, _: OrgContext = Depends(org_context), c: Container = Depends(get_container)):
    return wrap(request, svc.providers_catalog(c.settings))


@router.get("/projects/{project_id}/connections")
async def list_connections(request: Request, ctx: ProjectContext = Depends(project_access(Cap.READ)),
                           conn: AsyncConnection = Depends(get_conn)):
    rows = await (await conn.execute("SELECT * FROM connections WHERE project_id=%s ORDER BY kind", (ctx.project_id,))).fetchall()
    return wrap(request, [svc.public(r) for r in rows])


@router.post("/projects/{project_id}/connections", status_code=202)
async def create_project_connection(
    body: ConnectionCreate, request: Request,
    ctx: ProjectContext = Depends(project_access(Cap.MANAGE_CONNECTIONS)),
    conn: AsyncConnection = Depends(get_conn), c: Container = Depends(get_container),
):
    row, ver = await svc.create_connection(
        conn, c.vault, organization_id=ctx.org.organization_id, actor=ctx.org.principal.user_id, project_id=ctx.project_id,
        draft_id=None, provider=body.provider, auth_method=body.auth_method,
        config=clean_config(body.provider, body.config), secret=body.secret, secret_expires_at=body.secret_expires_at,
        request=request)
    job = await svc.enqueue_verify(conn, row, ctx.org.principal.user_id, ver)
    await request_graph_sync(conn, organization_id=ctx.org.organization_id, project_id=ctx.project_id)
    await conn.commit()
    return wrap(request, svc.accepted(job, row["id"]))


@router.get("/connections/{connection_id}")
async def get_connection(connection_id: UUID, request: Request, org: OrgContext = Depends(org_context),
                         conn: AsyncConnection = Depends(get_conn)):
    row = await load_authorized(conn, org, connection_id, Cap.READ)
    cred = await (await conn.execute("SELECT version, expires_at FROM credential_versions WHERE connection_id=%s AND status='active'",
                                     (connection_id,))).fetchone()
    return wrap(request, svc.public(row, {"hint": f"credential v{cred['version']}"} if cred else None))


@router.get("/connections/{connection_id}/checks")
async def connection_checks(connection_id: UUID, request: Request, org: OrgContext = Depends(org_context),
                            conn: AsyncConnection = Depends(get_conn)):
    await load_authorized(conn, org, connection_id, Cap.READ)
    rows = await (await conn.execute(
        "SELECT id, outcome, stages, started_at, finished_at FROM connection_checks WHERE connection_id=%s"
        " ORDER BY started_at DESC LIMIT 20", (connection_id,))).fetchall()
    return wrap(request, rows)


@router.post("/connections/{connection_id}/verify", status_code=202)
async def reverify(connection_id: UUID, request: Request, org: OrgContext = Depends(org_context),
                   conn: AsyncConnection = Depends(get_conn)):
    row = await load_authorized(conn, org, connection_id, Cap.MANAGE_CONNECTIONS)
    if row["status"] == "disconnected":
        raise Conflict("Connection is disconnected", code="connection_disconnected")
    # Fresh key per request so an explicit re-verify is never swallowed by idempotency of an earlier one.
    job = await queue.enqueue(conn, organization_id=row["organization_id"], project_id=row["project_id"], kind="connection.verify",
                              payload={"connection_id": str(connection_id), "credential_version": None},
                              requested_by=org.principal.user_id, priority=queue.PRIORITY_HUMAN,
                              resource_type="connection", resource_id=connection_id)
    await conn.commit()
    return wrap(request, svc.accepted(job, connection_id))


@router.post("/connections/{connection_id}/rotate", status_code=202)
async def rotate(connection_id: UUID, body: RotateIn, request: Request, org: OrgContext = Depends(org_context),
                 conn: AsyncConnection = Depends(get_conn), c: Container = Depends(get_container)):
    """The replacement is verified *before* activation; if it fails the working credential stays in force."""
    row = await load_authorized(conn, org, connection_id, Cap.MANAGE_CONNECTIONS)
    if row["auth_method"] not in svc.SECRET_METHODS:
        raise Unprocessable("This connection does not hold a rotatable secret")
    if row["status"] == "disconnected":
        raise Conflict("Connection is disconnected", code="connection_disconnected")
    ver = await svc.store_secret(conn, c.vault, organization_id=row["organization_id"], connection_id=connection_id,
                                 secret=body.secret, kind=row["auth_method"], actor=org.principal.user_id,
                                 expires_at=body.secret_expires_at)
    await audit(conn, organization_id=row["organization_id"], project_id=row["project_id"], actor=org.principal.user_id,
                action="connection.rotate", target_type="connection", target_id=connection_id, request=request,
                details={"pending_version": ver})
    job = await svc.enqueue_verify(conn, row, org.principal.user_id, ver)
    await conn.commit()
    return wrap(request, svc.accepted(job, connection_id))


@router.post("/connections/{connection_id}/disconnect")
async def disconnect(connection_id: UUID, request: Request, org: OrgContext = Depends(org_context),
                     conn: AsyncConnection = Depends(get_conn)):
    """Halts future jobs and revokes local access immediately. Retained evidence/citations are untouched."""
    row = await load_authorized(conn, org, connection_id, Cap.MANAGE_CONNECTIONS)
    await conn.execute("UPDATE connections SET status='disconnected', last_error=NULL WHERE id=%s", (connection_id,))
    await conn.execute("UPDATE credential_versions SET status='revoked', ciphertext='\\x'::bytea WHERE connection_id=%s", (connection_id,))
    await conn.execute("DELETE FROM connection_secrets WHERE connection_id=%s", (connection_id,))  # drain / ingest signing secrets
    cancelled = await queue.cancel_for_resource(conn, "connection", connection_id)
    await audit(conn, organization_id=row["organization_id"], project_id=row["project_id"], actor=org.principal.user_id,
                action="connection.disconnect", target_type="connection", target_id=connection_id, request=request,
                details={"jobs_cancelled": cancelled})
    await conn.commit()
    return wrap(request, {"id": connection_id, "status": "disconnected", "jobs_cancelled": cancelled})


@router.get("/connections/{connection_id}/resources")
async def resources(
    connection_id: UUID, request: Request, q: str | None = Query(None, max_length=80), include_archived: bool = False,
    org: OrgContext = Depends(org_context), conn: AsyncConnection = Depends(get_conn), c: Container = Depends(get_container),
):
    """Discovery with credentials kept server-side. GitHub: repositories. Vercel: projects. Bounded (5 pages)."""
    row = await load_authorized(conn, org, connection_id, Cap.READ)
    try:
        items, total, truncated = await svc.discover(c, conn, row, include_archived=include_archived)
    except ProviderError as e:
        raise provider_http_error(e) from None
    if q:
        n = q.strip().lower()
        items = [i for i in items if n in (i.get("full_name") or i.get("name") or "").lower()]
    return wrap(request, items, coverage={"total_visible": total, "truncated": truncated, "filtered_archived": not include_archived})


# --- jobs ---------------------------------------------------------------------------------------------------------

def job_public(j: dict) -> dict:
    keys = ("id", "kind", "status", "stage", "progress", "warnings", "result_ref", "attempt", "max_attempts", "resource_type",
            "resource_id", "created_at", "started_at", "finished_at")
    return {**{k: j[k] for k in keys}, "error": j["error"]}  # error is {code,message,retryable}; never secrets


async def _job_for(conn: AsyncConnection, org: OrgContext, job_id: UUID) -> dict:
    j = await (await conn.execute("SELECT * FROM jobs WHERE id=%s AND organization_id=%s", (job_id, org.organization_id))).fetchone()
    if j is None:
        raise NotFound("Job not found")
    if j["requested_by"] != org.principal.user_id:
        if not j["project_id"]:
            raise NotFound("Job not found")
        ctx = await load_project_context(conn, org, j["project_id"])
        if not ctx.caps & {Cap.MANAGE_CONNECTIONS, Cap.INDEX, Cap.INVESTIGATE}:
            raise NotFound("Job not found")
    return j


@router.get("/jobs/{job_id}")
async def get_job(job_id: UUID, request: Request, org: OrgContext = Depends(org_context), conn: AsyncConnection = Depends(get_conn)):
    return wrap(request, job_public(await _job_for(conn, org, job_id)))


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: UUID, request: Request, org: OrgContext = Depends(org_context), conn: AsyncConnection = Depends(get_conn)):
    await _job_for(conn, org, job_id)
    status = await queue.request_cancel(conn, job_id, org.organization_id)
    if status is None:
        raise Conflict("Job already finished", code="job_terminal")
    return wrap(request, {"id": job_id, "status": status})
