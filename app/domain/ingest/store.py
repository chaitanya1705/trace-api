"""Durable receipt of accepted evidence rows. Idempotent: a provider record id is stored once per connection (at-least-once transport,
exactly-once logical evidence). Writing a row also marks the matching log-scope resource as having logs, with its latest event time."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

from app.domain.ingest.vercel_logs import LogRow


@dataclass
class StoreResult:
    accepted: int = 0
    duplicates: int = 0


async def env_lookup(conn: AsyncConnection, project_id: UUID) -> dict[str, UUID]:
    return {r["environment_key"]: r["id"] for r in await (await conn.execute("SELECT id, environment_key FROM environments WHERE project_id=%s", (project_id,))).fetchall()}


async def store_rows(conn: AsyncConnection, *, organization_id: UUID, project_id: UUID, connection_id: UUID | None, provider: str, rows: list[tuple[LogRow, UUID | None]],
                     ticket_id: UUID | None = None) -> StoreResult:
    """rows: (normalized row, environment id or None). Environment stays NULL when it could not be mapped (never guessed)."""
    res = StoreResult()
    latest: dict[str, Any] = {}
    for row, env_id in rows:
        got = await (await conn.execute(
            "INSERT INTO evidence_inbox(organization_id,project_id,connection_id,ticket_id,environment_id,provider,kind,source_record_key,event_time,level,source,provider_project_id,"
            " deployment_id,request_id,status_code,service_hint,message,content_hash,extra) VALUES (%s,%s,%s,%s,%s,%s,'log',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)"
            " ON CONFLICT (project_id, source_record_key) DO NOTHING RETURNING id",
            (organization_id, project_id, connection_id, ticket_id, env_id, provider, row.source_record_key, row.event_time, row.level, row.source, row.provider_project_id,
             row.deployment_id, row.request_id, row.status_code, row.service_hint, row.message, row.content_hash, json.dumps(row.extra, default=str)))).fetchone()
        if got:
            res.accepted += 1
            if connection_id and row.event_time and (row.provider_project_id not in latest or row.event_time > latest[row.provider_project_id]):
                latest[row.provider_project_id] = row.event_time
        else:
            res.duplicates += 1
    for ppid, ts in latest.items():
        await conn.execute(
            "UPDATE log_scope_entries SET log_availability='available', availability_note='Logs received', last_event_at=GREATEST(COALESCE(last_event_at, %s), %s)"
            " WHERE connection_id=%s AND provider_resource_id=%s", (ts, ts, connection_id, ppid))
    return res
