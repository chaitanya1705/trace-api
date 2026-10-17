"""Failing requests reported through the app log source open tickets. Events are FIXTURES shaped like ShopFlow's diagnostics."""
import json

import pytest

from tests.conftest import token_for
from tests.test_log_source import sig

pytestmark = pytest.mark.db
API = "/api/v1"


def ev(i, rid, *, level="ERROR", name="request.failed", op="POST /api/checkout", error_type="AttributeError", source=None, sec=10):
    e = {"event_id": f"auto-{i}", "event_time": f"2026-10-17T09:00:{sec:02d}.000000Z", "request_id": rid, "component_id": "checkout",
         "operation": op, "level": level, "event_name": name, "message": "Unhandled exception while processing request", "error_type": error_type}
    if source:
        e["source"] = source
    return e


async def _source(client, org):
    h = token_for(org["owner_id"], org["owner_email"])
    pid = (await client.post(f"{API}/projects", headers=h, json={"name": f"Auto{org['id'].hex[:6]}", "environments": [{"name": "production"}]})).json()["data"]["id"]
    made = (await client.post(f"{API}/projects/{pid}/log-source", headers=h, json={"environment_key": "production"})).json()["data"]
    url, secret = f"{API}/ingest/events/{made['connection_id']}", made["secret"]

    async def push(events):
        body = json.dumps({"events": events}).encode()
        r = await client.post(url, content=body, headers={"x-trace-signature": sig(secret, body)})
        assert r.status_code == 200, r.text
        return r.json()

    async def tickets():
        return (await client.get(f"{API}/tickets?view=all", headers=h)).json()["data"]

    return pid, h, push, tickets


async def test_failing_request_opens_one_ticket_and_repeats_fold_into_it(client, org):
    pid, h, push, tickets = await _source(client, org)
    # One request: an operation-level failure and the request-level failure -> one occurrence, named by the request event.
    await push([ev(1, "r1", name="operation.failed", op="fulfilment", error_type="AttributeError", sec=1), ev(2, "r1", sec=2),
                ev(3, "r1", level="INFO", name="request.completed")])
    [t] = await tickets()
    assert t["source"] == "webhook" and t["project_id"] == pid and t["status"] == "open"
    assert t["title"] == "POST /api/checkout failing in production: AttributeError" and t["service_hint"] == "POST /api/checkout"
    assert t["severity"] is None and t["environment_key"] == "production"
    assert t["evidence_summary"]["occurrences"] == 1 and t["evidence_summary"]["recent_request_ids"] == ["r1"]
    # Ready for an explicit Investigate click; opening the ticket started nothing.
    assert t["investigate"] == {"available": True, "reason": None}

    # Two more failing requests of the same kind fold in; a redelivered batch is not counted twice.
    await push([ev(4, "r2", sec=20), ev(5, "r3", sec=30)])
    await push([ev(4, "r2", sec=20)])
    [t] = await tickets()
    assert t["evidence_summary"]["occurrences"] == 3 and t["evidence_summary"]["recent_request_ids"][:2] == ["r2", "r3"]
    assert t["evidence_summary"]["last_seen"].startswith("2026-10-17T09:00:30")


async def test_distinct_failures_split_and_closed_tickets_are_not_reopened(client, org):
    pid, h, push, tickets = await _source(client, org)
    await push([ev(1, "a"), ev(2, "b", op="POST /api/checkout", error_type="TimeoutError", name="request.timed_out")])
    assert sorted(t["title"] for t in await tickets()) == ["POST /api/checkout failing in production: AttributeError",
                                                           "POST /api/checkout failing in production: TimeoutError"]
    first = next(t for t in await tickets() if t["title"].endswith("AttributeError"))
    detail = (await client.get(f"{API}/tickets/{first['id']}", headers=h)).json()["data"]
    assert (await client.patch(f"{API}/tickets/{first['id']}", headers=h, json={"status": "resolved", "version": detail["version"]})).status_code == 200
    await push([ev(3, "c")])
    same = [t for t in await tickets() if t["title"].endswith("AttributeError")]
    assert len(same) == 2 and {t["status"] for t in same} == {"open", "resolved"}


async def test_browser_and_non_error_events_open_no_ticket(client, org):
    _, _, push, tickets = await _source(client, org)
    await push([ev(1, "x", source="browser"), ev(2, "y", level="WARNING"), ev(3, "z", level="INFO", name="request.completed")])
    assert await tickets() == []
