"""graph.apply_evidence / graph.apply_outbox: durable appliers that put control-plane records into the evidence graph (T-009, T-006).

Both are safe to run twice and safe to kill: see app/domain/ingest/graph_apply.py and app/graph/outbox.py. Without Neo4j nothing is
claimed, rows stay pending, and the coverage endpoints say so.
"""
from __future__ import annotations

from uuid import UUID

from app.domain.ingest import graph_apply as ga
from app.graph import outbox as ob
from app.jobs.queue import JobCancelled, PermanentJobError, TransientJobError
from app.jobs.worker import JobContext, handler


@handler("graph.apply_evidence")
async def apply_evidence(ctx: JobContext) -> dict:
    c = ctx.container
    if c.extras.get("neo4j") is None:
        raise PermanentJobError("graph_unavailable", "The evidence graph (Neo4j) is not configured; evidence stays pending.")
    project_id = UUID(ctx.job["payload"]["project_id"])
    try:
        out = await ga.apply_pending(c, ctx.job["organization_id"], project_id, ctx.owner, check_cancel=ctx.check_cancel)
    except JobCancelled:
        raise
    except Exception as e:  # noqa: BLE001 - rows were released with backoff; the queue retries the job a bounded number of times
        raise TransientJobError("graph_write_failed", f"Writing evidence to the graph failed ({type(e).__name__}).") from e
    if out["remaining"]:
        async with c.pool.connection() as conn:
            await ga.request_apply(conn, c, ctx.job["organization_id"], project_id)
            await conn.commit()
    return out


@handler("graph.apply_outbox")
async def apply_outbox(ctx: JobContext) -> dict:
    c = ctx.container
    if c.extras.get("neo4j") is None:
        raise PermanentJobError("graph_unavailable", "The evidence graph (Neo4j) is not configured; outbox rows stay pending.")
    try:
        return await ob.apply_outbox(c, ctx.job["organization_id"], ctx.owner, check_cancel=ctx.check_cancel)
    except JobCancelled:
        raise
    except Exception as e:  # noqa: BLE001
        raise TransientJobError("graph_write_failed", f"Applying the outbox to the graph failed ({type(e).__name__}).") from e
