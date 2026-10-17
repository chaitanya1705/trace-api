"""App log push: signed webhook -> project logs. Events are FIXTURES shaped like ShopFlow's own diagnostics (same field names)."""
import hashlib
import hmac
import json

import pytest

from app.api.v1.log_source import _read_signed_body
from app.domain.ingest.app_events import normalize_app_event
from tests.conftest import token_for

pytestmark = pytest.mark.db
API = "/api/v1"


def sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def test_signed_body_reader_stops_after_crossing_chunk():
    class ChunkedRequest:
        def __init__(self):
            self.yielded = []

        async def stream(self):
            for chunk in (b"1234", b"56", b"later"):
                self.yielded.append(chunk)
                yield chunk

    request = ChunkedRequest()
    raw, body_bytes, digest = await _read_signed_body(request, 5, "secret")

    assert raw is None and digest == ""
    assert body_bytes == 6  # includes the already-yielded chunk that crossed the cap
    assert request.yielded == [b"1234", b"56"]  # the later stream chunk was never requested


def sf_event(i, op="validate_request", level="INFO", **kw):
    return {"event_id": f"evt-{i}", "event_time": f"2026-10-16T18:32:{10 + i}.000000Z", "request_id": kw.get("rid", "req-A"), "run_id": "run-1", "component_id": "checkout",
            "operation": op, "level": level, "event_name": "operation.started", "message": f"{op} started", "duration_ms": None, "error_type": kw.get("error_type"),
            "source_revision": "218241b0cd51", "deployment_id": "dpl_X"}


def test_normalizer_accepts_shopflow_shape_and_never_invents_time():
    r = normalize_app_event(sf_event(1, "checkout", "ERROR", error_type="TimeoutError"), "production")
    assert r.level == "error" and r.env_key == "production" and r.request_id == "req-A" and r.event_time.year == 2026 and r.message.endswith("[TimeoutError]")
    assert r.extra["source_revision"] == "218241b0cd51" and r.extra["via"] == "app_push" and r.service_hint == "checkout"
    browser = normalize_app_event({**sf_event(2), "source": "browser", "trust_class": "trusted", "related_request_id": "client-claim"}, "production")
    assert browser.source == "browser" and browser.extra["trust_class"] == "untrusted_browser"
    assert browser.request_id == "req-A" and browser.extra["client_reported_related_request_id"] == "client-claim"
    assert normalize_app_event({**sf_event(3), "source": "other"}, "production") is None
    assert normalize_app_event({"id": "x", "message": "m"}, None).event_time is None                     # unknown stays unknown
    assert normalize_app_event({"id": "x", "message": "m", "time": 1760000000}, None).event_time.year == 2025
    assert normalize_app_event({"event_id": "x", "message": "Authorization: Bearer abcdefghijklmnop1234", "environment": "Staging"}, "production").env_key == "staging"
    for bad in ("nope", {"message": "no id"}, {"event_id": "x"}, {"event_id": "x", "message": ""}):
        assert normalize_app_event(bad, None) is None


async def _project(client, org):
    h = token_for(org["owner_id"], org["owner_email"])
    p = (await client.post(f"{API}/projects", headers=h, json={"name": f"Src{org['id'].hex[:6]}", "environments": [{"name": "production"}, {"name": "staging"}]})).json()["data"]
    return p["id"], h


async def test_setup_signing_ingest_dedupe_search_and_rotation(client, org):
    pid, h = await _project(client, org)
    before = (await client.get(f"{API}/projects/{pid}/log-source", headers=h)).json()["data"]
    assert before["configured"] is False and "forward" in before["forwarder_snippet"] and "x-trace-signature" in before["forwarder_snippet"]
    assert (await client.post(f"{API}/projects/{pid}/log-source", headers=h, json={"environment_key": "nope"})).status_code == 422
    made = (await client.post(f"{API}/projects/{pid}/log-source", headers=h, json={"environment_key": "production"})).json()["data"]
    assert made["configured"] and made["secret_shown_once"] and len(made["secret"]) == 48 and made["environment_key"] == "production"
    secret, cid, url = made["secret"], made["connection_id"], f"{API}/ingest/events/{made['connection_id']}"
    assert made["endpoint_url"].endswith(f"/api/v1/ingest/events/{cid}")
    again = (await client.get(f"{API}/projects/{pid}/log-source", headers=h)).json()["data"]
    assert again["secret"] is None and again["secret_configured"] is True                        # never readable again

    body = json.dumps({"events": [sf_event(1), sf_event(2, "calculate_price"), sf_event(3, "checkout", "ERROR", error_type="TimeoutError"), {"bad": 1}]}).encode()
    assert (await client.post(url, content=body, headers={"x-trace-signature": "sha256=00"})).status_code == 401
    assert (await client.post(url, content=body)).status_code == 401
    ok = await client.post(url, content=body, headers={"x-trace-signature": sig(secret, body)})
    assert ok.status_code == 200 and ok.json() == {"received": 4, "accepted": 3, "duplicates": 0, "rejected": 1}
    rep = await client.post(url, content=body, headers={"x-trace-signature": sig(secret, body)})
    assert rep.json()["duplicates"] == 3 and rep.json()["accepted"] == 0                             # exactly-once logical evidence
    nd = "\n".join(json.dumps(sf_event(i)) for i in (7, 8)).encode()                                  # NDJSON and bare arrays also accepted
    assert (await client.post(url, content=nd, headers={"x-trace-signature": sig(secret, nd)})).json()["accepted"] == 2
    arr = json.dumps([sf_event(9)]).encode()
    assert (await client.post(url, content=arr, headers={"x-trace-signature": sig(secret, arr)})).json()["accepted"] == 1
    browser_body = json.dumps({"events": [{**sf_event(11), "source": "browser", "trust_class": "trusted"}]}).encode()
    assert (await client.post(url, content=browser_body, headers={"x-trace-signature": sig(secret, browser_body)})).json()["accepted"] == 1

    logs = (await client.get(f"{API}/projects/{pid}/logs", headers=h)).json()
    assert logs["meta"]["coverage"]["stored_total"] == 7 and {x["provider"] for x in logs["data"]} == {"app_push"} and {x["environment_key"] for x in logs["data"]} == {"production"}
    browser_log = next(x for x in logs["data"] if x["source"] == "browser")
    assert browser_log["trust_class"] == "untrusted_browser" and browser_log["extra"]["trust_class"] == "untrusted_browser"
    err = (await client.get(f"{API}/projects/{pid}/logs?level=error", headers=h)).json()["data"]
    assert len(err) == 1 and err[0]["extra"]["error_type"] == "TimeoutError" and err[0]["request_id"] == "req-A"
    st = (await client.get(f"{API}/projects/{pid}/log-source", headers=h)).json()["data"]
    assert st["stored_events"] == 7 and st["last_batch_at"] and st["last_batch"]["accepted"] == 1

    # rotation invalidates the old secret
    new = (await client.post(f"{API}/projects/{pid}/log-source", headers=h, json={"environment_key": "production"})).json()["data"]["secret"]
    assert new != secret
    assert (await client.post(url, content=arr, headers={"x-trace-signature": sig(secret, arr)})).status_code == 401
    arr2 = json.dumps([sf_event(10)]).encode()
    assert (await client.post(url, content=arr2, headers={"x-trace-signature": sig(new, arr2)})).status_code == 200


async def test_disconnect_wipes_secret_and_blocks_ingest(client, org):
    pid, h = await _project(client, org)
    made = (await client.post(f"{API}/projects/{pid}/log-source", headers=h, json={})).json()["data"]
    cid, secret = made["connection_id"], made["secret"]
    body = json.dumps([sf_event(1)]).encode()
    r = await client.post(f"{API}/ingest/events/{cid}", content=body, headers={"x-trace-signature": sig(secret, body)})
    assert r.json()["accepted"] == 1
    logs = (await client.get(f"{API}/projects/{pid}/logs", headers=h)).json()["data"]
    assert logs[0]["environment_key"] is None                                                        # no default environment set: stays unmapped, not guessed
    assert (await client.post(f"{API}/connections/{cid}/disconnect", headers=h)).status_code == 200
    assert (await client.post(f"{API}/ingest/events/{cid}", content=body, headers={"x-trace-signature": sig(secret, body)})).status_code == 404
    assert (await client.get(f"{API}/projects/{pid}/log-source", headers=h)).json()["data"]["configured"] is False


async def test_signed_ingest_enforces_streamed_body_limit(client, org, container):
    pid, h = await _project(client, org)
    made = (await client.post(f"{API}/projects/{pid}/log-source", headers=h, json={})).json()["data"]
    secret, cid = made["secret"], made["connection_id"]
    url = f"{API}/ingest/events/{cid}"
    body = json.dumps({"events": [{"event_id": "at-limit", "message": "accepted"}]}).encode()

    container.settings.ingest_max_body_bytes = len(body)
    accepted = await client.post(url, content=body, headers={"x-trace-signature": sig(secret, body)})
    assert accepted.status_code == 200 and accepted.json()["accepted"] == 1

    container.settings.ingest_max_body_bytes = len(body) - 1
    oversized = await client.post(url, content=body, headers={"x-trace-signature": sig(secret, body)})
    assert oversized.status_code == 413 and oversized.json()["error"]["code"] == "payload_too_large"
    async with container.pool.connection() as conn:
        batches = await (await conn.execute("SELECT count(*) AS n FROM ingestion_batches WHERE connection_id=%s", (cid,))).fetchone()
    assert batches["n"] == 1  # only the exact-cap, signature-validated request was accounted
