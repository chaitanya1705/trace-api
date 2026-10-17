"""Accounting at the end of an intake request, shared by the Vercel drain, the app log push and live capture.

Every request that was authenticated leaves one `ingestion_batches` row, in the same transaction as the evidence rows it stored, and queues the
graph applier (the job row is the outbox). A request refused whole (oversize, malformed, too many events) still leaves its row, with a
`reason`, so the loss shows up as a coverage gap in the graph instead of disappearing.
"""
from __future__ import annotations

from uuid import UUID, uuid4

from psycopg import AsyncConnection

from app.container import Container
from app.domain.ingest.graph_apply import request_apply
from app.domain.ingest.store import StoreResult, record_batch


async def finish(conn: AsyncConnection, c: Container, row: dict, *, via: str, received: int, res: StoreResult | None = None, out_of_scope: int = 0,
                 rejected: int = 0, reason: str | None = None, body_bytes: int | None = None, batch_id: UUID | None = None) -> None:
    """Record the batch, queue the applier, commit. `row` is the connection row (project and organization of the source)."""
    await record_batch(conn, batch_id=batch_id or uuid4(), organization_id=row["organization_id"], project_id=row["project_id"], connection_id=row["id"], via=via,
                       received=received, res=res, out_of_scope=out_of_scope, rejected=rejected, reason=reason, body_bytes=body_bytes)
    await request_apply(conn, c, row["organization_id"], row["project_id"])
    await conn.commit()
