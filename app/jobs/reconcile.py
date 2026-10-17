"""Reconciler (T-006, architecture A2): finds control-plane records that should be in the graph but are not, and queues the appliers.

It never writes to Neo4j itself. It looks for work that is *due*:
- evidence rows pending (retry time reached) or claimed with an expired lease (a worker died between claim and acknowledgement),
- ingestion batches whose coverage gaps were not written yet,
- outbox rows pending or leased by a worker that died.
and makes sure one applier job is queued for each project / organization. Appliers are idempotent, so reconciling twice is harmless.
Run on worker start and then periodically from the worker loop. Without Neo4j it does nothing and the rows stay pending.
"""
from __future__ import annotations

from typing import Any

from app.container import Container
from app.domain.ingest import graph_apply as ga
from app.jobs import queue


async def reconcile_graph_work(c: Container) -> dict[str, Any]:
    if c.extras.get("neo4j") is None or c.pool is None:
        return {"evidence_projects": 0, "outbox_orgs": 0, "skipped": "graph not configured"}
    async with c.pool.connection() as conn:
        await ga.reap_stuck(conn)
        projects = await (await conn.execute(
            "SELECT DISTINCT organization_id, project_id FROM evidence_inbox WHERE (graph_status='pending' AND graph_next_attempt_at <= now())"
            " OR (graph_status='claimed' AND graph_lease_expires_at < now())"
            f" UNION SELECT organization_id, project_id FROM ingestion_batches WHERE gaps_applied_at IS NULL AND ({ga.GAP_COLUMNS})")).fetchall()
        for p in projects:
            await ga.request_apply(conn, c, p["organization_id"], p["project_id"])
        orgs = await (await conn.execute(
            "SELECT DISTINCT organization_id FROM outbox WHERE status='pending' AND next_attempt_at <= now() AND (lease_expires_at IS NULL OR lease_expires_at < now())")).fetchall()
        for o in orgs:
            queued = await (await conn.execute("SELECT 1 FROM jobs WHERE kind='graph.apply_outbox' AND organization_id=%s AND status='queued' LIMIT 1", (o["organization_id"],))).fetchone()
            if not queued:
                await queue.enqueue(conn, organization_id=o["organization_id"], kind="graph.apply_outbox", priority=queue.PRIORITY_INGEST)
        await conn.commit()
    return {"evidence_projects": len(projects), "outbox_orgs": len(orgs)}
