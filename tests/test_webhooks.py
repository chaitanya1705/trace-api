import hashlib
import hmac
import json

import pytest

from tests.test_connections import _project, provider  # noqa: F401  (fixtures)

pytestmark = pytest.mark.db
API = "/api/v1"
SECRET = "whsec_fixture"


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


async def post(client, event, payload, *, delivery, signature=None, raw=None):
    body = raw if raw is not None else json.dumps(payload).encode()
    return await client.post(f"{API}/webhooks/github", content=body, headers={
        "x-github-event": event, "x-github-delivery": delivery, "x-hub-signature-256": signature or sign(body)})


async def test_signature_required_and_redelivery_deduped(client, container):
    container.settings.github_webhook_secret = SECRET
    assert (await post(client, "ping", {}, delivery="d-1", signature="sha256=00")).status_code == 401
    assert (await client.post(f"{API}/webhooks/github", content=b"{}", headers={"x-github-delivery": "d-x"})).status_code == 401
    assert (await post(client, "ping", {}, delivery="d-2")).json()["status"] == "received"
    assert (await post(client, "ping", {}, delivery="d-2")).json()["status"] == "duplicate"
    assert (await post(client, "ping", {}, delivery="d-3", raw=b"not json")).json()["status"] == "ignored"


async def test_unconfigured_secret_is_503_not_open(client, container):
    container.settings.github_webhook_secret = None
    assert (await post(client, "ping", {}, delivery="d-9")).status_code == 503


async def test_installation_deleted_revokes_connection_and_stops_jobs(client, container, org, provider):  # noqa: F811
    container.settings.github_webhook_secret = SECRET
    pid, h = await _project(client, org)
    cid = (await client.post(f"{API}/projects/{pid}/connections", headers=h, json={
        "provider": "github", "auth_method": "github_app", "config": {"installation_id": "777"}})).json()["data"]["resource_id"]
    r = await post(client, "installation", {"action": "deleted", "installation": {"id": 777}}, delivery="d-del-1")
    assert r.status_code == 202
    c = (await client.get(f"{API}/connections/{cid}", headers=h)).json()["data"]
    assert c["status"] == "revoked" and c["last_error"]["code"] == "installation_deleted"
    async with container.pool.connection() as conn:
        job = await (await conn.execute("SELECT status FROM jobs WHERE resource_id=%s", (cid,))).fetchone()
    assert job["status"] == "cancelled"                      # queued verify never runs against a revoked installation
