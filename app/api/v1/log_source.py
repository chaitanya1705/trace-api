"""'Send logs from your app' (no hosting plan needed): the monitored application pushes its own structured events to TRACE.
One signed endpoint per project; the signing secret is sealed, shown once and bound to that project's source."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from psycopg import AsyncConnection
from pydantic import BaseModel, Field

from app.audit import audit
from app.auth.authz import Cap
from app.auth.deps import ProjectContext, get_conn, get_container, project_access
from app.container import Container
from app.domain.ingest.app_events import normalize_app_event
from app.domain.ingest.store import env_lookup, store_rows
from app.domain.ingest.vercel_logs import MAX_EVENTS_PER_BATCH, parse_body
from app.envelope import wrap
from app.errors import Conflict, DependencyUnavailable, NotFound, TraceError, Unauthenticated, Unprocessable
from app.storage.secrets import Sealed

router = APIRouter(tags=["log-source"])
KIND = "ingest_secret"
SIG_HEADER = "x-trace-signature"


class PayloadTooLarge(TraceError):
    status, code = 413, "payload_too_large"


class SourceIn(BaseModel):
    environment_key: str | None = Field(None, max_length=40)  # applied to events that carry no environment of their own


def endpoint_url(c: Container, connection_id: UUID) -> str:
    return f"{c.settings.trace_public_origin.rstrip('/')}/api/v1/ingest/events/{connection_id}"


def signature_ok(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    return hmac.compare_digest("sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(), header.strip().lower())


FORWARDER = '''"""Forwards each request's diagnostics events to TRACE. Off unless both variables are set; never raises into the request path."""
import asyncio
import hashlib
import hmac
import json
import os
import urllib.request


def _post(url: str, secret: str, events: list[dict]) -> None:
    body = json.dumps({"events": events}, default=str).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"content-type": "application/json", "x-trace-signature": signature, "user-agent": "app-trace-forwarder/1"},
    )
    urllib.request.urlopen(request, timeout=2).read()


async def forward(events) -> None:
    url, secret = os.environ.get("TRACE_INGEST_URL"), os.environ.get("TRACE_INGEST_SECRET")
    if not (url and secret and events):
        return
    try:
        await asyncio.to_thread(_post, url, secret, [e.model_dump(mode="json") for e in events])
    except Exception:
        pass  # logging to TRACE must never break or slow a request beyond its short timeout
'''

HOOK = '''# app/main.py: in the request_context middleware's `finally:` block, send this request's events before the context ends
#   from app.observability.forward import forward
#   ...
#       finally:
#           await forward(ctx.events)
#           context.end(token)
'''


async def _secret(conn: AsyncConnection, c: Container, connection_id: UUID) -> str | None:
    row = await (await conn.execute("SELECT * FROM connection_secrets WHERE connection_id=%s AND kind=%s", (connection_id, KIND))).fetchone()
    if row is None or c.vault is None:
        return None
    return c.vault.open(Sealed(row["key_version"], bytes(row["nonce"]), bytes(row["ciphertext"])), connection_id=str(connection_id), version=row["version"], kind=KIND)


async def _source(conn: AsyncConnection, project_id: UUID) -> dict | None:
    return await (await conn.execute("SELECT * FROM connections WHERE project_id=%s AND provider='generic' AND kind='telemetry' AND status<>'disconnected'", (project_id,))).fetchone()


async def _new_secret(conn: AsyncConnection, c: Container, row: dict) -> str:
    if c.vault is None:
        raise DependencyUnavailable("Credential vault is not configured")
    sec = secrets.token_hex(24)
    ver = (await (await conn.execute("SELECT COALESCE(max(version),0)+1 AS v FROM connection_secrets WHERE connection_id=%s AND kind=%s", (row["id"], KIND))).fetchone())["v"]
    sealed = c.vault.seal(sec, connection_id=str(row["id"]), version=ver, kind=KIND)
    await conn.execute(
        "INSERT INTO connection_secrets(organization_id,connection_id,kind,version,key_version,nonce,ciphertext) VALUES (%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (connection_id, kind) DO UPDATE SET version=EXCLUDED.version, key_version=EXCLUDED.key_version, nonce=EXCLUDED.nonce, ciphertext=EXCLUDED.ciphertext, created_at=now()",
        (row["organization_id"], row["id"], KIND, ver, sealed.key_version, sealed.nonce, sealed.ciphertext))
    return sec


async def _view(conn: AsyncConnection, c: Container, project_id: UUID, row: dict | None, *, new_secret: str | None = None) -> dict:
    if row is None:
        return {"configured": False, "forwarder_snippet": FORWARDER, "hook_snippet": HOOK, "accepted_fields": "event_id, event_time, message, level, request_id, run_id, component_id, operation, event_name, duration_ms, error_type, source_revision, deployment_id"}
    has = await (await conn.execute("SELECT 1 FROM connection_secrets WHERE connection_id=%s AND kind=%s", (row["id"], KIND))).fetchone()
    last = await (await conn.execute("SELECT created_at, received, accepted, duplicates, rejected FROM ingestion_batches WHERE connection_id=%s ORDER BY created_at DESC LIMIT 1", (row["id"],))).fetchone()
    n = (await (await conn.execute("SELECT count(*) AS n FROM evidence_inbox WHERE connection_id=%s", (row["id"],))).fetchone())["n"]
    return {"configured": True, "connection_id": row["id"], "endpoint_url": endpoint_url(c, row["id"]), "signature_header": f"{SIG_HEADER}: sha256=<hex HMAC-SHA256 of the raw request body>",
            "environment_key": (row["config"] or {}).get("environment_key"), "secret_configured": has is not None, "secret": new_secret, "secret_shown_once": new_secret is not None,
            "last_batch_at": last["created_at"] if last else None, "last_batch": dict(last) if last else None, "stored_events": n,
            "forwarder_snippet": FORWARDER, "hook_snippet": HOOK,
            "accepted_fields": "event_id, event_time, message, level, request_id, run_id, component_id, operation, event_name, duration_ms, error_type, source_revision, deployment_id"}


@router.get("/projects/{project_id}/log-source")
async def get_source(request: Request, ctx: ProjectContext = Depends(project_access(Cap.READ)), conn: AsyncConnection = Depends(get_conn), c: Container = Depends(get_container)):
    return wrap(request, await _view(conn, c, ctx.project_id, await _source(conn, ctx.project_id)))


@router.post("/projects/{project_id}/log-source")
async def create_source(body: SourceIn, request: Request, ctx: ProjectContext = Depends(project_access(Cap.MANAGE_CONNECTIONS)), conn: AsyncConnection = Depends(get_conn), c: Container = Depends(get_container)):
    """Create (or reconfigure) the project's app-log source and generate its signing secret, shown once. Calling again rotates the secret."""
    env_key = body.environment_key.strip().lower() if body.environment_key else None
    if env_key and env_key not in await env_lookup(conn, ctx.project_id):
        raise Unprocessable(f"Unknown environment: {env_key}")
    row = await _source(conn, ctx.project_id)
    if row is None:
        row = await (await conn.execute(
            "INSERT INTO connections(organization_id,project_id,provider,kind,auth_method,status,config,created_by,last_verified_at) VALUES (%s,%s,'generic','telemetry','signed_webhook','connected',%s::jsonb,%s,now()) RETURNING *",
            (ctx.org.organization_id, ctx.project_id, json.dumps({"environment_key": env_key}), ctx.org.principal.user_id))).fetchone()
    else:
        row = await (await conn.execute("UPDATE connections SET config = config || %s::jsonb WHERE id=%s RETURNING *", (json.dumps({"environment_key": env_key}), row["id"]))).fetchone()
    sec = await _new_secret(conn, c, row)
    await audit(conn, organization_id=ctx.org.organization_id, project_id=ctx.project_id, actor=ctx.org.principal.user_id, action="log_source.secret", target_type="connection", target_id=row["id"], request=request)
    await conn.commit()
    return wrap(request, await _view(conn, c, ctx.project_id, row, new_secret=sec))


@router.post("/ingest/events/{connection_id}")
async def ingest_events(connection_id: UUID, request: Request, conn: AsyncConnection = Depends(get_conn), c: Container = Depends(get_container)):
    """Public on purpose: the application posts here. The HMAC-SHA256 signature is the authentication; the answer acknowledges durable receipt."""
    raw = await request.body()
    if len(raw) > c.settings.ingest_max_body_bytes:
        raise PayloadTooLarge("Batch is too large")
    row = await (await conn.execute("SELECT * FROM connections WHERE id=%s AND provider='generic' AND kind='telemetry'", (connection_id,))).fetchone()
    secret = await _secret(conn, c, connection_id) if row else None
    if row is None or secret is None:
        raise NotFound("Unknown source")
    if not signature_ok(secret, raw, request.headers.get(SIG_HEADER)):
        raise Unauthenticated("Invalid signature", code="invalid_signature")
    if row["status"] != "connected" or row["project_id"] is None:
        raise Conflict("This source is not active", code="source_not_active")
    items = parse_body(raw)
    if len(items) == 1 and isinstance(items[0], dict) and isinstance(items[0].get("events"), list):
        items = items[0]["events"]
        if len(items) > MAX_EVENTS_PER_BATCH:
            raise Unprocessable(f"Batch has more than {MAX_EVENTS_PER_BATCH} events", code="batch_too_large")
    envs = await env_lookup(conn, row["project_id"])
    default_env = (row["config"] or {}).get("environment_key")
    rows, rejected = [], 0
    for it in items:
        r = normalize_app_event(it, default_env)
        if r is None:
            rejected += 1
            continue
        rows.append((r, envs.get(r.env_key) if r.env_key else None))
    res = await store_rows(conn, organization_id=row["organization_id"], project_id=row["project_id"], connection_id=connection_id, provider="app_push", rows=rows)
    await conn.execute("INSERT INTO ingestion_batches(organization_id,project_id,connection_id,received,accepted,duplicates,out_of_scope,rejected) VALUES (%s,%s,%s,%s,%s,%s,0,%s)",
                       (row["organization_id"], row["project_id"], connection_id, len(items), res.accepted, res.duplicates, rejected))
    await conn.commit()
    return JSONResponse({"received": len(items), "accepted": res.accepted, "duplicates": res.duplicates, "rejected": rejected})
