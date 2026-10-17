"""graph.sync: project the control plane (scope, connections, repositories, deployments, tickets, stored log events) into
the evidence graph. Queued automatically after anything that adds traceable facts, so Neo4j stays the one place where the
whole project can be traced; `POST /projects/{id}/graph/sync` runs the same thing on demand.
"""
from __future__ import annotations

from uuid import UUID

from app.graph import sync as S
from app.graph.repository import GraphRepository
from app.jobs.queue import TransientJobError
from app.jobs.triggers import request_graph_sync
from app.jobs.worker import JobContext, handler


@handler("graph.sync")
async def graph_sync(ctx: JobContext) -> dict:
    c = ctx.container
    driver = c.extras.get("neo4j")
    if driver is None:
        raise TransientJobError("graph_unavailable", "The evidence graph (Neo4j) is not configured.")
    pid = UUID(ctx.job["payload"]["project_id"])
    repo = GraphRepository(driver, c.settings.neo4j_database)
    async with c.pool.connection() as conn:
        report = await S.run(conn, repo, pid)
        # A large backlog continues in a follow-up job rather than one unbounded run.
        if report.stored_events_remaining and report.stored_events:
            await request_graph_sync(conn, organization_id=ctx.job["organization_id"], project_id=pid, key=f"{pid}:after:{ctx.job['id']}")
            await conn.commit()
    return {"deployments": report.deployments, "tickets": report.ticket_nodes, "stored_events": report.stored_events,
            "failed": report.stored_events_failed, "remaining": report.stored_events_remaining}
