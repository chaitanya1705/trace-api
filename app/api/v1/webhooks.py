"""Provider webhooks. Acknowledge a durable receipt, never completed analysis (A5)."""
from __future__ import annotations

import hashlib
import hmac
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from psycopg import AsyncConnection

from app.auth.deps import get_conn, get_container
from app.container import Container
from app.errors import DependencyUnavailable, TraceError, Unauthenticated
from app.jobs import queue

router = APIRouter(tags=["webhooks"])
MAX_BODY = 5 * 1024 * 1024


class PayloadTooLarge(TraceError):
    status, code = 413, "payload_too_large"


def github_signature_ok(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


@router.post("/webhooks/github", status_code=202)
async def github_webhook(request: Request, conn: AsyncConnection = Depends(get_conn), c: Container = Depends(get_container)):
    secret = c.settings.github_webhook_secret
    if not secret:
        raise DependencyUnavailable("GitHub webhooks are not configured")
    body = await request.body()
    if len(body) > MAX_BODY:
        raise PayloadTooLarge("Webhook payload too large")
    if not github_signature_ok(secret, body, request.headers.get("x-hub-signature-256")):
        raise Unauthenticated("Invalid webhook signature", code="bad_signature")
    delivery = request.headers.get("x-github-delivery")
    event = request.headers.get("x-github-event", "")
    if not delivery:
        raise Unauthenticated("Missing delivery id", code="bad_delivery")
    # Durable receipt first; a redelivery of the same id is acknowledged and ignored.
    fresh = await (await conn.execute(
        "INSERT INTO webhook_deliveries(provider, delivery_id, outcome) VALUES ('github',%s,%s)"
        " ON CONFLICT (provider, delivery_id) DO NOTHING RETURNING id", (delivery, event))).fetchone()
    if fresh is None:
        await conn.rollback()
        return JSONResponse({"status": "duplicate"}, status_code=202)
    try:
        payload = json.loads(body)
    except ValueError:
        await conn.commit()
        return JSONResponse({"status": "ignored"}, status_code=202)
    handled = "ignored"
    if event == "installation":
        iid = str((payload.get("installation") or {}).get("id", ""))
        action = payload.get("action")
        new_status = {"deleted": "revoked", "suspend": "expired"}.get(action)
        if iid and new_status:
            rows = await (await conn.execute(
                "UPDATE connections SET status=%s, last_error=%s::jsonb WHERE provider='github' AND config->>'installation_id'=%s"
                " AND status NOT IN ('disconnected') RETURNING id", (new_status, json.dumps(
                    {"code": f"installation_{action}", "message": f"GitHub reported the app installation was {action}d."}), iid))).fetchall()
            for r in rows:   # access is gone: stop future work immediately
                await queue.cancel_for_resource(conn, "connection", r["id"])
            handled = f"{len(rows)} connection(s) {new_status}"
        elif iid and action == "unsuspend":
            for r in await (await conn.execute(
                    "SELECT id, organization_id, project_id, created_by FROM connections WHERE provider='github' AND config->>'installation_id'=%s"
                    " AND status='expired'", (iid,))).fetchall():
                await queue.enqueue(conn, organization_id=r["organization_id"], project_id=r["project_id"], kind="connection.verify",
                                    payload={"connection_id": str(r["id"]), "credential_version": None}, requested_by=r["created_by"],
                                    resource_type="connection", resource_id=r["id"], idempotency_key=f"unsuspend:{delivery}")
            handled = "reverify queued"
    await conn.execute("UPDATE webhook_deliveries SET outcome=%s WHERE provider='github' AND delivery_id=%s", (f"{event}: {handled}", delivery))
    await conn.commit()
    return JSONResponse({"status": "received"}, status_code=202)
