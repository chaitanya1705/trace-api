from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

from fastapi import Request
from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation

from app.audit import audit
from app.domain.projects.readiness import compute_readiness
from app.domain.projects.schemas import EnvironmentIn, ProjectCreate, ServiceIn, slug
from app.errors import Conflict, NotFound, Unprocessable

NAME_TAKEN = "A project with this name already exists in your organization"


def name_key(name: str) -> str:
    return " ".join(name.strip().lower().split())


def env_rows(envs: list[EnvironmentIn]) -> list[tuple[str, str]]:
    rows, seen = [], set()
    for e in envs:
        key = e.key or slug(e.name)
        if key in seen:
            raise Unprocessable(f"Duplicate environment key: {key}")
        seen.add(key)
        rows.append((e.name.strip(), key))
    return rows


def svc_rows(svcs: list[ServiceIn]) -> list[tuple[str, str]]:
    rows, seen = [], set()
    for s in svcs:
        key = s.key or re.sub(r"[^a-z0-9_.-]+", "-", s.name.strip().lower()).strip("-")[:64]
        if not key:
            raise Unprocessable("Service needs a name or key")
        if key in seen:
            raise Unprocessable(f"Duplicate service key: {key}")
        seen.add(key)
        rows.append((s.name.strip(), key))
    return rows


async def create_project(
    conn: AsyncConnection,
    *,
    organization_id: UUID,
    actor: UUID,
    body: ProjectCreate,
    request: Request | None,
) -> dict[str, Any]:
    envs = env_rows(body.environments)
    svcs = svc_rows(body.services)
    owner = body.owner_user_id or actor
    ok = await (
        await conn.execute(
            "SELECT 1 FROM memberships WHERE organization_id=%s AND user_id=%s AND status='active'",
            (organization_id, owner),
        )
    ).fetchone()
    if ok is None:
        raise Unprocessable("Owner must be an active member of the organization")
    try:
        p = await (
            await conn.execute(
                "INSERT INTO projects(organization_id,name,name_key,description,owner_user_id)"
                " VALUES (%s,%s,%s,%s,%s) RETURNING *",
                (organization_id, body.name.strip(), name_key(body.name), body.description, owner),
            )
        ).fetchone()
    except UniqueViolation:
        await conn.rollback()
        raise Conflict(NAME_TAKEN, code="project_name_taken") from None
    for n, k in envs:
        await conn.execute(
            "INSERT INTO environments(organization_id,project_id,name,environment_key) VALUES (%s,%s,%s,%s)",
            (organization_id, p["id"], n, k),
        )
    for n, k in svcs:
        await conn.execute(
            "INSERT INTO services(organization_id,project_id,name,service_key) VALUES (%s,%s,%s,%s)",
            (organization_id, p["id"], n, k),
        )
    # Creator administers and may investigate; separation of duty: no approver grant is implied.
    for role in ("admin", "investigator"):
        await conn.execute(
            "INSERT INTO project_memberships(organization_id,project_id,user_id,role) VALUES (%s,%s,%s,%s)"
            " ON CONFLICT DO NOTHING",
            (organization_id, p["id"], actor, role),
        )
    if owner != actor:
        await conn.execute(
            "INSERT INTO project_memberships(organization_id,project_id,user_id,role) VALUES (%s,%s,%s,'admin')"
            " ON CONFLICT DO NOTHING",
            (organization_id, p["id"], owner),
        )
    await audit(conn, organization_id=organization_id, project_id=p["id"], actor=actor, action="project.create",
                target_type="project", target_id=p["id"], request=request, details={"name": body.name})
    await enqueue_scope_projection(conn, p, [k for _, k in envs], [k for _, k in svcs])
    return p


async def enqueue_scope_projection(conn: AsyncConnection, project: dict, env_keys: list[str], svc_keys: list[str]) -> None:
    """Mirror non-secret scope to the graph via the outbox in the same transaction (A2). The worker applies it
    idempotently; the operation key is deterministic per project version."""
    await conn.execute(
        "INSERT INTO outbox(organization_id,project_id,kind,operation_key,payload)"
        " VALUES (%s,%s,'project_scope',%s,%s::jsonb) ON CONFLICT (operation_key) DO NOTHING",
        (project["organization_id"], project["id"], f"project_scope:{project['id']}:v{project['version']}",
         json.dumps({"project_id": str(project["id"]), "name": project["name"],
                     "environments": env_keys, "services": svc_keys})),
    )


async def _graph_status(conn: AsyncConnection, project_ids: list[UUID]) -> dict[UUID, str]:
    """Reads graph_builds once T-007 creates it; until then no project has a build."""
    exists = await (await conn.execute("SELECT to_regclass('public.graph_builds') AS t")).fetchone()
    if not exists or not exists["t"]:
        return {}
    rows = await (await conn.execute(
        "SELECT DISTINCT ON (project_id) project_id, status FROM graph_builds WHERE project_id = ANY(%s)"
        " ORDER BY project_id, created_at DESC", (project_ids,))).fetchall()
    return {r["project_id"]: r["status"] for r in rows}


async def project_summaries(conn: AsyncConnection, project_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not project_rows:
        return []
    ids = [p["id"] for p in project_rows]
    envs = await (await conn.execute(
        "SELECT id, project_id, name, environment_key AS key FROM environments"
        " WHERE project_id = ANY(%s) ORDER BY created_at, name", (ids,))).fetchall()
    conns = await (await conn.execute(
        "SELECT id, project_id, provider, kind, status, last_verified_at FROM connections"
        " WHERE project_id = ANY(%s)", (ids,))).fetchall()
    scope = await (await conn.execute(
        "SELECT project_id, count(*) AS total, count(*) FILTER (WHERE log_availability <> 'available') AS gaps"
        " FROM log_scope_entries WHERE project_id = ANY(%s) GROUP BY project_id", (ids,))).fetchall()
    repos = await (await conn.execute(
        "SELECT project_id, count(*) AS n FROM repository_bindings WHERE project_id = ANY(%s) GROUP BY project_id",
        (ids,))).fetchall()
    graph = await _graph_status(conn, ids)
    out = []
    for p in project_rows:
        pid = p["id"]
        sc = next((s for s in scope if s["project_id"] == pid), None)
        pc = [c for c in conns if c["project_id"] == pid]
        pe = [e for e in envs if e["project_id"] == pid]
        out.append({
            "id": pid, "name": p["name"], "description": p["description"], "owner_user_id": p["owner_user_id"],
            "environments": [{"id": e["id"], "name": e["name"], "key": e["key"]} for e in pe],
            "connections": [{k: c[k] for k in ("id", "provider", "kind", "status", "last_verified_at")} for c in pc],
            "readiness": [r.model_dump() for r in compute_readiness(
                has_environments=bool(pe),
                connections=[(c["kind"], c["status"]) for c in pc],
                log_scope_total=sc["total"] if sc else 0, log_scope_gaps=sc["gaps"] if sc else 0,
                repo_count=next((r["n"] for r in repos if r["project_id"] == pid), 0),
                graph_status=graph.get(pid))],
            "updated_at": p["updated_at"], "version": p["version"],
        })
    return out


async def get_project_row(conn: AsyncConnection, org_id: UUID, project_id: UUID) -> dict[str, Any]:
    p = await (await conn.execute(
        "SELECT * FROM projects WHERE id=%s AND organization_id=%s AND status='active'", (project_id, org_id))).fetchone()
    if p is None:
        raise NotFound("Project not found")
    return p
