"""graph.sync: project the control plane (scope, connections, repositories, deployments, tickets) into the evidence graph.
Stored log events are written by the evidence applier (app/graph/apply_evidence.py), not here. Queued automatically after anything that adds traceable facts, so Neo4j stays the one place where the
whole project can be traced; `POST /projects/{id}/graph/sync` runs the same thing on demand.
"""
from __future__ import annotations

import asyncio
from uuid import UUID

from app.graph import sync as S
from app.graph.repository import GraphRepository
from app.jobs.queue import TransientJobError
from app.jobs.worker import JobContext, handler


@handler("graph.sync")
async def graph_sync(ctx: JobContext) -> dict:
    c = ctx.container
    driver = c.extras.get("neo4j")
    if driver is None:
        raise TransientJobError("graph_unavailable", "The evidence graph (Neo4j) is not configured.")
    pid = UUID(ctx.job["payload"]["project_id"])
    async with c.pool.connection() as conn:
        bundle = await S.collect(conn, pid)
    report = await asyncio.to_thread(S.project, GraphRepository(driver, c.settings.neo4j_database), bundle)
    return {"environments": report.environments, "services": report.services, "connections": report.connections, "repositories": report.repositories,
            "deployments": report.deployments, "tickets": report.ticket_nodes}
