"""Outbox applier (T-006, architecture A2): control-plane writes that must reach the graph are committed in Postgres together with an `outbox`
row; this applies them to Neo4j afterwards and acknowledges the row. The two stores never share a transaction.

Crash safety: a claim carries a lease, so a row whose worker died is claimable again once the lease expires (that is the reconciler:
`app/jobs/reconcile.py` queues the applier whenever rows are due or their lease ran out). Graph ids are deterministic and the writes are
MERGEs, so replaying a row that had already reached Neo4j changes nothing.

`project_scope` rows are applied from the project's CURRENT state rather than from the (possibly older) payload, so several rows for one
project collapse into a single graph write.
"""
from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

from app.container import Container
from app.graph import identity as I
from app.graph.repository import GraphRepository, prepare_node, prepare_rel

KIND_SCOPE = "project_scope"
MAX_ATTEMPTS = 8
LEASE_SECONDS = 120
BATCH = 50


async def claim(conn: AsyncConnection, organization_id: UUID, owner: str, *, limit: int = BATCH, lease_seconds: int = LEASE_SECONDS) -> list[dict]:
    rows = await (await conn.execute(
        "UPDATE outbox o SET lease_owner=%s, lease_expires_at = now() + make_interval(secs => %s), attempts = o.attempts + 1 FROM ("
        " SELECT id FROM outbox WHERE organization_id=%s AND status='pending' AND next_attempt_at <= now() AND (lease_expires_at IS NULL OR lease_expires_at < now())"
        " ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT %s) n WHERE o.id = n.id RETURNING o.*",
        (owner, lease_seconds, organization_id, limit))).fetchall()
    await conn.commit()
    return rows


async def acknowledge(conn: AsyncConnection, ids: list[UUID], owner: str) -> int:
    cur = await conn.execute(
        "UPDATE outbox SET status='applied', applied_at=now(), lease_owner=NULL, lease_expires_at=NULL, last_error=NULL WHERE id = ANY(%s) AND lease_owner=%s AND status='pending'", (ids, owner))
    await conn.commit()
    return cur.rowcount


async def release(conn: AsyncConnection, ids: list[UUID], owner: str, error: str, *, permanent: bool = False) -> None:
    await conn.execute(
        "UPDATE outbox SET status = CASE WHEN %s OR attempts >= %s THEN 'failed' ELSE 'pending' END, lease_owner=NULL, lease_expires_at=NULL, last_error=%s,"
        " next_attempt_at = now() + make_interval(secs => LEAST(300, 5 * power(2, GREATEST(attempts - 1, 0)))) WHERE id = ANY(%s) AND lease_owner=%s AND status='pending'",
        (permanent, MAX_ATTEMPTS, error[:300], ids, owner))
    await conn.commit()


async def collect_scope(conn: AsyncConnection, project_id: UUID) -> dict[str, Any] | None:
    p = await (await conn.execute("SELECT id, organization_id, name FROM projects WHERE id=%s", (project_id,))).fetchone()
    if p is None:
        return None
    envs = await (await conn.execute("SELECT environment_key, name FROM environments WHERE project_id=%s ORDER BY environment_key", (project_id,))).fetchall()
    svcs = await (await conn.execute("SELECT service_key, name FROM services WHERE project_id=%s ORDER BY service_key", (project_id,))).fetchall()
    return {"project_id": str(p["id"]), "organization_id": str(p["organization_id"]), "name": p["name"], "environments": envs, "services": svcs}


def project_scope(repo: GraphRepository, scope: dict[str, Any]) -> dict[str, int]:
    """Project, environments and logical services as graph nodes under the project (idempotent MERGE by deterministic id)."""
    pid = scope["project_id"]
    prj = I.project_node_id(pid)
    nodes = [prepare_node("Project", prj, pid, {"organization_id": scope["organization_id"], "name": scope["name"]})]
    rels = []
    for e in scope["environments"]:
        eid = I.environment_id(pid, e["environment_key"])
        nodes.append(prepare_node("Environment", eid, pid, {"name": e["name"], "environment_key": e["environment_key"]}))
        rels.append(prepare_rel("HAS_ENVIRONMENT", "Project", prj, "Environment", eid, pid, I.edge_key("HAS_ENVIRONMENT", prj, eid)))
    for s in scope["services"]:
        sid = I.service_id(pid, s["service_key"])
        nodes.append(prepare_node("Service", sid, pid, {"name": s["name"], "service_key": s["service_key"]}))
        rels.append(prepare_rel("HAS_SERVICE", "Project", prj, "Service", sid, pid, I.edge_key("HAS_SERVICE", prj, sid)))
    repo.write_nodes(nodes)
    repo.write_relationships(rels)
    return {"environments": len(scope["environments"]), "services": len(scope["services"])}


async def apply_outbox(c: Container, organization_id: UUID, owner: str, *, max_batches: int = 10, check_cancel=None) -> dict[str, Any]:
    """Claim due rows, mirror each project's scope into Neo4j, acknowledge after the graph commit."""
    driver = c.extras.get("neo4j")
    if driver is None:
        raise RuntimeError("graph not configured")
    repo = GraphRepository(driver, c.settings.neo4j_database)
    out = {"applied": 0, "failed": 0, "projects": 0}
    for _ in range(max_batches):
        if check_cancel:
            await check_cancel()
        async with c.pool.connection() as conn:
            rows = await claim(conn, organization_id, owner)
        if not rows:
            break
        by_project: dict[UUID | None, list[dict]] = {}
        for r in rows:
            by_project.setdefault(r["project_id"], []).append(r)
        for project_id, group in by_project.items():
            unknown = [r["id"] for r in group if r["kind"] != KIND_SCOPE]
            known = [r["id"] for r in group if r["kind"] == KIND_SCOPE]
            async with c.pool.connection() as conn:
                if unknown:
                    await release(conn, unknown, owner, "unknown outbox kind", permanent=True)
                    out["failed"] += len(unknown)
                scope = await collect_scope(conn, project_id) if known and project_id else None
            if not known:
                continue
            if scope is None:       # the project is gone: nothing to mirror
                async with c.pool.connection() as conn:
                    out["applied"] += await acknowledge(conn, known, owner)
                continue
            try:
                await asyncio.to_thread(project_scope, repo, scope)
            except Exception as e:  # noqa: BLE001 - graph unreachable or write refused: retry with backoff; class name only
                async with c.pool.connection() as conn:
                    await release(conn, known, owner, type(e).__name__)
                raise
            async with c.pool.connection() as conn:
                out["applied"] += await acknowledge(conn, known, owner)
            out["projects"] += 1
    return out
