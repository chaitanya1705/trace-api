"""logs.capture: explicit, bounded, read-only live capture of a deployment's runtime logs ("Fetch from source", R-018).
Results are normalized and stored before they are shown; a failed read stores nothing and says so.

Every scoped deployment is followed at the same time for the whole window, and each stream stays open
DELIVERY_LAG_SECONDS longer than the window: Vercel delivers runtime-log lines about 10-11 s after they are logged
(measured 2026-10-17 against ShopFlow), so lines logged near the end of the window would otherwise never arrive.
Splitting the window between scoped projects and reading them one after another starved each of them (with three
scoped projects a 30 s capture stored 2 lines where one scoped project stored 13 for the same traffic)."""
from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from app.connectors import vercel as vc
from app.connectors.base import ProviderError
from app.domain.connections import service as svc
from app.domain.ingest.intake import finish
from app.domain.ingest.store import env_lookup, store_rows
from app.domain.ingest.vercel_logs import normalize_runtime
from app.jobs.queue import JobCancelled, PermanentJobError, TransientJobError
from app.jobs.worker import JobContext, handler

MAX_SECONDS = 60
DELIVERY_LAG_SECONDS = 15
FLUSH = 100
NOTE = ("Live capture is partial: Vercel's runtime-log stream delivers only some lines logged while it runs (measured: about 3 of "
        "~16 diagnostics events per request), about 10 s late, and no history. Use a log drain for complete logs.")


def _job_error(e: ProviderError) -> Exception:
    return (TransientJobError if e.retryable else PermanentJobError)(e.code, e.message)


def _capture_totals(previous: dict | None, current: dict) -> dict:
    """Keep stored-event accounting from earlier attempts when a capture retries."""
    if not previous:
        return current
    return {
        **current,
        **{key: previous.get(key, 0) + current.get(key, 0) for key in ("read", "accepted", "duplicates")},
        "seconds": previous.get("seconds", 0) + current.get("seconds", 0),
        "listening_seconds": previous.get("listening_seconds", 0) + current.get("listening_seconds", 0),
        "resources": [*previous.get("resources", []), *current.get("resources", [])],
    }


class _PartialResourceFailure(Exception):
    def __init__(self, error: ProviderError, result: dict):
        super().__init__(str(error))
        self.error = error
        self.result = result


class _PartialResourceCancellation(JobCancelled):
    def __init__(self, result: dict):
        super().__init__()
        self.result = result


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
    resources: list[dict] = []
    try:
        targets = []
        for sp in scoped:
            await ctx.check_cancel()
            ppid, ek = sp["provider_resource_id"], sp["environment_key"]
            try:
                deps, _ = await client.deployments(project_id=ppid, target="production" if ek == "production" else None, limit=1)
            except ProviderError as e:
                raise _job_error(e) from None
            if not deps:
                resources.append({"provider_project_id": ppid, "environment": ek, "deployment": None, "outcome": "no_deployment",
                                  "read": 0, "accepted": 0, "duplicates": 0})
                continue
            targets.append((ppid, ek, deps[0]["uid"]))
        listen = seconds + DELIVERY_LAG_SECONDS
        await ctx.progress("capturing", {"deployments": [t[2] for t in targets], "seconds": seconds, "listening_seconds": listen})
        results = await asyncio.gather(*(_follow(ctx, row, client, envs, ppid, ek, dep, listen) for ppid, ek, dep in targets),
                                       return_exceptions=True)
    finally:
        await http.aclose()
    errors = [r for r in results if isinstance(r, BaseException)]
    if errors:
        # Cancellation and unexpected failures win over provider errors; lines already stored stay stored.
        if any(isinstance(e, JobCancelled) for e in errors):
            resources += [r.result for r in errors if isinstance(r, (_PartialResourceFailure, _PartialResourceCancellation))]
            resources += [r for r in results if not isinstance(r, BaseException)]
            total = {k: sum(r[k] for r in resources) for k in ("read", "accepted", "duplicates")}
            partial = {**total, "seconds": seconds, "listening_seconds": listen, "resources": resources, "note": NOTE}
            cancelled = next(e for e in errors if isinstance(e, JobCancelled))
            cancelled.result_ref = _capture_totals(ctx.job.get("result_ref"), partial)
            raise cancelled
        other = next((e for e in errors if not isinstance(e, (_PartialResourceFailure, ProviderError))), None)
        if other is not None:
            raise other
        resources += [r.result for r in errors if isinstance(r, _PartialResourceFailure)]
        resources += [r for r in results if not isinstance(r, BaseException)]
        total = {k: sum(r[k] for r in resources) for k in ("read", "accepted", "duplicates")}
        partial = {**total, "seconds": seconds, "listening_seconds": listen, "resources": resources, "note": NOTE}
        provider_error = next((e.error for e in errors if isinstance(e, _PartialResourceFailure)), None)
        if provider_error is None:
            provider_error = errors[0]
        job_error = _job_error(provider_error)
        job_error.result_ref = _capture_totals(ctx.job.get("result_ref"), partial)
        raise job_error from None
    resources += results
    total = {k: sum(r[k] for r in resources) for k in ("read", "accepted", "duplicates")}
    current = {**total, "seconds": seconds, "listening_seconds": listen, "resources": resources, "note": NOTE}
    return _capture_totals(ctx.job.get("result_ref"), current)


async def _follow(ctx: JobContext, row, client, envs, ppid: str, ek: str, dep: str, listen: float) -> dict:
    counts = {"read": 0, "accepted": 0, "duplicates": 0}
    batch: list = []
    unusable = 0   # lines in the current slice that could not be normalised (counted, never silently dropped)
    try:
        async for rec in vc.tail_runtime_logs(client, ppid, dep, seconds=listen):
            counts["read"] += 1
            r = normalize_runtime(rec, deployment_id=dep, provider_project_id=ppid, env_key=ek)
            if r is not None:
                batch.append((r, envs.get(ek)))
            else:
                unusable += 1
            if len(batch) + unusable >= FLUSH:
                await _flush(ctx.container, row, batch, counts, unusable)
                batch, unusable = [], 0
            await ctx.check_cancel()
    except JobCancelled:
        if batch or unusable:
            await _flush(ctx.container, row, batch, counts, unusable)
        raise _PartialResourceCancellation({"provider_project_id": ppid, "environment": ek, "deployment": dep,
                                           "outcome": "partial", **counts}) from None
    except ProviderError as e:
        if batch or unusable:
            await _flush(ctx.container, row, batch, counts, unusable)
        raise _PartialResourceFailure(e, {"provider_project_id": ppid, "environment": ek, "deployment": dep,
                                          "outcome": "partial", **counts}) from None
    if batch or unusable:
        await _flush(ctx.container, row, batch, counts, unusable)
    return {"provider_project_id": ppid, "environment": ek, "deployment": dep, "outcome": "captured", **counts}


async def _flush(c, row, batch, counts, unusable: int = 0):
    """Store one slice of what the stream delivered, account for it as an intake batch and queue the graph applier, in one transaction."""
    batch_id = uuid4()
    async with c.pool.connection() as conn:
        r = await store_rows(conn, organization_id=row["organization_id"], project_id=row["project_id"], connection_id=row["id"], provider="vercel", rows=batch, batch_id=batch_id)
        await finish(conn, c, row, via="capture", received=len(batch) + unusable, res=r, rejected=unusable, batch_id=batch_id)
    counts["accepted"] += r.accepted
    counts["duplicates"] += r.duplicates
