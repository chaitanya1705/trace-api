"""schema.capture: read the customer's Postgres schema metadata (read-only) and store a snapshot. Also queued after a successful verify."""
from __future__ import annotations

import json
from uuid import UUID

from app.connectors import postgres_db as pgdb
from app.connectors.base import ProviderError
from app.domain.connections import service as svc
from app.jobs.queue import PermanentJobError, TransientJobError
from app.jobs.worker import JobContext, handler


@handler("schema.capture")
async def capture_schema(ctx: JobContext) -> dict:
    c = ctx.container
    cid = UUID(ctx.job["payload"]["connection_id"])
    async with c.pool.connection() as conn:
        row = await (await conn.execute("SELECT * FROM connections WHERE id=%s", (cid,))).fetchone()
        if row is None or row["status"] in ("disconnected", "revoked") or row["provider"] != "postgres":
            raise PermanentJobError("connection_gone", "Database connection is gone or disconnected")
        if row["project_id"] is None:
            raise PermanentJobError("no_project", "Connection is not attached to a project")
        dsn = await svc.active_secret(conn, c.vault, row)
    if not dsn:
        raise PermanentJobError("no_credential", "No stored connection string")
    await ctx.progress("reading_schema")
    try:
        snap = await pgdb.introspect(dsn)
    except ProviderError as e:
        if e.retryable:
            raise TransientJobError(e.code, e.message, e.retry_after) from None
        raise PermanentJobError(e.code, e.message) from None
    await ctx.check_cancel()
    cols = sum(len(t["columns"]) for t in snap["tables"])
    async with c.pool.connection() as conn:
        cur = await (await conn.execute("SELECT status FROM connections WHERE id=%s", (cid,))).fetchone()
        if cur["status"] == "disconnected":  # disconnected while reading: store nothing
            raise PermanentJobError("connection_gone", "Disconnected during the read")
        await conn.execute(
            "INSERT INTO schema_snapshots(organization_id,project_id,connection_id,database_name,server_version,table_count,column_count,truncated,schema)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
            (row["organization_id"], row["project_id"], cid, snap["database"], snap["server_version"], len(snap["tables"]), cols,
             snap["truncated"], json.dumps({"tables": snap["tables"]})))
        await conn.execute("DELETE FROM schema_snapshots WHERE connection_id=%s AND id NOT IN (SELECT id FROM schema_snapshots WHERE connection_id=%s ORDER BY captured_at DESC LIMIT 5)", (cid, cid))
        await conn.commit()
    return {"tables": len(snap["tables"]), "columns": cols, "truncated": snap["truncated"]}
