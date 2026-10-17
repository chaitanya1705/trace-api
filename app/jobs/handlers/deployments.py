"""deployments.sync: read recent deployments of the Vercel projects in log scope (read-only) and upsert them.
The commit SHA comes only from provider metadata; without it the revision mapping is 'unknown' (ambiguous_revision gap)."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.connectors import vercel as vc
from app.connectors.base import ProviderError
from app.domain.connections import service as svc
from app.jobs.queue import PermanentJobError, TransientJobError
from app.jobs.triggers import request_graph_sync
from app.jobs.worker import JobContext, handler

PER_PROJECT = 50


def _ts(ms: Any) -> datetime | None:
    return datetime.fromtimestamp(ms / 1000, tz=UTC) if isinstance(ms, (int, float)) and not isinstance(ms, bool) else None


def map_environment(dep: dict, env_keys: set[str]) -> str | None:
    """Production target -> 'production'; custom environment slug -> that slug; preview -> 'preview'. Unknown stays None."""
    ce = (dep.get("customEnvironment") or {}).get("slug") if isinstance(dep.get("customEnvironment"), dict) else None
    cand = ce or dep.get("target") or ("preview" if dep.get("target") is None else None)
    return cand if cand in env_keys else None


def deployment_row(dep: dict) -> dict[str, Any]:
    meta = dep.get("meta") or {}
    sha = vc.deployment_sha(meta)
    org, repo = meta.get("githubOrg"), meta.get("githubRepo")
    return {
        "id": dep["uid"], "project_id": dep.get("projectId"), "name": dep.get("name"), "target": dep.get("target"), "state": dep.get("readyState") or dep.get("state"),
        "sha": sha, "ref": meta.get("githubCommitRef"), "repo": f"{org}/{repo}" if org and repo else None, "mapping": "provider_metadata" if sha else "unknown",
        "url": dep.get("url"), "created": _ts(dep.get("created") or dep.get("createdAt")), "ready": _ts(dep.get("ready")),
    }


@handler("deployments.sync")
async def sync_deployments(ctx: JobContext) -> dict:
    c = ctx.container
    cid = UUID(ctx.job["payload"]["connection_id"])
    async with c.pool.connection() as conn:
        row = await (await conn.execute("SELECT * FROM connections WHERE id=%s", (cid,))).fetchone()
        if row is None or row["status"] in ("disconnected", "revoked") or row["provider"] != "vercel" or row["project_id"] is None:
            raise PermanentJobError("connection_gone", "Hosting connection is gone or not attached to a project")
        secret = await svc.active_secret(conn, c.vault, row)
        scoped = await (await conn.execute("SELECT DISTINCT provider_resource_id FROM log_scope_entries WHERE connection_id=%s", (cid,))).fetchall()
        env_keys = {r["environment_key"] for r in await (await conn.execute("SELECT environment_key FROM environments WHERE project_id=%s", (row["project_id"],))).fetchall()}
        env_ids = {r["environment_key"]: r["id"] for r in await (await conn.execute("SELECT id, environment_key FROM environments WHERE project_id=%s", (row["project_id"],))).fetchall()}
    if not scoped:
        raise PermanentJobError("no_scope", "No Vercel projects are selected in Log scope, so there is nothing to read deployments for.")
    client, http = svc.build_adapter(c, row, secret)
    total = 0
    try:
        for i, sp in enumerate(scoped):
            await ctx.check_cancel()
            await ctx.progress("reading_deployments", {"project": i + 1, "of": len(scoped)})
            try:
                deps, _ = await client.deployments(project_id=sp["provider_resource_id"], limit=PER_PROJECT)
            except ProviderError as e:
                if e.retryable:
                    raise TransientJobError(e.code, e.message, e.retry_after) from None
                raise PermanentJobError(e.code, e.message) from None
            async with c.pool.connection() as conn:
                for d in deps:
                    r = deployment_row(d)
                    ek = map_environment(d, env_keys)
                    await conn.execute(
                        "INSERT INTO deployments(organization_id,project_id,connection_id,environment_id,provider_deployment_id,provider_project_id,project_name,target,state,"
                        " commit_sha,git_ref,git_repository,revision_mapping,url,created_at_provider,ready_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                        " ON CONFLICT (connection_id, provider_deployment_id) DO UPDATE SET state=EXCLUDED.state, ready_at=EXCLUDED.ready_at, environment_id=EXCLUDED.environment_id,"
                        " commit_sha=COALESCE(EXCLUDED.commit_sha, deployments.commit_sha), revision_mapping=EXCLUDED.revision_mapping, updated_at=now()",
                        (row["organization_id"], row["project_id"], cid, env_ids.get(ek) if ek else None, r["id"], sp["provider_resource_id"], r["name"], r["target"], r["state"],
                         r["sha"], r["ref"], r["repo"], r["mapping"], r["url"], r["created"], r["ready"]))
                await request_graph_sync(conn, organization_id=row["organization_id"], project_id=row["project_id"])
                await conn.commit()
            total += len(deps)
    finally:
        await http.aclose()
    return {"deployments": total, "projects": len(scoped)}
