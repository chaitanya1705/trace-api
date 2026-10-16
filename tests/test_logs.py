"""Log intake, live capture, stored search. Provider traffic is FIXTURE data shaped like Vercel's documented formats."""
import hashlib
import hmac
import json

import httpx
import pytest

import app.jobs.handlers  # noqa: F401
import app.jobs.handlers.logs  # noqa: F401
from app.domain.ingest import vercel_logs as vl
from app.errors import Unprocessable
from app.jobs import worker
from tests.conftest import token_for
from tests.test_connections import Provider

pytestmark = pytest.mark.db
API = "/api/v1"
KINDS = ["connection.verify", "logs.capture"]


def sig(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()


def drain_rec(i: int, **kw):
    return {"id": f"log-{i}", "deploymentId": "dpl_1", "source": "lambda", "host": "x.vercel.app", "timestamp": 1760000000000 + i * 1000, "projectId": "prj_1",
            "level": "info", "message": f"hello {i}", "requestId": f"req-{i}", "statusCode": 200, "path": "/api/orders", "environment": "production", **kw}


class RuntimeLogs(Provider):
    LINES = [
        {"level": "info", "message": json.dumps({"event_id": "ev-1", "request_id": "rq-9", "run_id": "run-1", "operation": "validate_request", "event_name": "operation.started", "message": "validate_request started", "source_revision": "218241b0"}),
         "messageTruncated": False, "rowId": "1", "source": "function", "timestampInMs": 1760000100000, "requestMethod": "POST", "requestPath": "/api/checkout", "domain": "d", "responseStatusCode": -1},
        {"level": "error", "message": "TimeoutError: authorize exceeded 800ms token=abcd1234efgh5678", "messageTruncated": False, "rowId": "2", "source": "function", "timestampInMs": 1760000101000,
         "requestMethod": "POST", "requestPath": "/api/checkout", "domain": "d", "responseStatusCode": 500},
    ]

    def handler(self, req):
        if req.url.host == "api.vercel.com" and req.url.path.endswith("/runtime-logs"):
            return httpx.Response(200, content=("\n".join(json.dumps(x) for x in self.LINES) + "\n").encode(), headers={"content-type": "application/stream+json"})
        if req.url.host == "api.vercel.com" and req.url.path == "/v7/deployments":
            return httpx.Response(200, json={"deployments": [{"uid": "dpl_1", "projectId": "prj_1", "name": "api", "target": "production", "readyState": "READY", "created": 1760000000000, "meta": {}}], "pagination": {}})
        return super().handler(req)


async def setup(client, container, org, transport=None):
    h = token_for(org["owner_id"], org["owner_email"])
    p = (await client.post(f"{API}/projects", headers=h, json={"name": f"Logs{org['id'].hex[:6]}", "environments": [{"name": "production"}, {"name": "preview"}]})).json()["data"]
    container.extras["http_transport"] = httpx.MockTransport((transport or Provider()).handler)
    cid = (await client.post(f"{API}/projects/{p['id']}/connections", headers=h, json={"provider": "vercel", "auth_method": "vercel_token", "secret": "vcp_good_token"})).json()["data"]["resource_id"]
    while await worker.run_one(container, "t", KINDS):
        pass
    res = (await client.get(f"{API}/connections/{cid}/resources", headers=h)).json()["data"]
    assert (await client.put(f"{API}/projects/{p['id']}/log-scope", headers=h, json={"environments": {"production": [{"provider_resource_id": res[0]["provider_resource_id"]}]}})).status_code == 200
    return p["id"], cid, h, res[0]["provider_resource_id"]


async def secret_for(client, cid, h):
    return (await client.post(f"{API}/connections/{cid}/drain/secret", headers=h)).json()["data"]["secret"]


def test_normalizers():
    assert vl.signature_ok("s", b"body", sig("s", b"body")) and not vl.signature_ok("s", b"body", "00") and not vl.signature_ok("s", b"body", None)
    assert len(vl.parse_body(b'[{"a":1},{"a":2}]')) == 2 and len(vl.parse_body(b'{"a":1}\n{"a":2}\n')) == 2
    with pytest.raises(Unprocessable):
        vl.parse_body(b'{"a":1}\nnot json')
    r = vl.normalize({"id": "x", "projectId": "p", "timestamp": 1760000000000, "message": "Authorization: Bearer abcdefghijklmnop1234", "level": "error"})
    assert "abcdefghijklmnop" not in r.message and r.event_time.year == 2025
    assert vl.normalize({"id": "x", "projectId": "p", "message": "m"}).event_time is None        # unknown time stays unknown
    assert vl.normalize({"projectId": "p"}) is None and vl.normalize("nope") is None
    rr = vl.normalize_runtime(RuntimeLogs.LINES[0], deployment_id="dpl_1", provider_project_id="prj_1", env_key="production")
    assert rr.request_id == "rq-9" and rr.extra["operation"] == "validate_request" and rr.extra["source_revision"] == "218241b0" and rr.status_code is None and rr.message == "validate_request started"


async def test_drain_intake_signature_formats_dedupe_scope_and_search(client, container, org):
    pid, cid, h, vproj = await setup(client, container, org)
    url = f"{API}/ingest/vercel/{cid}"
    body = json.dumps([drain_rec(1), drain_rec(2, level="error", message="boom api_key=sk-supersecretsupersecret1234"), drain_rec(3, projectId="prj_OTHER")]).encode()
    # no secret configured yet -> indistinguishable from an unknown drain
    assert (await client.post(url, content=body, headers={"x-vercel-signature": "x"})).status_code == 404
    secret = await secret_for(client, cid, h)
    assert (await client.post(url, content=body, headers={"x-vercel-signature": "bad"})).status_code == 401
    ok = await client.post(url, content=body, headers={"x-vercel-signature": sig(secret, body)})
    assert ok.status_code == 200
    # the fixture log-scope project id is prj_1; records for prj_OTHER are out of scope and never stored
    assert ok.json() == {"received": 3, "accepted": 2, "duplicates": 0, "out_of_scope": 1, "rejected": 0}
    again = await client.post(url, content=body, headers={"x-vercel-signature": sig(secret, body)})
    assert again.json()["duplicates"] == 2 and again.json()["accepted"] == 0                # at-least-once transport, exactly-once evidence
    nd = "\n".join(json.dumps(drain_rec(i)) for i in (4, 5)).encode()                      # NDJSON works too
    assert (await client.post(url, content=nd, headers={"x-vercel-signature": sig(secret, nd)})).json()["accepted"] == 2
    junk = b'{"id":"z"}'
    assert (await client.post(url, content=junk, headers={"x-vercel-signature": sig(secret, junk)})).json()["rejected"] == 1

    r = (await client.get(f"{API}/projects/{pid}/logs", headers=h)).json()
    assert r["meta"]["coverage"]["stored_total"] == 4 and r["meta"]["coverage"]["not_yet_in_graph"] == 4 and r["meta"]["coverage"]["mode"] == "stored"
    assert r["data"][0]["environment_key"] == "production" and "supersecret" not in json.dumps(r)
    assert [x["message"] for x in (await client.get(f"{API}/projects/{pid}/logs?level=error", headers=h)).json()["data"]][0].startswith("boom")
    assert len((await client.get(f"{API}/projects/{pid}/logs?q=HELLO", headers=h)).json()["data"]) == 3
    assert len((await client.get(f"{API}/projects/{pid}/logs?request_id=req-4", headers=h)).json()["data"]) == 1
    none = (await client.get(f"{API}/projects/{pid}/logs?q=zzz", headers=h)).json()
    assert none["data"] == [] and none["meta"]["coverage"]["empty_dataset"] is False and none["meta"]["coverage"]["matched"] == 0   # no match != nothing stored
    p1 = (await client.get(f"{API}/projects/{pid}/logs?limit=2", headers=h)).json()
    p2 = (await client.get(f"{API}/projects/{pid}/logs?limit=2&cursor={p1['meta']['next_cursor']}", headers=h)).json()
    assert len(p1["data"]) == 2 and len(p2["data"]) == 2 and not set(x["id"] for x in p1["data"]) & set(x["id"] for x in p2["data"])
    st = (await client.get(f"{API}/projects/{pid}/logs/status", headers=h)).json()["data"]
    assert st["resources"][0]["log_availability"] == "available" and st["resources"][0]["last_event_at"] and st["drain"]["secret_configured"] is True
    assert (await client.get(f"{API}/projects/{pid}/logs?since=garbage", headers=h)).status_code == 422


async def test_empty_state_is_unknown_not_healthy(client, container, org):
    pid, cid, h, _ = await setup(client, container, org)
    st = (await client.get(f"{API}/projects/{pid}/logs/status", headers=h)).json()["data"]
    assert st["resources"][0]["log_availability"] == "unknown" and st["resources"][0]["last_event_at"] is None and st["drain"]["secret_configured"] is False
    r = (await client.get(f"{API}/projects/{pid}/logs", headers=h)).json()
    assert r["data"] == [] and r["meta"]["coverage"]["empty_dataset"] is True


async def test_live_capture_stores_structured_runtime_logs(client, container, org):
    pid, cid, h, _ = await setup(client, container, org, RuntimeLogs())
    assert (await client.post(f"{API}/projects/{pid}/logs/capture", headers=h, json={"seconds": 5})).status_code == 202
    while await worker.run_one(container, "t", KINDS):
        pass
    r = (await client.get(f"{API}/projects/{pid}/logs?request_id=rq-9", headers=h)).json()["data"]
    assert len(r) == 1 and r[0]["extra"]["source_revision"] == "218241b0" and r[0]["environment_key"] == "production"
    errs = (await client.get(f"{API}/projects/{pid}/logs?level=error", headers=h)).json()["data"]
    assert errs[0]["status_code"] == 500 and "abcd1234efgh5678" not in errs[0]["message"]
    st = (await client.get(f"{API}/projects/{pid}/logs/status", headers=h)).json()["data"]
    assert st["last_capture"]["status"] == "succeeded" and st["last_capture"]["result_ref"]["accepted"] == 2
    # capturing again does not duplicate evidence
    await client.post(f"{API}/projects/{pid}/logs/capture", headers=h, json={"seconds": 5})
    while await worker.run_one(container, "t", KINDS):
        pass
    assert (await client.get(f"{API}/projects/{pid}/logs", headers=h)).json()["meta"]["coverage"]["stored_total"] == 2


async def test_capture_needs_hosting_and_scope(client, container, org):
    h = token_for(org["owner_id"], org["owner_email"])
    p = (await client.post(f"{API}/projects", headers=h, json={"name": f"NoHost{org['id'].hex[:6]}", "environments": [{"name": "production"}]})).json()["data"]
    assert (await client.post(f"{API}/projects/{p['id']}/logs/capture", headers=h, json={})).json()["error"]["code"] == "no_hosting"
    assert (await client.post(f"{API}/projects/{p['id']}/logs/capture", headers=h, json={"seconds": 1})).status_code == 422
