"""Projects (R-015): list/create/detail/patch, environments, services, team. Wizard drafts live in drafts.py."""
from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation

from app.audit import audit
from app.auth.authz import Cap
from app.auth.deps import (
    OrgContext,
    ProjectContext,
    get_conn,
    load_project_context,
    org_context,
    project_access,
)
from app.domain.projects import service as svc
from app.domain.projects.schemas import (
    EnvironmentOut,
    EnvironmentsPut,
    MemberOut,
    MembersPut,
    ProjectCreate,
    ProjectDetail,
    ProjectPatch,
    ProjectSummary,
    ServiceOut,
    ServicesPut,
)
from app.envelope import Envelope, wrap
from app.errors import Conflict, Forbidden, Unprocessable
from app.jobs.triggers import request_graph_sync
from app.pagination import clamp_limit, decode_cursor, encode_cursor

router = APIRouter(tags=["projects"])


@router.get("/projects", response_model=Envelope[list[ProjectSummary]])
async def list_projects(
    request: Request,
    q: str | None = Query(None, max_length=80),
    sort: Literal["name", "updated"] = "name",
    limit: int | None = Query(None, ge=1, le=100),
    cursor: str | None = None,
    org: OrgContext = Depends(org_context),
    conn: AsyncConnection = Depends(get_conn),
):
    """Only projects the caller holds a grant on. 'No projects' and 'no matches' are distinguishable via
    meta.coverage.total_authorized (empty dataset vs. empty search)."""
    n = clamp_limit(limit)
    cur = decode_cursor(cursor)
    where = ["p.organization_id = %s", "p.status = 'active'",
             "EXISTS (SELECT 1 FROM project_memberships m WHERE m.project_id = p.id AND m.user_id = %s)"]
    params: list = [org.organization_id, org.principal.user_id]
    if q:
        where.append("p.name_key LIKE %s")
        params.append("%" + svc.name_key(q).replace("%", r"\%").replace("_", r"\_") + "%")
    total = (await (await conn.execute(
        "SELECT count(*) AS n FROM projects p WHERE p.organization_id=%s AND p.status='active' AND EXISTS"
        " (SELECT 1 FROM project_memberships m WHERE m.project_id=p.id AND m.user_id=%s)",
        (org.organization_id, org.principal.user_id))).fetchone())["n"]
    if sort == "name":
        order, key = "p.name_key, p.id", ("name_key", "id")
        if cur:
            where.append("(p.name_key, p.id) > (%s, %s)")
            params += [cur["name_key"], UUID(cur["id"])]
    else:
        order, key = "p.updated_at DESC, p.id DESC", ("updated_at", "id")
        if cur:
            where.append("(p.updated_at, p.id) < (%s, %s)")
            params += [cur["updated_at"], UUID(cur["id"])]
    rows = await (await conn.execute(
        f"SELECT p.* FROM projects p WHERE {' AND '.join(where)} ORDER BY {order} LIMIT %s", (*params, n + 1)
    )).fetchall()
    more = len(rows) > n
    rows = rows[:n]
    nxt = encode_cursor({key[0]: str(rows[-1][key[0]]), "id": str(rows[-1]["id"])}) if more else None
    return wrap(request, await svc.project_summaries(conn, rows), next_cursor=nxt,
                coverage={"total_authorized": total})


@router.post("/projects", status_code=201, response_model=Envelope[ProjectSummary])
async def create_project(
    body: ProjectCreate,
    request: Request,
    org: OrgContext = Depends(org_context),
    conn: AsyncConnection = Depends(get_conn),
):
    if not org.is_org_admin:
        raise Forbidden("Only organization owners and admins create projects")
    p = await svc.create_project(conn, organization_id=org.organization_id, actor=org.principal.user_id,
                                 body=body, request=request)
    await conn.commit()
    return wrap(request, (await svc.project_summaries(conn, [p]))[0])


@router.get("/projects/{project_id}", response_model=Envelope[ProjectDetail])
async def get_project(
    request: Request,
    ctx: ProjectContext = Depends(project_access(Cap.READ)),
    conn: AsyncConnection = Depends(get_conn),
):
    p = await svc.get_project_row(conn, ctx.org.organization_id, ctx.project_id)
    data = (await svc.project_summaries(conn, [p]))[0]
    data["services"] = [dict(r) for r in await (await conn.execute(
        "SELECT id, name, service_key AS key FROM services WHERE project_id=%s ORDER BY name", (ctx.project_id,))).fetchall()]
    data["capabilities"] = sorted(c.value for c in ctx.caps)
    return wrap(request, data)


@router.patch("/projects/{project_id}", response_model=Envelope[ProjectSummary])
async def patch_project(
    body: ProjectPatch,
    request: Request,
    ctx: ProjectContext = Depends(project_access(Cap.MANAGE_PROJECT)),
    conn: AsyncConnection = Depends(get_conn),
):
    sets, params = [], []
    if body.name is not None:
        sets += ["name=%s", "name_key=%s"]
        params += [body.name.strip(), svc.name_key(body.name)]
    if body.description is not None:
        sets.append("description=%s")
        params.append(body.description)
    if body.owner_user_id is not None:
        ok = await (await conn.execute(
            "SELECT 1 FROM memberships WHERE organization_id=%s AND user_id=%s AND status='active'",
            (ctx.org.organization_id, body.owner_user_id))).fetchone()
        if ok is None:
            raise Unprocessable("Owner must be an active member of the organization")
        sets.append("owner_user_id=%s")
        params.append(body.owner_user_id)
    if not sets:
        raise Unprocessable("Nothing to change")
    try:
        row = await (await conn.execute(
            f"UPDATE projects SET {', '.join(sets)} WHERE id=%s AND version=%s RETURNING *",
            (*params, ctx.project_id, body.version))).fetchone()
    except UniqueViolation:
        await conn.rollback()
        raise Conflict(svc.NAME_TAKEN, code="project_name_taken") from None
    if row is None:
        await conn.rollback()
        raise Conflict("Project was changed by someone else. Reload and retry.", code="version_conflict")
    await audit(conn, organization_id=ctx.org.organization_id, project_id=ctx.project_id, actor=ctx.org.principal.user_id,
                action="project.update", target_type="project", target_id=ctx.project_id, request=request,
                details={"fields": [s.split("=")[0] for s in sets]})
    await conn.commit()
    return wrap(request, (await svc.project_summaries(conn, [row]))[0])


# --- environments / services: versioned arrays of stable IDs (A5) -------------------------------------------------

@router.put("/projects/{project_id}/environments", response_model=Envelope[list[EnvironmentOut]])
async def put_environments(
    body: EnvironmentsPut,
    request: Request,
    ctx: ProjectContext = Depends(project_access(Cap.MANAGE_PROJECT)),
    conn: AsyncConnection = Depends(get_conn),
):
    """Upsert by key; environments absent from the list are removed only if nothing references them yet.
    Existing IDs are stable across renames so historical manifests are never invalidated."""
    rows = svc.env_rows(body.environments)
    keys = [k for _, k in rows]
    for name, key in rows:
        await conn.execute(
            "INSERT INTO environments(organization_id,project_id,name,environment_key) VALUES (%s,%s,%s,%s)"
            " ON CONFLICT (project_id, environment_key) DO UPDATE SET name = EXCLUDED.name",
            (ctx.org.organization_id, ctx.project_id, name, key))
    removed = await (await conn.execute(
        "SELECT id, environment_key FROM environments WHERE project_id=%s AND NOT (environment_key = ANY(%s))",
        (ctx.project_id, keys))).fetchall()
    for r in removed:
        used = await (await conn.execute(
            "SELECT 1 FROM log_scope_entries WHERE environment_id=%s LIMIT 1", (r["id"],))).fetchone()
        if used:
            await conn.rollback()
            raise Conflict(f"Environment '{r['environment_key']}' has log scope entries; remove them first",
                           code="environment_in_use")
        await conn.execute("DELETE FROM environments WHERE id=%s", (r["id"],))
    await audit(conn, organization_id=ctx.org.organization_id, project_id=ctx.project_id, actor=ctx.org.principal.user_id,
                action="project.environments.put", target_type="project", target_id=ctx.project_id, request=request,
                details={"keys": keys})
    await request_graph_sync(conn, organization_id=ctx.org.organization_id, project_id=ctx.project_id)
    await conn.commit()
    out = await (await conn.execute(
        "SELECT id, name, environment_key AS key FROM environments WHERE project_id=%s ORDER BY created_at, name",
        (ctx.project_id,))).fetchall()
    return wrap(request, out)


@router.get("/projects/{project_id}/environments", response_model=Envelope[list[EnvironmentOut]])
async def get_environments(request: Request, ctx: ProjectContext = Depends(project_access(Cap.READ)),
                           conn: AsyncConnection = Depends(get_conn)):
    out = await (await conn.execute(
        "SELECT id, name, environment_key AS key FROM environments WHERE project_id=%s ORDER BY created_at, name",
        (ctx.project_id,))).fetchall()
    return wrap(request, out)


@router.put("/projects/{project_id}/services", response_model=Envelope[list[ServiceOut]])
async def put_services(
    body: ServicesPut,
    request: Request,
    ctx: ProjectContext = Depends(project_access(Cap.MANAGE_PROJECT)),
    conn: AsyncConnection = Depends(get_conn),
):
    rows = svc.svc_rows(body.services)
    keys = [k for _, k in rows]
    for name, key in rows:
        await conn.execute(
            "INSERT INTO services(organization_id,project_id,name,service_key) VALUES (%s,%s,%s,%s)"
            " ON CONFLICT (project_id, service_key) DO UPDATE SET name = EXCLUDED.name",
            (ctx.org.organization_id, ctx.project_id, name, key))
    await conn.execute("DELETE FROM services WHERE project_id=%s AND NOT (service_key = ANY(%s))",
                       (ctx.project_id, keys))
    await audit(conn, organization_id=ctx.org.organization_id, project_id=ctx.project_id, actor=ctx.org.principal.user_id,
                action="project.services.put", target_type="project", target_id=ctx.project_id, request=request,
                details={"keys": keys})
    await request_graph_sync(conn, organization_id=ctx.org.organization_id, project_id=ctx.project_id)
    await conn.commit()
    out = await (await conn.execute(
        "SELECT id, name, service_key AS key FROM services WHERE project_id=%s ORDER BY name", (ctx.project_id,))).fetchall()
    return wrap(request, out)


@router.get("/projects/{project_id}/services", response_model=Envelope[list[ServiceOut]])
async def get_services(request: Request, ctx: ProjectContext = Depends(project_access(Cap.READ)),
                       conn: AsyncConnection = Depends(get_conn)):
    out = await (await conn.execute(
        "SELECT id, name, service_key AS key FROM services WHERE project_id=%s ORDER BY name", (ctx.project_id,))).fetchall()
    return wrap(request, out)


# --- team (project membership; NOT organization membership) -------------------------------------------------------

async def _members_ctx(
    project_id: UUID,
    org: OrgContext = Depends(org_context),
    conn: AsyncConnection = Depends(get_conn),
) -> OrgContext:
    """Org owners/admins may administer access to any project in their org (access administration, audited), even
    without a data grant. Everyone else needs the project's manage_members capability."""
    if org.is_org_admin:
        await svc.get_project_row(conn, org.organization_id, project_id)
        return org
    ctx = await load_project_context(conn, org, project_id)
    ctx.require(Cap.MANAGE_MEMBERS)
    return org


@router.get("/projects/{project_id}/members", response_model=Envelope[list[MemberOut]])
async def get_members(
    project_id: UUID,
    request: Request,
    org: OrgContext = Depends(org_context),
    conn: AsyncConnection = Depends(get_conn),
):
    """Any project reader sees the team; org admins see it too for administration."""
    if not org.is_org_admin:
        await load_project_context(conn, org, project_id)
    else:
        await svc.get_project_row(conn, org.organization_id, project_id)
    rows = await (await conn.execute(
        "SELECT pm.user_id, pm.role, pm.environment_ids, m.email, m.display_name, m.status AS member_status"
        " FROM project_memberships pm JOIN memberships m ON m.user_id = pm.user_id AND m.organization_id = pm.organization_id"
        " WHERE pm.project_id=%s ORDER BY lower(m.email), pm.role", (project_id,))).fetchall()
    return wrap(request, rows)


@router.put("/projects/{project_id}/members", response_model=Envelope[list[MemberOut]])
async def put_members(
    project_id: UUID,
    body: MembersPut,
    request: Request,
    org: OrgContext = Depends(_members_ctx),
    conn: AsyncConnection = Depends(get_conn),
):
    """Replaces the grant list. Guards: every user must be an active org member; environment ids must belong to
    the project; at least one admin must remain so the project is never orphaned."""
    if not any(m.role == "admin" for m in body.members):
        raise Unprocessable("A project needs at least one admin")
    uids = list({m.user_id for m in body.members})
    active = {r["user_id"] for r in await (await conn.execute(
        "SELECT user_id FROM memberships WHERE organization_id=%s AND status='active' AND user_id = ANY(%s)",
        (org.organization_id, uids))).fetchall()}
    if set(uids) - active:
        raise Unprocessable("All members must be active members of the organization")
    env_ids = {r["id"] for r in await (await conn.execute(
        "SELECT id FROM environments WHERE project_id=%s", (project_id,))).fetchall()}
    for m in body.members:
        if m.environment_ids is not None and (not m.environment_ids or set(m.environment_ids) - env_ids):
            raise Unprocessable("environment_ids must be a non-empty subset of the project's environments")
    await conn.execute("DELETE FROM project_memberships WHERE project_id=%s", (project_id,))
    for m in body.members:
        await conn.execute(
            "INSERT INTO project_memberships(organization_id,project_id,user_id,role,environment_ids)"
            " VALUES (%s,%s,%s,%s,%s) ON CONFLICT (project_id,user_id,role) DO UPDATE SET environment_ids=EXCLUDED.environment_ids",
            (org.organization_id, project_id, m.user_id, m.role, m.environment_ids))
    await conn.execute("UPDATE memberships SET authz_version = authz_version + 1 WHERE organization_id=%s AND user_id = ANY(%s)",
                       (org.organization_id, uids))
    await audit(conn, organization_id=org.organization_id, project_id=project_id, actor=org.principal.user_id,
                action="project.members.put", target_type="project", target_id=project_id, request=request,
                details={"count": len(body.members), "via": "org_admin" if org.is_org_admin else "project_admin"})
    await conn.commit()
    return await get_members(project_id, request, org, conn)
