"""graph.index: fetch one bound repository at an exact commit and index it into the evidence graph.

Stages (recorded on the run row and on the job): resolving -> downloading -> extracting -> indexing -> publishing.
Read-only against GitHub; nothing from the repository is executed. A failure leaves any previously published build alone.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from uuid import UUID

from app.audit import audit
from app.connectors.base import ProviderError
from app.connectors.github_source import download_archive, resolve_commit
from app.domain.connections import service as svc
from app.graph.archive import ArchiveError, extract_tarball
from app.graph.indexer import index_repository
from app.graph.repository import GraphRepository
from app.jobs.queue import PermanentJobError, TransientJobError
from app.jobs.worker import JobContext, handler

NOW = object()  # sentinel: write the database clock, not a Python timestamp


async def _set(c, run_id: UUID, **cols) -> None:
    parts, vals = [], []
    for k, v in cols.items():
        if v is NOW:
            parts.append(f"{k}=now()")
        elif k in ("counts", "gaps", "error"):
            parts.append(f"{k}=%s::jsonb")
            vals.append(json.dumps(v) if v is not None else None)
        else:
            parts.append(f"{k}=%s")
            vals.append(v)
    async with c.pool.connection() as conn:
        await conn.execute(f"UPDATE graph_index_runs SET {', '.join(parts)} WHERE id=%s", (*vals, run_id))
        await conn.commit()


async def _fail(ctx: JobContext, run_id: UUID, code: str, message: str, *, retryable: bool) -> None:
    """Record the failure on the run only when it is final; transient failures are retried by the queue first."""
    final = (not retryable) or ctx.job["attempt"] >= ctx.job["max_attempts"]
    if final:
        await _set(ctx.container, run_id, status="failed", error={"code": code, "message": message}, finished_at=NOW)
    if retryable and not final:
        raise TransientJobError(code, message)
    raise PermanentJobError(code, message)


@handler("graph.index")
async def graph_index(ctx: JobContext) -> dict:
    c = ctx.container
    run_id = UUID(ctx.job["payload"]["run_id"])
    driver = c.extras.get("neo4j")
    async with c.pool.connection() as conn:
        run = await (await conn.execute("SELECT * FROM graph_index_runs WHERE id=%s", (run_id,))).fetchone()
        if run is None:
            raise PermanentJobError("run_gone", "Indexing request was removed")
        b = await (await conn.execute("SELECT * FROM repository_bindings WHERE id=%s", (run["repository_binding_id"],))).fetchone()
        cx = await (await conn.execute("SELECT * FROM connections WHERE id=%s", (b["connection_id"],))).fetchone() if b else None
        proj = await (await conn.execute("SELECT name FROM projects WHERE id=%s", (run["project_id"],))).fetchone()
        if b is None or cx is None or cx["status"] in ("disconnected", "revoked", "failed", "expired"):
            await _fail(ctx, run_id, "connection_unavailable", "The GitHub connection is not usable. Reconnect it and try again.", retryable=False)
        secret = await svc.active_secret(conn, c.vault, cx)
    if driver is None:
        await _fail(ctx, run_id, "graph_unavailable", "The evidence graph (Neo4j) is not configured.", retryable=False)

    await _set(c, run_id, status="running", stage="resolving", started_at=NOW)
    await ctx.progress("resolving")
    client, http = svc.build_adapter(c, cx, secret)
    work = tempfile.mkdtemp(prefix="trace-index-")
    try:
        try:
            if cx["auth_method"] == "github_app":
                iid = (cx["config"] or {}).get("installation_id")
                if not iid:
                    await _fail(ctx, run_id, "connection_unavailable", "No GitHub installation recorded for this connection.", retryable=False)
                token, _ = await client.installation_token(iid)
            else:
                token = secret or ""
            sha = await resolve_commit(client, token, b["full_name"], run["requested_ref"])
            await _set(c, run_id, commit_sha=sha, stage="downloading")
            await ctx.progress("downloading")
            await ctx.check_cancel()
            archive = os.path.join(work, "src.tar.gz")
            await download_archive(client, token, b["full_name"], sha, archive)
        except ProviderError as e:
            await _fail(ctx, run_id, e.code, e.message, retryable=e.retryable)
        finally:
            await http.aclose()

        await _set(c, run_id, stage="extracting")
        await ctx.progress("extracting")
        tree = os.path.join(work, "tree")
        os.makedirs(tree)
        try:
            await asyncio.to_thread(extract_tarball, archive, tree)
        except ArchiveError as e:
            await _fail(ctx, run_id, "archive_refused", f"Repository archive was refused: {e}", retryable=False)
        await ctx.check_cancel()

        await _set(c, run_id, stage="indexing")
        await ctx.progress("indexing")
        repo = GraphRepository(driver, c.settings.neo4j_database)
        try:
            report = await asyncio.to_thread(
                index_repository, repo, project_id=str(run["project_id"]), root=tree, repository_name=b["full_name"],
                organization_id=str(run["organization_id"]), project_name=proj["name"] if proj else None, provider="github",
                provider_repository_id=b["provider_repository_id"], canonical_url=f"https://github.com/{b['full_name']}", commit_sha=sha,
            )
        except ValueError as e:
            await _fail(ctx, run_id, "nothing_to_index", str(e), retryable=False)
        except Exception as e:  # noqa: BLE001 - graph/driver failures: retry a bounded number of times, class only in the message
            await _fail(ctx, run_id, "graph_write_failed", f"Writing the graph failed ({type(e).__name__}).", retryable=True)

        counts = {"files": report.files, "chunks": report.chunks, "symbols": report.symbols, "edges": report.edges, "skipped_files": report.skipped}
        await _set(c, run_id, status="succeeded", stage="published", build_id=report.build_id, manifest_id=report.manifest_id,
                   counts=counts, gaps=report.gaps, extractor=report.extractor, finished_at=NOW)
        async with c.pool.connection() as conn:
            await audit(conn, organization_id=run["organization_id"], project_id=run["project_id"], actor=ctx.job["requested_by"],
                        action="graph.index", result="success", target_type="repository_binding", target_id=run["repository_binding_id"],
                        details={"commit": sha[:12], "symbols": report.symbols})
            await conn.commit()
        return {"build_id": report.build_id, "commit_sha": sha}
    finally:
        shutil.rmtree(work, ignore_errors=True)

