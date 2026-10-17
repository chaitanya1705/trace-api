"""Logs (R-018): Vercel log-drain intake, explicit live capture, stored search and freshness. Everything shown here is stored first;
nothing is displayed straight from a provider response. Rows are stored with graph_status=pending and become 'applied' only after the graph
applier (app/domain/ingest/graph_apply.py) has written them to Neo4j; every coverage block reports pending vs applied honestly."""
from __future__ import annotations

import secrets
from datetime import datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from psycopg import AsyncConnection
from pydantic import BaseModel, Field

from app.api.v1.connections import load_authorized
from app.audit import audit
from app.auth.authz import Cap
from app.auth.deps import OrgContext, ProjectContext, get_conn, get_container, org_context, project_access
from app.container import Container
from app.domain.ingest import vercel_logs as vl
from app.domain.ingest.graph_apply import graph_state
from app.domain.ingest.intake import finish
from app.domain.ingest.store import env_lookup, store_rows
from app.envelope import wrap
from app.errors import Conflict, DependencyUnavailable, NotFound, TraceError, Unauthenticated, Unprocessable
from app.jobs import queue
from app.pagination import clamp_limit, decode_cursor, encode_cursor
from app.storage.secrets import Sealed

router = APIRouter(tags=["logs"])
DRAIN_KIND = "drain_secret"


class PayloadTooLarge(TraceError):
    status, code = 413, "payload_too_large"


def drain_url(c: Container, connection_id: UUID) -> str:
    return f"{c.settings.trace_public_origin.rstrip('/')}/api/v1/ingest/vercel/{connection_id}"


async def _drain_secret(conn: AsyncConnection, c: Container, connection_id: UUID) -> str | None:
    row = await (await conn.execute("SELECT * FROM connection_secrets WHERE connection_id=%s AND kind=%s", (connection_id, DRAIN_KIND))).fetchone()
    if row is None or c.vault is None:
        return None
    return c.vault.open(Sealed(row["key_version"], bytes(row["nonce"]), bytes(row["ciphertext"])), connection_id=str(connection_id), version=row["version"], kind=DRAIN_KIND)


# --- drain setup ---------------------------------------------------------------------------------------------------

@router.post("/connections/{connection_id}/drain/secret")
async def make_drain_secret(connection_id: UUID, request: Request, org: OrgContext = Depends(org_context), conn: AsyncConnection = Depends(get_conn), c: Container = Depends(get_container)):
    """(Re)generate the drain signing secret. It is shown once: paste it into Vercel's Drain 'signature secret' together with the URL."""
    row = await load_authorized(conn, org, connection_id, Cap.MANAGE_CONNECTIONS)
    if row["provider"] != "vercel":
        raise Unprocessable("Log drains are for Vercel connections")
    if c.vault is None:
        raise DependencyUnavailable("Credential vault is not configured")
    secret = secrets.token_hex(24)
    ver = (await (await conn.execute("SELECT COALESCE(max(version),0)+1 AS v FROM connection_secrets WHERE connection_id=%s AND kind=%s", (connection_id, DRAIN_KIND))).fetchone())["v"]
    sealed = c.vault.seal(secret, connection_id=str(connection_id), version=ver, kind=DRAIN_KIND)
    await conn.execute(
        "INSERT INTO connection_secrets(organization_id,connection_id,kind,version,key_version,nonce,ciphertext) VALUES (%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (connection_id, kind) DO UPDATE SET version=EXCLUDED.version, key_version=EXCLUDED.key_version, nonce=EXCLUDED.nonce, ciphertext=EXCLUDED.ciphertext, created_at=now()",
        (row["organization_id"], connection_id, DRAIN_KIND, ver, sealed.key_version, sealed.nonce, sealed.ciphertext))
    await audit(conn, organization_id=row["organization_id"], project_id=row["project_id"], actor=org.principal.user_id, action="connection.drain_secret", target_type="connection",
                target_id=connection_id, request=request)
    await conn.commit()
    return wrap(request, {"endpoint_url": drain_url(c, connection_id), "secret": secret, "signature_header": "x-vercel-signature (HMAC-SHA1, hex)",
                          "format": "JSON or NDJSON", "shown_once": True,
                          "reachable_note": "Vercel must be able to reach this URL. For a local TRACE use a tunnel and set TRACE_PUBLIC_ORIGIN to its address."})


@router.get("/connections/{connection_id}/drain")
async def drain_status(connection_id: UUID, request: Request, org: OrgContext = Depends(org_context), conn: AsyncConnection = Depends(get_conn), c: Container = Depends(get_container)):
    row = await load_authorized(conn, org, connection_id, Cap.READ)
    has = await (await conn.execute("SELECT created_at FROM connection_secrets WHERE connection_id=%s AND kind=%s", (connection_id, DRAIN_KIND))).fetchone()
    last = await (await conn.execute(
        "SELECT b.id, b.created_at, b.received, b.accepted, b.rejected, b.reason, b.truncated, b.unmapped,"
        " (SELECT COALESCE(jsonb_object_agg(g.graph_status, g.n), '{}'::jsonb) FROM (SELECT graph_status, count(*) AS n FROM evidence_inbox WHERE batch_id=b.id GROUP BY graph_status) g) AS graph"
        " FROM ingestion_batches b WHERE b.connection_id=%s AND b.via='drain' ORDER BY b.created_at DESC LIMIT 1", (connection_id,))).fetchone()
    tot = await (await conn.execute("SELECT count(*) AS n FROM evidence_inbox WHERE connection_id=%s", (connection_id,))).fetchone()
    return wrap(request, {"provider": row["provider"], "secret_configured": has is not None, "endpoint_url": drain_url(c, connection_id),
                          "last_batch_at": last["created_at"] if last else None, "last_batch": dict(last) if last else None, "stored_events": tot["n"],
                          "graph": {"configured": c.extras.get("neo4j") is not None, **await graph_state(conn, row["project_id"], connection_id)} if row["project_id"] else None})


# --- intake --------------------------------------------------------------------------------------------------------

@router.post("/ingest/vercel/{connection_id}", status_code=200)
async def ingest_vercel(connection_id: UUID, request: Request, conn: AsyncConnection = Depends(get_conn), c: Container = Depends(get_container)):
    """Public on purpose: Vercel posts here. The HMAC signature is the authentication. The response acknowledges durable receipt, not analysis."""
    raw = await request.body()
    row = await (await conn.execute("SELECT * FROM connections WHERE id=%s AND provider='vercel'", (connection_id,))).fetchone()
    secret = await _drain_secret(conn, c, connection_id) if row else None
    if row is None or secret is None:
        raise NotFound("Unknown drain")  # same answer for 'no such connection' and 'no secret yet': nothing is revealed
    if not vl.signature_ok(secret, raw, request.headers.get("x-vercel-signature")):
        raise Unauthenticated("Invalid signature", code="invalid_signature")
    if row["status"] not in ("connected", "connected_with_warnings") or row["project_id"] is None:
        raise Conflict("This connection is not active", code="connection_not_ready")
    # From here the sender is authenticated: whatever is refused or damaged is accounted for (and becomes a coverage gap in the graph).
    if len(raw) > c.settings.ingest_max_body_bytes:
        await finish(conn, c, row, via="drain", received=0, reason="oversize_body", body_bytes=len(raw))
        raise PayloadTooLarge("Batch is too large")
    try:
        records = vl.parse_body(raw)
    except Unprocessable as e:
        await finish(conn, c, row, via="drain", received=0, reason="batch_too_large" if e.code == "batch_too_large" else "malformed_payload", body_bytes=len(raw))
        raise
    envs = await env_lookup(conn, row["project_id"])
    scope = await (await conn.execute(
        "SELECT l.provider_resource_id, e.environment_key FROM log_scope_entries l JOIN environments e ON e.id=l.environment_id WHERE l.connection_id=%s", (connection_id,))).fetchall()
    by_project: dict[str, list[str]] = {}
    for s in scope:
        by_project.setdefault(s["provider_resource_id"], []).append(s["environment_key"])
    rows, rejected, out = [], 0, 0
    for rec in records:
        r = vl.normalize(rec)
        if r is None:
            rejected += 1
            continue
        mapped = by_project.get(r.provider_project_id)
        if not mapped:
            out += 1  # a Vercel project that is not in this project's log scope is never stored
            continue
        key = r.env_key if r.env_key in envs and r.env_key in mapped else (mapped[0] if len(mapped) == 1 else None)
        rows.append((r, envs.get(key) if key else None))
    batch_id = uuid4()
    res = await store_rows(conn, organization_id=row["organization_id"], project_id=row["project_id"], connection_id=connection_id, provider="vercel", rows=rows, batch_id=batch_id)
    await finish(conn, c, row, via="drain", received=len(records), res=res, out_of_scope=out, rejected=rejected, body_bytes=len(raw), batch_id=batch_id)
    return JSONResponse({"received": len(records), "accepted": res.accepted, "duplicates": res.duplicates, "out_of_scope": out, "rejected": rejected})


# --- capture, search, status ---------------------------------------------------------------------------------------

class CaptureIn(BaseModel):
    environment: str | None = Field(None, max_length=40)
    seconds: int = Field(30, ge=5, le=60)


@router.post("/projects/{project_id}/logs/capture", status_code=202)
async def capture(body: CaptureIn, request: Request, ctx: ProjectContext = Depends(project_access(Cap.INDEX)), conn: AsyncConnection = Depends(get_conn)):
    """Explicit 'Fetch from source': a bounded, read-only live capture. It stores what arrives during the window; it is not history."""
    host = await (await conn.execute("SELECT id, status FROM connections WHERE project_id=%s AND provider='vercel' AND kind='hosting' AND status<>'disconnected'", (ctx.project_id,))).fetchone()
    if host is None or host["status"] not in ("connected", "connected_with_warnings"):
        raise Conflict("Connect and verify Vercel first.", code="no_hosting")
    job = await queue.enqueue(conn, organization_id=ctx.org.organization_id, project_id=ctx.project_id, kind="logs.capture", requested_by=ctx.org.principal.user_id,
                              payload={"connection_id": str(host["id"]), "environment": body.environment, "seconds": body.seconds}, resource_type="connection", resource_id=host["id"],
                              priority=queue.PRIORITY_HUMAN)
    await conn.commit()
    return wrap(request, {"job_id": job["id"], "resource_id": host["id"], "status": job["status"], "events_url": f"/api/v1/jobs/{job['id']}"})


def _dt(v: str | None, name: str) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise Unprocessable(f"{name} must be an ISO time") from None


@router.get("/projects/{project_id}/logs")
async def search_logs(
    request: Request, environment: str | None = Query(None, max_length=40), level: str | None = Query(None, pattern="^(info|warning|error|fatal)$"),
    q: str | None = Query(None, max_length=200), request_id: str | None = Query(None, max_length=100), deployment_id: str | None = Query(None, max_length=100),
    service_hint: str | None = Query(None, max_length=200), since: str | None = None, until: str | None = None,
    limit: int | None = Query(None, ge=1, le=100), cursor: str | None = None,
    ctx: ProjectContext = Depends(project_access(Cap.READ)), conn: AsyncConnection = Depends(get_conn), c: Container = Depends(get_container),
):
    """Stored logs only (filters are TRACE filters, not provider query syntax). 'Nothing stored' and 'nothing matched' are told apart."""
    n = clamp_limit(limit)
    cur = decode_cursor(cursor)
    where, params = ["i.project_id = %s"], [ctx.project_id]
    if environment:
        where.append("e.environment_key = %s")
        params.append(environment.strip().lower())
    if level:
        where.append("i.level = %s")
        params.append(level)
    if request_id:
        where.append("i.request_id = %s")
        params.append(request_id)
    if deployment_id:
        where.append("i.deployment_id = %s")
        params.append(deployment_id)
    if service_hint:
        # Ticket hints commonly include the HTTP method while ShopFlow log rows store the path only.
        parts = service_hint.split(" ", 1)
        if len(parts) == 2 and parts[0] in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"} and parts[1].startswith("/"):
            where.append("i.service_hint IN (%s, %s)")
            params.extend((service_hint, parts[1]))
        else:
            where.append("i.service_hint = %s")
            params.append(service_hint)
    if q:
        like = "%" + q.strip().lower().replace("%", r"\%").replace("_", r"\_") + "%"
        where.append("(lower(i.message) LIKE %s OR lower(coalesce(i.service_hint,'')) LIKE %s OR lower(coalesce(i.request_id,'')) LIKE %s)")
        params += [like, like, like]
    if t := _dt(since, "since"):
        where.append("COALESCE(i.event_time, i.ingested_at) >= %s")
        params.append(t)
    if t := _dt(until, "until"):
        where.append("COALESCE(i.event_time, i.ingested_at) < %s")
        params.append(t)
    base = " AND ".join(where)
    if cur:
        where.append("(COALESCE(i.event_time, i.ingested_at), i.id) < (%s, %s)")
        params += [cur["t"], UUID(cur["id"])]
    sql_from = "FROM evidence_inbox i LEFT JOIN environments e ON e.id = i.environment_id"
    rows = await (await conn.execute(
        f"SELECT i.id, i.event_time, i.ingested_at, COALESCE(i.event_time, i.ingested_at) AS sort_t, i.level, i.source, i.service_hint, i.message, i.request_id, i.status_code, i.deployment_id,"
        f" e.environment_key, i.extra, i.graph_status, i.provider {sql_from} WHERE {' AND '.join(where)} ORDER BY sort_t DESC, i.id DESC LIMIT %s", (*params, n + 1))).fetchall()
    more = len(rows) > n
    rows = rows[:n]
    total_all = (await (await conn.execute("SELECT count(*) AS n FROM evidence_inbox WHERE project_id=%s", (ctx.project_id,))).fetchone())["n"]
    matched = (await (await conn.execute(f"SELECT count(*) AS n {sql_from} WHERE {base}", params[: len(params) - (2 if cur else 0)])).fetchone())["n"]
    gstate = {"configured": c.extras.get("neo4j") is not None, **await graph_state(conn, ctx.project_id)}
    pend = gstate["unacknowledged"]
    src = await (await conn.execute(
        "SELECT (SELECT count(*) FROM connections WHERE project_id=%s AND provider='generic' AND kind='telemetry' AND status='connected') AS app_sources,"
        " (SELECT count(*) FROM log_scope_entries WHERE project_id=%s) AS hosting_resources", (ctx.project_id, ctx.project_id))).fetchone()
    out = [{**{k: r[k] for k in ("id", "event_time", "ingested_at", "level", "source", "service_hint", "request_id", "status_code", "deployment_id", "environment_key", "extra", "graph_status", "provider")},
            "trust_class": "untrusted_browser" if r["source"] == "browser" else "app_reported" if r["provider"] == "app_push" else "provider_reported",
            "message": (r["message"] or "")[:2000]} for r in rows]
    nxt = encode_cursor({"t": rows[-1]["sort_t"].isoformat(), "id": str(rows[-1]["id"])}) if more else None
    return wrap(request, out, next_cursor=nxt,
                coverage={"mode": "stored", "stored_total": total_all, "matched": matched, "empty_dataset": total_all == 0, "not_yet_in_graph": pend, "graph": gstate,
                          "source": {"configured": bool(src["app_sources"] or src["hosting_resources"]), "app_sources": src["app_sources"], "hosting_resources": src["hosting_resources"]},
                          "note": ("Stored events received through the log drain, the app log source or a live capture. The investigator reads the graph, and the graph does not have all of them yet."
                                   if pend else None)})


@router.get("/projects/{project_id}/logs/status")
async def logs_status(request: Request, ctx: ProjectContext = Depends(project_access(Cap.READ)), conn: AsyncConnection = Depends(get_conn), c: Container = Depends(get_container)):
    """Per-resource freshness. 'No events yet' is a gap, never healthy; 'available' means events were actually received."""
    res = await (await conn.execute(
        "SELECT l.id, l.name, l.resource_type, l.log_availability, l.availability_note, l.last_event_at, e.environment_key, l.connection_id,"
        " (SELECT count(*) FROM evidence_inbox i WHERE i.connection_id=l.connection_id AND i.provider_project_id=l.provider_resource_id AND i.environment_id=l.environment_id) AS stored,"
        " (SELECT count(*) FROM evidence_inbox i WHERE i.connection_id=l.connection_id AND i.provider_project_id=l.provider_resource_id AND i.environment_id=l.environment_id AND i.graph_status='applied') AS in_graph,"
        " (SELECT min(i.ingested_at) FROM evidence_inbox i WHERE i.connection_id=l.connection_id AND i.provider_project_id=l.provider_resource_id AND i.environment_id=l.environment_id AND i.graph_status<>'applied') AS oldest_not_in_graph"
        " FROM log_scope_entries l JOIN environments e ON e.id=l.environment_id WHERE l.project_id=%s ORDER BY e.environment_key, l.name", (ctx.project_id,))).fetchall()
    host = await (await conn.execute("SELECT id, status FROM connections WHERE project_id=%s AND provider='vercel' AND kind='hosting' AND status<>'disconnected'", (ctx.project_id,))).fetchone()
    drain = None
    if host:
        has = await (await conn.execute("SELECT 1 FROM connection_secrets WHERE connection_id=%s AND kind=%s", (host["id"], DRAIN_KIND))).fetchone()
        last = await (await conn.execute("SELECT created_at FROM ingestion_batches WHERE connection_id=%s AND via='drain' ORDER BY created_at DESC LIMIT 1", (host["id"],))).fetchone()
        drain = {"connection_id": host["id"], "secret_configured": has is not None, "endpoint_url": drain_url(c, host["id"]), "last_batch_at": last["created_at"] if last else None}
    job = await (await conn.execute("SELECT id, status, stage, result_ref, error, created_at FROM jobs WHERE project_id=%s AND kind='logs.capture' ORDER BY created_at DESC LIMIT 1", (ctx.project_id,))).fetchone()
    gaps = await (await conn.execute(
        "SELECT id AS batch_id, created_at, via, reason, received, rejected, out_of_scope, truncated, unmapped, body_bytes, gaps_applied_at IS NOT NULL AS in_graph FROM ingestion_batches"
        " WHERE project_id=%s AND (reason IS NOT NULL OR rejected > 0 OR out_of_scope > 0 OR truncated > 0 OR unmapped > 0) ORDER BY created_at DESC LIMIT 20", (ctx.project_id,))).fetchall()
    return wrap(request, {"resources": res, "hosting_connection_id": host["id"] if host else None, "drain": drain,
                          "graph": {"configured": c.extras.get("neo4j") is not None, **await graph_state(conn, ctx.project_id)}, "recent_gaps": gaps,
                          "last_capture": {k: job[k] for k in ("id", "status", "stage", "result_ref", "error", "created_at")} if job else None})
