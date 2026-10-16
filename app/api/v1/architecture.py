"""Projects > Architecture: System map (from the project's configured structure) and Database (schema metadata snapshot).
Both are fed from Postgres for now and move to the graph later; every response says where it came from and how fresh it is."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from psycopg import AsyncConnection

from app.auth.authz import Cap
from app.auth.deps import ProjectContext, get_conn, project_access
from app.envelope import wrap
from app.errors import Conflict
from app.jobs import queue

router = APIRouter(tags=["architecture"])


@router.get("/projects/{project_id}/schema")
async def get_schema(request: Request, ctx: ProjectContext = Depends(project_access(Cap.READ)), conn: AsyncConnection = Depends(get_conn)):
    """Latest schema snapshot of the project's database. `connected:false` / `captured:false` say what is missing; no tables are invented."""
    dbc = await (await conn.execute("SELECT id, status, config, last_error FROM connections WHERE project_id=%s AND kind='database' AND status<>'disconnected'",
                                    (ctx.project_id,))).fetchone()
    snap = None
    if dbc:
        snap = await (await conn.execute(
            "SELECT id, captured_at, database_name, server_version, table_count, column_count, truncated, schema FROM schema_snapshots"
            " WHERE connection_id=%s ORDER BY captured_at DESC LIMIT 1", (dbc["id"],))).fetchone()
    running = None
    if dbc:
        running = await (await conn.execute(
            "SELECT id, status, stage, error FROM jobs WHERE resource_id=%s AND kind='schema.capture' ORDER BY created_at DESC LIMIT 1", (dbc["id"],))).fetchone()
    data = {
        "connected": dbc is not None,
        "connection": {"id": dbc["id"], "status": dbc["status"], "host": (dbc["config"] or {}).get("host"), "database": (dbc["config"] or {}).get("database")} if dbc else None,
        "captured": snap is not None,
        "snapshot": {"captured_at": snap["captured_at"], "database": snap["database_name"], "server_version": snap["server_version"],
                     "table_count": snap["table_count"], "column_count": snap["column_count"], "truncated": snap["truncated"], "tables": snap["schema"]["tables"]} if snap else None,
        "last_job": {"id": running["id"], "status": running["status"], "stage": running["stage"], "error": running["error"]} if running else None,
        "provenance": "Read from the live database through a read-only session (structure only, no row data). The schema in your code or migrations may differ.",
    }
    return wrap(request, data)


@router.post("/projects/{project_id}/schema/refresh", status_code=202)
async def refresh_schema(request: Request, ctx: ProjectContext = Depends(project_access(Cap.INDEX)), conn: AsyncConnection = Depends(get_conn)):
    dbc = await (await conn.execute("SELECT id, status FROM connections WHERE project_id=%s AND kind='database' AND status<>'disconnected'", (ctx.project_id,))).fetchone()
    if dbc is None:
        raise Conflict("Connect a database first.", code="no_database")
    if dbc["status"] not in ("connected", "connected_with_warnings"):
        raise Conflict("The database connection is not verified.", code="connection_not_ready")
    job = await queue.enqueue(conn, organization_id=ctx.org.organization_id, project_id=ctx.project_id, kind="schema.capture", requested_by=ctx.org.principal.user_id,
                              payload={"connection_id": str(dbc["id"])}, resource_type="connection", resource_id=dbc["id"], priority=queue.PRIORITY_HUMAN)
    await conn.commit()
    return wrap(request, {"job_id": job["id"], "resource_id": dbc["id"], "status": job["status"], "events_url": f"/api/v1/jobs/{job['id']}"})


def _edge(a: str, b: str, rel: str, origin: str, note: str | None = None) -> dict[str, Any]:
    return {"from": a, "to": b, "relation": rel, "origin": origin, "note": note}


@router.get("/projects/{project_id}/system-map")
async def system_map(request: Request, ctx: ProjectContext = Depends(project_access(Cap.READ)), conn: AsyncConnection = Depends(get_conn)):
    """Structure as CONFIGURED in TRACE plus deployment facts from the provider. Nothing here is 'observed in logs': every edge
    carries its origin so the UI can label it. Unknown links are listed as gaps instead of being drawn."""
    pid = ctx.project_id
    q = lambda sql, *a: conn.execute(sql, a)  # noqa: E731
    envs = await (await q("SELECT id, name, environment_key FROM environments WHERE project_id=%s ORDER BY created_at, name", pid)).fetchall()
    svcs = await (await q("SELECT id, name, service_key FROM services WHERE project_id=%s ORDER BY name", pid)).fetchall()
    scope = await (await q(
        "SELECT l.provider_resource_id, l.name, l.resource_type, l.log_availability, l.last_event_at, e.environment_key, s.service_key"
        " FROM log_scope_entries l JOIN environments e ON e.id=l.environment_id LEFT JOIN services s ON s.id=l.service_id WHERE l.project_id=%s", pid)).fetchall()
    repos = await (await q("SELECT id, full_name, selected_ref, default_branch FROM repository_bindings WHERE project_id=%s ORDER BY full_name", pid)).fetchall()
    maps = await (await q(
        "SELECT b.full_name, s.service_key, m.path_prefix FROM repository_service_mappings m JOIN repository_bindings b ON b.id=m.repository_binding_id"
        " JOIN services s ON s.id=m.service_id WHERE m.project_id=%s", pid)).fetchall()
    deps = await (await q(
        "SELECT DISTINCT ON (d.environment_id, d.provider_project_id) d.provider_deployment_id, d.provider_project_id, d.project_name, d.target, d.state, d.commit_sha,"
        " d.git_repository, d.revision_mapping, d.created_at_provider, e.environment_key FROM deployments d LEFT JOIN environments e ON e.id=d.environment_id"
        " WHERE d.project_id=%s ORDER BY d.environment_id, d.provider_project_id, d.created_at_provider DESC", pid)).fetchall()
    dbc = await (await q("SELECT id, status, config FROM connections WHERE project_id=%s AND kind='database' AND status<>'disconnected'", pid)).fetchone()

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    gaps: list[dict[str, str]] = []
    for e in envs:
        nodes.append({"id": f"env:{e['environment_key']}", "kind": "environment", "label": e["name"], "sub": e["environment_key"]})
    for s in svcs:
        nodes.append({"id": f"svc:{s['service_key']}", "kind": "service", "label": s["name"], "sub": "service"})
    seen_hosting: set[str] = set()
    for r in scope:
        hid = f"host:{r['provider_resource_id']}"
        if hid not in seen_hosting:
            seen_hosting.add(hid)
            nodes.append({"id": hid, "kind": "hosting", "label": r["name"], "sub": r["resource_type"], "status": r["log_availability"]})
        edges.append(_edge(hid, f"env:{r['environment_key']}", "SERVES", "configured", "Selected in Log scope"))
        if r["service_key"]:
            edges.append(_edge(f"svc:{r['service_key']}", hid, "RUNS_ON", "configured"))
        if r["log_availability"] != "available":
            gaps.append({"kind": "logs_not_confirmed", "detail": f"{r['name']} ({r['environment_key']}): log availability is {r['log_availability']}."})
    for r in repos:
        nodes.append({"id": f"repo:{r['full_name']}", "kind": "repository", "label": r["full_name"], "sub": r["selected_ref"]})
    for m in maps:
        edges.append(_edge(f"repo:{m['full_name']}", f"svc:{m['service_key']}", "IMPLEMENTS_SERVICE", "configured", m["path_prefix"] or None))
    for repo in repos:
        if not any(m["full_name"] == repo["full_name"] for m in maps):
            gaps.append({"kind": "repository_unmapped", "detail": f"{repo['full_name']} is not mapped to any service. One repository is not assumed to be one service."})
    repo_names = {r["full_name"].lower(): r["full_name"] for r in repos}
    for d in deps:
        did = f"dep:{d['provider_deployment_id']}"
        sha = (d["commit_sha"] or "")[:7]
        nodes.append({"id": did, "kind": "deployment", "label": f"{sha or 'unknown revision'} · {d['target'] or 'unknown target'}", "sub": d["state"], "status": d["state"]})
        if d["environment_key"]:
            edges.append(_edge(f"env:{d['environment_key']}", did, "HAS_DEPLOYMENT", "provider_metadata", d["project_name"]))
        else:
            gaps.append({"kind": "deployment_environment_unknown", "detail": f"Deployment {d['provider_deployment_id']} ({d['project_name']}) could not be matched to an environment."})
        full = repo_names.get((d["git_repository"] or "").lower())
        if full and d["commit_sha"]:
            edges.append(_edge(did, f"repo:{full}", "DEPLOYS_REVISION", "provider_metadata", d["commit_sha"]))
        elif d["revision_mapping"] == "unknown":
            gaps.append({"kind": "ambiguous_revision", "detail": f"Deployment {d['provider_deployment_id']} carries no commit SHA, so its code cannot be identified."})
    if dbc:
        nodes.append({"id": "db:main", "kind": "database", "label": (dbc["config"] or {}).get("database") or "database", "sub": (dbc["config"] or {}).get("host"), "status": dbc["status"]})
        gaps.append({"kind": "database_link_unknown", "detail": "Which service uses the database is not known; it is shown unattached rather than guessed."})
    return wrap(request, {"nodes": nodes, "edges": edges, "gaps": gaps,
                          "provenance": "Configured structure and provider metadata. Links seen in runtime logs are not drawn until log evidence exists."},
                coverage={"environments": len(envs), "services": len(svcs), "repositories": len(repos), "deployments": len(deps), "has_database": dbc is not None})


@router.post("/projects/{project_id}/deployments/sync", status_code=202)
async def sync_deployments(request: Request, ctx: ProjectContext = Depends(project_access(Cap.INDEX)), conn: AsyncConnection = Depends(get_conn)):
    host = await (await conn.execute("SELECT id, status FROM connections WHERE project_id=%s AND kind='hosting' AND provider='vercel' AND status<>'disconnected'", (ctx.project_id,))).fetchone()
    if host is None or host["status"] not in ("connected", "connected_with_warnings"):
        raise Conflict("Connect and verify Vercel first.", code="no_hosting")
    job = await queue.enqueue(conn, organization_id=ctx.org.organization_id, project_id=ctx.project_id, kind="deployments.sync", requested_by=ctx.org.principal.user_id,
                              payload={"connection_id": str(host["id"])}, resource_type="connection", resource_id=host["id"], priority=queue.PRIORITY_HUMAN)
    await conn.commit()
    return wrap(request, {"job_id": job["id"], "resource_id": host["id"], "status": job["status"], "events_url": f"/api/v1/jobs/{job['id']}"})


@router.get("/projects/{project_id}/deployments")
async def list_deployments(request: Request, environment: str | None = None, limit: int = 50, ctx: ProjectContext = Depends(project_access(Cap.READ)),
                           conn: AsyncConnection = Depends(get_conn)):
    n = max(1, min(limit, 100))
    rows = await (await conn.execute(
        "SELECT d.provider_deployment_id AS id, d.project_name, d.target, d.state, d.commit_sha, d.git_ref, d.git_repository, d.revision_mapping, d.url,"
        " d.created_at_provider, d.ready_at, e.environment_key FROM deployments d LEFT JOIN environments e ON e.id=d.environment_id"
        " WHERE d.project_id=%s AND (%s::text IS NULL OR e.environment_key=%s) ORDER BY d.created_at_provider DESC NULLS LAST LIMIT %s",
        (ctx.project_id, environment, environment, n))).fetchall()
    return wrap(request, rows, coverage={"without_commit_sha": sum(1 for r in rows if r["revision_mapping"] == "unknown")})
