"""logs.capture: explicit, bounded, read-only live capture of a deployment's runtime logs ("Fetch from source", R-018).
Results are normalized and stored before they are shown; a failed read stores nothing and says so."""
from __future__ import annotations

from uuid import UUID

from app.connectors import vercel as vc
from app.connectors.base import ProviderError
from app.domain.connections import service as svc
from app.domain.ingest.store import env_lookup, store_rows
from app.domain.ingest.vercel_logs import normalize_runtime
from app.jobs.queue import PermanentJobError, TransientJobError
from app.jobs.worker import JobContext, handler

MAX_SECONDS = 60
FLUSH = 100


@handler("logs.capture")
async def capture_logs(ctx: JobContext) -> dict:
    c = ctx.container
    pl = ctx.job["payload"]
    cid = UUID(pl["connection_id"])
    env_key = pl.get("environment")
    seconds = max(5, min(int(pl.get("seconds") or 30), MAX_SECONDS))
    async with c.pool.connection() as conn:
        row = await (await conn.execute("SELECT * FROM connections WHERE id=%s", (cid,))).fetchone()
        if row is None or row["status"] in ("disconnected", "revoked") or row["provider"] != "vercel" or row["project_id"] is None:
            raise PermanentJobError("connection_gone", "Hosting connection is gone or not attached to a project")
        secret = await svc.active_secret(conn, c.vault, row)
        envs = await env_lookup(conn, row["project_id"])
        scoped = await (await conn.execute(
            "SELECT l.provider_resource_id, e.environment_key FROM log_scope_entries l JOIN environments e ON e.id=l.environment_id WHERE l.connection_id=%s", (cid,))).fetchall()
    scoped = [s for s in scoped if not env_key or s["environment_key"] == env_key]
    if not scoped:
        raise PermanentJobError("no_scope", "No Vercel project is selected in Log scope" + (f" for {env_key}" if env_key else "") + ".")
    client, http = svc.build_adapter(c, row, secret)
    total = {"accepted": 0, "duplicates": 0, "read": 0}
    try:
        for sp in scoped:
            await ctx.check_cancel()
            ppid, ek = sp["provider_resource_id"], sp["environment_key"]
            try:
                deps, _ = await client.deployments(project_id=ppid, target="production" if ek == "production" else None, limit=1)
            except ProviderError as e:
                raise (TransientJobError if e.retryable else PermanentJobError)(e.code, e.message) from None
            if not deps:
                continue
            dep = deps[0]["uid"]
            await ctx.progress("capturing", {"deployment": dep, "environment": ek, "seconds": seconds})
            batch: list = []
            try:
                async for rec in vc.tail_runtime_logs(client, ppid, dep, seconds=seconds / max(1, len(scoped))):
                    total["read"] += 1
                    r = normalize_runtime(rec, deployment_id=dep, provider_project_id=ppid, env_key=ek)
                    if r is not None:
                        batch.append((r, envs.get(ek)))
                    if len(batch) >= FLUSH:
                        total = await _flush(c, row, batch, total)
                        batch = []
                    await ctx.check_cancel()
            except ProviderError as e:
                if batch:
                    total = await _flush(c, row, batch, total)
                raise (TransientJobError if e.retryable else PermanentJobError)(e.code, e.message) from None
            if batch:
                total = await _flush(c, row, batch, total)
    finally:
        await http.aclose()
    return {**total, "seconds": seconds, "note": "Live capture is partial: Vercel's runtime-log stream delivers only some lines logged while it runs (measured: about 3 of ~16 diagnostics events per request) and no history. Use a log drain for complete logs."}


async def _flush(c, row, batch, total):
    async with c.pool.connection() as conn:
        r = await store_rows(conn, organization_id=row["organization_id"], project_id=row["project_id"], connection_id=row["id"], provider="vercel", rows=batch)
        await conn.commit()
    total["accepted"] += r.accepted
    total["duplicates"] += r.duplicates
    return total
