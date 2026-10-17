"""Helpers shared by the evidence-intake tests. Everything here builds FIXTURE data for tests; nothing is importable from app/."""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from tests.conftest import token_for

API = "/api/v1"
T0 = datetime(2026, 10, 16, 18, 30, tzinfo=UTC)


def sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def ev(i: int | str, *, at: datetime | None | str = "default", level: str = "INFO", message: str | None = None, **kw) -> dict:
    """A FIXTURE app event shaped like ShopFlow's diagnostics. `at=None` omits the time (unknown), otherwise ISO time."""
    out = {"event_id": f"evt-{i}", "request_id": kw.pop("rid", f"req-{i}"), "component_id": "checkout", "operation": kw.pop("op", "validate_request"), "level": level,
           "event_name": "operation.started", "message": message if message is not None else f"event {i}"}
    if at == "default":
        at = T0 + timedelta(seconds=int(i) if str(i).isdigit() else 0)
    if at is not None:
        out["event_time"] = at.isoformat()
    out.update(kw)
    return out


async def make_project(client, org, *, name: str = "Ev", services: list[str] | None = None):
    h = token_for(org["owner_id"], org["owner_email"])
    body = {"name": f"{name}{org['id'].hex[:6]}", "environments": [{"name": "production"}, {"name": "staging"}]}
    if services:
        body["services"] = [{"name": s} for s in services]
    p = (await client.post(f"{API}/projects", headers=h, json=body)).json()["data"]
    return p["id"], h


async def make_source(client, org, *, name: str = "Ev", environment_key: str | None = "production", service_key: str | None = None, services: list[str] | None = None):
    """A project with an app log source. Returns (project_id, connection_id, secret, headers)."""
    pid, h = await make_project(client, org, name=name, services=services)
    r = await client.post(f"{API}/projects/{pid}/log-source", headers=h, json={"environment_key": environment_key, "service_key": service_key})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    return pid, d["connection_id"], d["secret"], h


async def push(client, cid: str, secret: str, events: list[dict] | bytes, *, wrap: bool = True):
    body = events if isinstance(events, bytes) else json.dumps({"events": events} if wrap else events).encode()
    return await client.post(f"{API}/ingest/events/{cid}", content=body, headers={"x-trace-signature": sig(secret, body)})


async def rows(container, sql: str, *params):
    async with container.pool.connection() as conn:
        return await (await conn.execute(sql, params)).fetchall()


async def add_deployment(container, org_id, pid, cid, *, dep_id: str = "dpl_A", provider_project: str = "prj_api", name: str | None = "shopflow-api", env_key: str = "production",
                         sha: str | None = "218241b0cd51", at: datetime = T0 - timedelta(hours=1)):
    """A FIXTURE deployment row, as `deployments.sync` would store it."""
    async with container.pool.connection() as conn:
        env = await (await conn.execute("SELECT id FROM environments WHERE project_id=%s AND environment_key=%s", (UUID(str(pid)), env_key))).fetchone()
        await conn.execute(
            "INSERT INTO deployments(organization_id,project_id,connection_id,environment_id,provider_deployment_id,provider_project_id,project_name,target,state,commit_sha,revision_mapping,created_at_provider)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'READY',%s,%s,%s)",
            (org_id, UUID(str(pid)), UUID(str(cid)), env["id"], dep_id, provider_project, name, env_key, sha, "provider_metadata" if sha else "unknown", at))
        await conn.commit()
