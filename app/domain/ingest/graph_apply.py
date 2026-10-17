"""Postgres half of the evidence -> graph applier (T-009, T-006; architecture A2).

Receipt, claim, acknowledgement and the reconciler's view of `evidence_inbox`. No transaction is held across Neo4j: a claim is one short
transaction that leases rows, the graph write happens outside any transaction, and the acknowledgement is another short transaction.

    pending --claim--> claimed --graph commit--> applied
       ^                  |  (crash, lost lease: claimable again once the lease expires; deterministic ids make the replay a no-op)
       +-- retry w/ backoff + (attempt limit or refused by the ontology) --> failed   (visible, retryable on request)

The ingestion cursor (see `graph_state`) is derived from these statuses, so it can never advance past evidence the graph does not have.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

from app.container import Container
from app.graph.apply_evidence import ApplyReport, EvidenceBundle, project_evidence
from app.graph.repository import GraphRepository, GraphWriteError
from app.graph.validation import GraphValidationError
from app.jobs import queue

BATCH = 200            # rows per Neo4j write
MAX_BATCHES = 20       # per job: bounded work, the job re-queues itself if more remains
MAX_ATTEMPTS = 5
LEASE_SECONDS = 120
GAP_COLUMNS = "reason IS NOT NULL OR rejected > 0 OR out_of_scope > 0 OR truncated > 0 OR unmapped > 0"
LOOKUP_MAX = 10       # deployments fetched from the provider per call

log = logging.getLogger(__name__)


# ---- deployments the events name ---------------------------------------------------------------------------------
async def link_unknown_deployments(c: Container, project_id: UUID, deployment_ids: set[str]) -> int:
    """Fetch deployments that events name but TRACE has not synced yet (one provider read per id, bounded), so the events can be
    tied to the deployment's service instead of being stored with none. Only deployments of Vercel projects in this project's
    log scope are kept. Provider reads happen outside any transaction; a failed read leaves those events without a service."""
    from app.connectors.base import ProviderError
    from app.domain.connections import service as svc
    from app.jobs.handlers.deployments import upsert_deployment

    ids = sorted(deployment_ids)
    if not ids:
        return 0
    async with c.pool.connection() as conn:
        known = {r["provider_deployment_id"] for r in await (await conn.execute(
            "SELECT provider_deployment_id FROM deployments WHERE project_id=%s AND provider_deployment_id = ANY(%s)", (project_id, ids))).fetchall()}
        missing = [i for i in ids if i not in known][:LOOKUP_MAX]
        row = await (await conn.execute(
            "SELECT * FROM connections WHERE project_id=%s AND provider='vercel' AND status NOT IN ('disconnected','revoked') ORDER BY created_at LIMIT 1",
            (project_id,))).fetchone() if missing else None
        if row is None:
            return 0
        secret = await svc.active_secret(conn, c.vault, row)
        scoped = {r["provider_resource_id"] for r in await (await conn.execute(
            "SELECT provider_resource_id FROM log_scope_entries WHERE connection_id=%s", (row["id"],))).fetchall()}
        env_ids = {r["environment_key"]: r["id"] for r in await (await conn.execute(
            "SELECT id, environment_key FROM environments WHERE project_id=%s", (project_id,))).fetchall()}
        await conn.commit()
    client, http = svc.build_adapter(c, row, secret)
    found: list[dict] = []
    try:
        for dpl in missing:
            try:
                d = await client.deployment(dpl)
            except ProviderError as e:
                log.warning("deployment lookup %s failed: %s", dpl, e.code)
                continue
            if d.get("projectId") in scoped:
                found.append(d)
    finally:
        await http.aclose()
    if found:
        async with c.pool.connection() as conn:
            for d in found:
                await upsert_deployment(conn, row, d, d["projectId"], env_ids)
            await conn.commit()
    return len(found)


async def relink_unlinked(c: Container, project_id: UUID) -> int:
    """Events applied before their deployment was known carry no service, so the investigator cannot see them. Once the
    deployment is known (fetched here, or by a deployment sync) they are queued again with a re-link scope: the graph keeps
    each event node exactly as first written and gains the service and deployment edges. Recent events only (7 days)."""
    unlinked = ("i.project_id=%s AND i.graph_status='applied' AND i.deployment_id IS NOT NULL AND COALESCE(i.source,'') <> 'browser'"
                " AND i.graph_scope->>'service_key' IS NULL AND i.graph_scope->>'relink' IS NULL AND i.ingested_at > now() - interval '7 days'")
    async with c.pool.connection() as conn:
        ids = {r["deployment_id"] for r in await (await conn.execute(
            f"SELECT DISTINCT i.deployment_id FROM evidence_inbox i WHERE {unlinked}", (project_id,))).fetchall()}
    if not ids:
        return 0
    await link_unknown_deployments(c, project_id, ids)
    async with c.pool.connection() as conn:
        n = (await conn.execute(
            "UPDATE evidence_inbox i SET graph_scope='{\"relink\": true}'::jsonb, graph_status='pending', graph_attempts=0, graph_error=NULL,"
            f" graph_next_attempt_at=now() WHERE {unlinked}"
            " AND EXISTS (SELECT 1 FROM deployments d WHERE d.project_id=i.project_id AND d.provider_deployment_id=i.deployment_id)", (project_id,))).rowcount
        await conn.commit()
    return n


# ---- claiming ----------------------------------------------------------------------------------------------------
async def reap_stuck(conn: AsyncConnection) -> int:
    """A row whose lease keeps expiring (the worker dies on it every time) must not loop forever: it becomes visible as failed."""
    cur = await conn.execute(
        "UPDATE evidence_inbox SET graph_status='failed', graph_claim_owner=NULL, graph_lease_expires_at=NULL, graph_error='lease expired too often'"
        " WHERE graph_status='claimed' AND graph_lease_expires_at < now() AND graph_attempts >= %s", (MAX_ATTEMPTS,))
    return cur.rowcount


async def claim_rows(conn: AsyncConnection, project_id: UUID, owner: str, *, limit: int = BATCH, lease_seconds: int = LEASE_SECONDS) -> list[dict]:
    await reap_stuck(conn)
    rows = await (await conn.execute(
        "WITH next AS (SELECT id FROM evidence_inbox WHERE project_id=%s AND ((graph_status='pending' AND graph_next_attempt_at <= now())"
        "   OR (graph_status='claimed' AND graph_lease_expires_at < now())) ORDER BY ingested_at, id FOR UPDATE SKIP LOCKED LIMIT %s)"
        " UPDATE evidence_inbox i SET graph_status='claimed', graph_claim_owner=%s, graph_lease_expires_at = now() + make_interval(secs => %s),"
        " graph_attempts = i.graph_attempts + 1 FROM next WHERE i.id = next.id RETURNING i.*",
        (project_id, limit, owner, lease_seconds))).fetchall()
    await conn.commit()
    rows.sort(key=lambda r: (r["ingested_at"], r["id"]))
    return rows


async def resolve_scopes(conn: AsyncConnection, project_id: UUID, rows: list[dict]) -> None:
    """Decide, once per row, which service and deployment it belongs to, and keep the decision on the row. A retry then writes exactly the
    same node even if a deployment row appeared in between. Nothing is guessed: no mapping means no service (and no edge)."""
    need = [r for r in rows if r["graph_scope"] is None or r["graph_scope"].get("relink")]
    if not need:
        return
    deps = {d["provider_deployment_id"]: d for d in await (await conn.execute(
        "SELECT provider_deployment_id, provider_project_id, project_name FROM deployments WHERE project_id=%s AND provider_deployment_id = ANY(%s)",
        (project_id, sorted({r["deployment_id"] for r in need if r["deployment_id"]})))).fetchall()}
    scope = {(s["connection_id"], s["provider_resource_id"]): s["name"] for s in await (await conn.execute(
        "SELECT connection_id, provider_resource_id, name FROM log_scope_entries WHERE project_id=%s", (project_id,))).fetchall()}
    names = {d["provider_project_id"]: d["project_name"] for d in await (await conn.execute(
        "SELECT DISTINCT ON (provider_project_id) provider_project_id, project_name FROM deployments WHERE project_id=%s AND project_name IS NOT NULL"
        " ORDER BY provider_project_id, created_at_provider DESC NULLS LAST", (project_id,))).fetchall()}
    declared = {c["id"]: c["svc"] for c in await (await conn.execute(
        "SELECT id, config->>'service_key' AS svc FROM connections WHERE project_id=%s AND config ? 'service_key'", (project_id,))).fetchall()}
    for r in need:
        sc: dict[str, Any] = {"service_key": None, "service_name": None, "service_via": None, "deployment": None}
        if r["graph_scope"] is not None:
            sc["relink"] = True
        d = deps.get(r["deployment_id"]) if r["deployment_id"] else None
        pp = r["provider_project_id"]
        if d:
            sc.update(service_key=d["project_name"] or d["provider_project_id"], service_name=d["project_name"], service_via="deployment_metadata", deployment=r["deployment_id"])
        elif pp and (r["connection_id"], pp) in scope:
            nm = names.get(pp) or scope[(r["connection_id"], pp)]
            sc.update(service_key=nm or pp, service_name=nm, service_via="log_scope")
        elif declared.get(r["connection_id"]):
            sc.update(service_key=declared[r["connection_id"]], service_name=declared[r["connection_id"]], service_via="source_scope")
        await conn.execute("UPDATE evidence_inbox SET graph_scope=%s::jsonb WHERE id=%s AND (graph_scope IS NULL OR graph_scope->>'service_via' IS NULL)",
                           (json.dumps(sc), r["id"]))
        r["graph_scope"] = sc
    await conn.commit()


async def collect(conn: AsyncConnection, project_id: UUID, rows: list[dict], gaps: list[dict]) -> EvidenceBundle:
    proj = await (await conn.execute("SELECT id, organization_id, name FROM projects WHERE id=%s", (project_id,))).fetchone()
    envs = {str(e["id"]): {"environment_key": e["environment_key"], "name": e["name"]} for e in await (await conn.execute(
        "SELECT id, environment_key, name FROM environments WHERE project_id=%s", (project_id,))).fetchall()}
    wanted = sorted({(r["graph_scope"] or {}).get("deployment") for r in rows} - {None})
    deps = {d["provider_deployment_id"]: dict(d) for d in await (await conn.execute(
        "SELECT provider_deployment_id, provider_project_id, project_name, target, state, commit_sha, revision_mapping, created_at_provider, ready_at, environment_id"
        " FROM deployments WHERE project_id=%s AND provider_deployment_id = ANY(%s)", (project_id, wanted))).fetchall()} if wanted else {}
    return EvidenceBundle(str(proj["id"]), str(proj["organization_id"]), proj["name"], envs, rows, deps, gaps)


# ---- acknowledgement ---------------------------------------------------------------------------------------------
async def acknowledge(conn: AsyncConnection, ids: list[UUID], owner: str) -> int:
    """Only after Neo4j committed. A row whose lease moved to another worker is left to that worker (its replay is idempotent)."""
    if not ids:
        return 0
    cur = await conn.execute(
        "UPDATE evidence_inbox SET graph_status='applied', graph_applied_at=now(), graph_claim_owner=NULL, graph_lease_expires_at=NULL, graph_error=NULL"
        " WHERE id = ANY(%s) AND graph_status='claimed' AND graph_claim_owner=%s", (ids, owner))
    await conn.commit()
    return cur.rowcount


async def release(conn: AsyncConnection, ids: list[UUID], owner: str, error: str, *, permanent: bool = False) -> None:
    """Graph write failed: back to pending with backoff, or failed once attempts are used up (or the ontology refused the row)."""
    if not ids:
        return
    await conn.execute(
        "UPDATE evidence_inbox SET graph_status = CASE WHEN %s OR graph_attempts >= %s THEN 'failed' ELSE 'pending' END, graph_claim_owner=NULL, graph_lease_expires_at=NULL,"
        " graph_error=%s, graph_next_attempt_at = now() + make_interval(secs => LEAST(300, 5 * power(2, GREATEST(graph_attempts - 1, 0))))"
        " WHERE id = ANY(%s) AND graph_status='claimed' AND graph_claim_owner=%s", (permanent, MAX_ATTEMPTS, error[:300], ids, owner))
    await conn.commit()


async def retry_failed(conn: AsyncConnection, project_id: UUID) -> int:
    cur = await conn.execute(
        "UPDATE evidence_inbox SET graph_status='pending', graph_attempts=0, graph_error=NULL, graph_next_attempt_at=now() WHERE project_id=%s AND graph_status='failed'", (project_id,))
    await conn.commit()
    return cur.rowcount


# ---- the cursor --------------------------------------------------------------------------------------------------
async def graph_state(conn: AsyncConnection, project_id: UUID, connection_id: UUID | None = None) -> dict[str, Any]:
    """Honest sync state. `acknowledged_through` is the ingestion cursor: every row TRACE received before it is in the graph. It sits at the
    oldest row the graph does not have yet (pending, claimed or failed), so it cannot advance before the graph acknowledges, and it equals
    `received_through` only when nothing is outstanding."""
    where, params = "project_id=%s", [project_id]
    if connection_id:
        where += " AND connection_id=%s"
        params.append(connection_id)
    rows = await (await conn.execute(
        f"SELECT graph_status, count(*) AS n, min(ingested_at) AS oldest, max(ingested_at) AS newest FROM evidence_inbox WHERE {where} GROUP BY graph_status", params)).fetchall()
    by = {r["graph_status"]: r for r in rows}
    n = {k: (by[k]["n"] if k in by else 0) for k in ("pending", "claimed", "applied", "failed")}
    outstanding = [by[k]["oldest"] for k in ("pending", "claimed", "failed") if k in by]
    newest = [r["newest"] for r in rows]
    received = max(newest) if newest else None
    oldest_out = min(outstanding) if outstanding else None
    return {**n, "received_through": received, "oldest_unacknowledged_at": oldest_out, "acknowledged_through": oldest_out or received,
            "unacknowledged": n["pending"] + n["claimed"] + n["failed"]}


# ---- orchestration -----------------------------------------------------------------------------------------------
async def _isolate(repo: GraphRepository, bundle: EvidenceBundle) -> tuple[list[UUID], list[tuple[UUID, str]]]:
    """A batch the ontology refused: find the rows that are fine and the ones that are not, so one bad row cannot hold back the rest."""
    ok: list[UUID] = []
    bad: list[tuple[UUID, str]] = []
    for r in bundle.rows:
        try:
            await asyncio.to_thread(project_evidence, repo, bundle.subset([r]))
            ok.append(r["id"])
        except (GraphValidationError, GraphWriteError) as e:
            bad.append((r["id"], f"{type(e).__name__}: {e}"[:300]))
    return ok, bad


async def apply_pending(c: Container, organization_id: UUID, project_id: UUID, owner: str, *, max_batches: int = MAX_BATCHES, check_cancel=None) -> dict[str, Any]:
    """Claim bounded batches, write them to Neo4j, acknowledge. Returns counts; `remaining` > 0 means more is due and the caller should go again."""
    driver = c.extras.get("neo4j")
    if driver is None:
        raise RuntimeError("graph not configured")
    repo = GraphRepository(driver, c.settings.neo4j_database)
    total = ApplyReport()
    out: dict[str, Any] = {"applied": 0, "failed": 0, "batches": 0, "relinked": await relink_unlinked(c, project_id)}
    for _ in range(max_batches):
        if check_cancel:
            await check_cancel()
        async with c.pool.connection() as conn:
            rows = await claim_rows(conn, project_id, owner, limit=BATCH)
        if not rows:
            break
        await link_unknown_deployments(c, project_id, {r["deployment_id"] for r in rows if r["deployment_id"] and (r["graph_scope"] is None or r["graph_scope"].get("relink"))})
        async with c.pool.connection() as conn:
            await resolve_scopes(conn, project_id, rows)
            bundle = await collect(conn, project_id, rows, [])
        ids = [r["id"] for r in rows]
        try:
            total.add(await asyncio.to_thread(project_evidence, repo, bundle))
            ok, bad = ids, []
        except (GraphValidationError, GraphWriteError):
            ok, bad = await _isolate(repo, bundle)
        except Exception as e:  # noqa: BLE001 - driver/network failure: the whole batch goes back for a retry; the class name is the only detail kept
            async with c.pool.connection() as conn:
                await release(conn, ids, owner, type(e).__name__)
            raise
        async with c.pool.connection() as conn:
            out["applied"] += await acknowledge(conn, ok, owner)
            for rid, msg in bad:
                await release(conn, [rid], owner, msg, permanent=True)
            out["failed"] += len(bad)
        out["batches"] += 1
    out["gaps"] = await apply_gaps(c, organization_id, project_id, repo)
    async with c.pool.connection() as conn:
        out["remaining"] = (await (await conn.execute(
            "SELECT count(*) AS n FROM evidence_inbox WHERE project_id=%s AND ((graph_status='pending' AND graph_next_attempt_at <= now())"
            " OR (graph_status='claimed' AND graph_lease_expires_at < now()))", (project_id,))).fetchone())["n"]
    out["report"] = {k: getattr(total, k) for k in total.__dataclass_fields__}
    return out


async def apply_gaps(c: Container, organization_id: UUID, project_id: UUID, repo: GraphRepository) -> int:
    """Intake requests that lost or damaged input become EvidenceGap nodes. Idempotent (deterministic ids); marked done after the graph commit."""
    async with c.pool.connection() as conn:
        batches = await (await conn.execute(
            f"SELECT * FROM ingestion_batches WHERE project_id=%s AND gaps_applied_at IS NULL AND ({GAP_COLUMNS}) ORDER BY created_at LIMIT 200", (project_id,))).fetchall()
        if not batches:
            return 0
        bundle = await collect(conn, project_id, [], batches)
    rep = await asyncio.to_thread(project_evidence, repo, bundle)
    async with c.pool.connection() as conn:
        await conn.execute("UPDATE ingestion_batches SET gaps_applied_at=now() WHERE id = ANY(%s)", ([b["id"] for b in batches],))
        await conn.commit()
    return rep.gaps


async def request_apply(conn: AsyncConnection, c: Container, organization_id: UUID, project_id: UUID) -> bool:
    """Queue one applier job for the project (the job row is committed in the caller's transaction, next to the receipt it serves).
    No Neo4j configured: nothing is queued and rows stay pending, which the coverage endpoints report. Duplicate queued jobs are not created."""
    if c.extras.get("neo4j") is None:
        return False
    queued = await (await conn.execute("SELECT id FROM jobs WHERE kind='graph.apply_evidence' AND project_id=%s AND status='queued' LIMIT 1", (project_id,))).fetchone()
    if queued:
        return True
    await queue.enqueue(conn, organization_id=organization_id, project_id=project_id, kind="graph.apply_evidence", payload={"project_id": str(project_id)}, priority=queue.PRIORITY_INGEST)
    return True
