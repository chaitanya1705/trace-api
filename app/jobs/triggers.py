"""Requests for background work that other modules raise without importing the handlers (keeps imports acyclic)."""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from psycopg import AsyncConnection

from app.jobs import queue

#: Requests inside one window collapse into one graph.sync job (the job reads everything pending when it runs).
DEBOUNCE_SECONDS = 30


async def request_graph_sync(conn: AsyncConnection, *, organization_id: UUID, project_id: UUID | None, requested_by: UUID | None = None, key: str | None = None) -> None:
    """Queue a projection of the project into the evidence graph, at most one per debounce window. The caller commits."""
    if project_id is None:
        return
    bucket = int(datetime.now(UTC).timestamp()) // DEBOUNCE_SECONDS
    await queue.enqueue(conn, organization_id=organization_id, project_id=project_id, kind="graph.sync", payload={"project_id": str(project_id)},
                        requested_by=requested_by, idempotency_key=key or f"{project_id}:{bucket}", resource_type="project", resource_id=project_id)
