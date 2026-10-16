"""Project scope: log scope, repository bindings, service/path mappings (Projects > Log scope / Code tabs)."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from psycopg import AsyncConnection
from pydantic import BaseModel, Field

from app.audit import audit
from app.auth.authz import Cap
from app.auth.deps import ProjectContext, get_conn, get_container, project_access
from app.container import Container
from app.domain.projects import binding as b
from app.envelope import wrap
from app.errors import NotFound, Unprocessable

router = APIRouter(tags=["project-scope"])


class LogScopePut(BaseModel):
    environments: dict[str, list[b.ScopeResource]] = Field(max_length=8)


class ReposPut(BaseModel):
    repositories: list[b.RepoSelection] = Field(max_length=50)


class MappingsPut(BaseModel):
    services: list[b.ServiceMap] = Field(default_factory=list, max_length=50)
    paths: list[b.PathMap] = Field(default_factory=list, max_length=100)


@router.get("/projects/{project_id}/log-scope")
async def get_log_scope(request: Request, ctx: ProjectContext = Depends(project_access(Cap.READ)),
                        conn: AsyncConnection = Depends(get_conn)):
    rows = await (await conn.execute(
        "SELECT l.id, e.environment_key, l.provider_resource_id, l.resource_type, l.name, l.log_availability, l.availability_note,"
        " l.last_event_at, s.service_key FROM log_scope_entries l JOIN environments e ON e.id=l.environment_id"
        " LEFT JOIN services s ON s.id=l.service_id WHERE l.project_id=%s ORDER BY e.environment_key, l.name", (ctx.project_id,))).fetchall()
    gaps = sum(1 for r in rows if r["log_availability"] != "available")
    return wrap(request, rows, coverage={"total": len(rows), "without_usable_logs": gaps})


@router.put("/projects/{project_id}/log-scope")
async def put_log_scope(body: LogScopePut, request: Request, ctx: ProjectContext = Depends(project_access(Cap.MANAGE_PROJECT)),
                        conn: AsyncConnection = Depends(get_conn), c: Container = Depends(get_container)):
    n = await b.apply_log_scope(c, conn, organization_id=ctx.org.organization_id, project_id=ctx.project_id, draft_id=None,
                                env_map=body.environments)
    await audit(conn, organization_id=ctx.org.organization_id, project_id=ctx.project_id, actor=ctx.org.principal.user_id,
                action="project.log_scope.put", target_type="project", target_id=ctx.project_id, request=request, details={"entries": n})
    await conn.commit()
    return await get_log_scope(request, ctx, conn)


@router.get("/projects/{project_id}/repositories")
async def get_repositories(request: Request, ctx: ProjectContext = Depends(project_access(Cap.READ)),
                           conn: AsyncConnection = Depends(get_conn)):
    rows = await (await conn.execute(
        "SELECT id, connection_id, provider_repository_id, full_name, default_branch, selected_ref, visibility, archived"
        " FROM repository_bindings WHERE project_id=%s ORDER BY full_name", (ctx.project_id,))).fetchall()
    return wrap(request, rows)


@router.put("/projects/{project_id}/repositories")
async def put_repositories(body: ReposPut, request: Request, ctx: ProjectContext = Depends(project_access(Cap.MANAGE_PROJECT)),
                           conn: AsyncConnection = Depends(get_conn), c: Container = Depends(get_container)):
    rows = await b.apply_repositories(c, conn, organization_id=ctx.org.organization_id, project_id=ctx.project_id, draft_id=None,
                                      repos=body.repositories)
    keep = [r["provider_repository_id"] for r in rows]
    await conn.execute("DELETE FROM repository_bindings WHERE project_id=%s AND NOT (provider_repository_id = ANY(%s))",
                       (ctx.project_id, keep))
    await audit(conn, organization_id=ctx.org.organization_id, project_id=ctx.project_id, actor=ctx.org.principal.user_id,
                action="project.repositories.put", target_type="project", target_id=ctx.project_id, request=request,
                details={"repositories": [r["full_name"] for r in rows]})
    await conn.commit()
    return await get_repositories(request, ctx, conn)


async def _binding(conn: AsyncConnection, project_id: UUID, binding_id: UUID) -> dict:
    r = await (await conn.execute("SELECT * FROM repository_bindings WHERE id=%s AND project_id=%s", (binding_id, project_id))).fetchone()
    if r is None:
        raise NotFound("Repository not found")
    return r


@router.get("/projects/{project_id}/repositories/{binding_id}/mappings")
async def get_mappings(binding_id: UUID, request: Request, ctx: ProjectContext = Depends(project_access(Cap.READ)),
                       conn: AsyncConnection = Depends(get_conn)):
    await _binding(conn, ctx.project_id, binding_id)
    svc = await (await conn.execute(
        "SELECT s.service_key, m.path_prefix FROM repository_service_mappings m JOIN services s ON s.id=m.service_id"
        " WHERE m.repository_binding_id=%s ORDER BY s.service_key, m.path_prefix", (binding_id,))).fetchall()
    paths = await (await conn.execute("SELECT runtime_prefix, repo_prefix FROM path_mappings WHERE repository_binding_id=%s ORDER BY runtime_prefix",
                                      (binding_id,))).fetchall()
    return wrap(request, {"services": svc, "paths": paths})


@router.put("/projects/{project_id}/repositories/{binding_id}/mappings")
async def put_mappings(binding_id: UUID, body: MappingsPut, request: Request,
                       ctx: ProjectContext = Depends(project_access(Cap.MANAGE_PROJECT)), conn: AsyncConnection = Depends(get_conn)):
    """One repo can map to many services and a service to many repos/paths; never inferred from names."""
    await _binding(conn, ctx.project_id, binding_id)
    sv = {r["service_key"]: r["id"] for r in await (await conn.execute(
        "SELECT id, service_key FROM services WHERE project_id=%s", (ctx.project_id,))).fetchall()}
    for s in body.services:
        if s.service_key not in sv:
            raise Unprocessable(f"Unknown service: {s.service_key}")
        if ".." in s.path_prefix.split("/") or s.path_prefix.startswith("/"):
            raise Unprocessable("path_prefix must be a relative repository path")
    for p in body.paths:
        if ".." in p.repo_prefix.split("/") or p.repo_prefix.startswith("/"):
            raise Unprocessable("repo_prefix must be a relative repository path")
    await conn.execute("DELETE FROM repository_service_mappings WHERE repository_binding_id=%s", (binding_id,))
    await conn.execute("DELETE FROM path_mappings WHERE repository_binding_id=%s", (binding_id,))
    for s in body.services:
        await conn.execute("INSERT INTO repository_service_mappings(organization_id,project_id,repository_binding_id,service_id,path_prefix)"
                           " VALUES (%s,%s,%s,%s,%s)", (ctx.org.organization_id, ctx.project_id, binding_id, sv[s.service_key], s.path_prefix))
    for p in body.paths:
        await conn.execute("INSERT INTO path_mappings(organization_id,project_id,repository_binding_id,runtime_prefix,repo_prefix)"
                           " VALUES (%s,%s,%s,%s,%s)", (ctx.org.organization_id, ctx.project_id, binding_id, p.runtime_prefix, p.repo_prefix))
    await audit(conn, organization_id=ctx.org.organization_id, project_id=ctx.project_id, actor=ctx.org.principal.user_id,
                action="project.mappings.put", target_type="repository", target_id=binding_id, request=request)
    await conn.commit()
    return await get_mappings(binding_id, request, ctx, conn)
