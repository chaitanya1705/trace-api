import json
import uuid

import pytest

from app.redact import redact_json, redact_text
from tests.conftest import add_member, token_for

pytestmark = pytest.mark.db
API = "/api/v1"

# FIXTURE shaped like PulseLab's documented incident export (schema 1.0). Not real run data.
EXPORT = {
    "schema_version": "1.0",
    "ticket": {"ticket_id": "pl-t-001", "run_id": "run-77", "title": "Mixed cart total is wrong",
               "description": "Totals differ from expectation. Authorization: Bearer abcdefghijklmnop123456",
               "target_url": "https://shopflow-backend-gray.vercel.app", "environment_label": "Production",
               "started_at": "2026-10-16T10:00:00Z", "ended_at": "2026-10-16T10:05:00Z", "affected_operation": "POST /api/orders",
               "observed_counts": {"requests": 40, "business_failures": 12}, "relevant_request_ids": ["r1", "r2"],
               "evidence_availability": "partial"},
    "target": {"url": "https://shopflow-backend-gray.vercel.app"},
    "requests": [{"id": "r1", "status": 200}, {"id": "r2", "status": 200}],
    "backend_events": [{"request_id": "r1", "msg": "total computed", "api_key": "sk-supersecretsupersecret1234"}],
    "evidence_gaps": [{"kind": "missing_backend_log"}],
}


def test_redaction_patterns_and_keys():
    assert "abcdefghijklmnop" not in redact_text("Authorization: Bearer abcdefghijklmnop123456")
    assert "ghp_" not in redact_text("token ghp_" + "a" * 30)
    assert "hunter22" not in redact_text("postgres://u:hunter22@db/x password=hunter22")
    out = redact_json({"api_key": "sk-" + "x" * 30, "nested": [{"password": "p"}], "msg": "ok"})
    assert out == {"api_key": "[REDACTED]", "nested": [{"password": "[REDACTED]"}], "msg": "ok"}


async def _setup(client, org, name="Shop"):
    h = token_for(org["owner_id"], org["owner_email"])
    p = (await client.post(f"{API}/projects", headers=h, json={"name": f"{name}{org['id'].hex[:5]}", "environments": [{"name": "production"}]})).json()["data"]
    return p["id"], h


async def _jobs(container, org):
    async with container.pool.connection() as conn:
        return (await (await conn.execute("SELECT count(*) AS n FROM jobs WHERE organization_id=%s AND kind <> 'graph.sync'", (org["id"],))).fetchone())["n"]


async def test_manual_ticket_lists_views_and_empty_vs_no_match(client, org):
    pid, h = await _setup(client, org)
    empty = (await client.get(f"{API}/tickets", headers=h)).json()
    assert empty["data"] == [] and empty["meta"]["coverage"]["empty_dataset"] is True
    r = await client.post(f"{API}/tickets", headers=h, json={"title": "Login latency spike", "severity": "SEV-2", "environment_key": "Production",
                                                             "service_hint": "auth-api"})
    assert r.status_code == 201
    t = r.json()["data"]["ticket"]
    assert t["display_id"].startswith("TKT-") and t["project_id"] is None and t["environment_key"] == "production"
    assert t["investigate"] == {"available": False, "reason": "Assign this ticket to a project first."}
    need = (await client.get(f"{API}/tickets?view=need_project", headers=h)).json()
    assert [x["title"] for x in need["data"]] == ["Login latency spike"] and need["meta"]["coverage"]["counts"]["need_project"] == 1
    nomatch = (await client.get(f"{API}/tickets?view=all&q=zzz", headers=h)).json()
    assert nomatch["data"] == [] and nomatch["meta"]["coverage"]["empty_dataset"] is False   # "no matches" != "empty dataset"
    assert len((await client.get(f"{API}/tickets?view=all&q=AUTH", headers=h)).json()["data"]) == 1
    assert len((await client.get(f"{API}/tickets?view=all&severity=SEV-3", headers=h)).json()["data"]) == 0


async def test_assignment_enqueues_nothing_and_sets_owner(client, org, container):
    pid, h = await _setup(client, org)
    t = (await client.post(f"{API}/tickets", headers=h, json={"title": "Webhook failures"})).json()["data"]["ticket"]
    before = await _jobs(container, org)
    a = await client.patch(f"{API}/tickets/{t['id']}", headers=h, json={"version": t["version"], "project_id": pid})
    assert a.status_code == 200, a.text
    got = a.json()["data"]
    assert got["project_id"] == pid and got["owner_user_id"] == str(org["owner_id"]) and got["project_name"].startswith("Shop")
    assert await _jobs(container, org) == before                     # assignment started no run (D-01)
    assert got["investigate"] == {"available": True, "reason": None}   # an assigned open ticket may be investigated, but only by an explicit action
    for _ in range(2):                                               # refresh / open also enqueue nothing
        await client.get(f"{API}/tickets", headers=h)
        await client.get(f"{API}/tickets/{t['id']}", headers=h)
    assert await _jobs(container, org) == before
    stale = await client.patch(f"{API}/tickets/{t['id']}", headers=h, json={"version": t["version"], "status": "closed"})
    assert stale.status_code == 409
    closed = await client.patch(f"{API}/tickets/{t['id']}", headers=h, json={"version": got["version"], "status": "resolved"})
    assert closed.json()["data"]["status"] == "resolved"
    res = (await client.get(f"{API}/tickets?view=resolved", headers=h)).json()
    assert [x["id"] for x in res["data"]] == [t["id"]] and res["meta"]["coverage"]["counts"]["open"] == 0


async def test_pulselab_import_maps_redacts_dedupes_and_revises(client, org):
    pid, h = await _setup(client, org)
    r = await client.post(f"{API}/tickets/import/pulselab", headers=h, content=json.dumps(EXPORT))
    assert r.status_code == 201, r.text
    t = r.json()["data"]["ticket"]
    assert t["source"] == "pulselab" and t["external_id"] == "pl-t-001" and t["environment_key"] == "production"
    assert t["service_hint"] == "POST /api/orders" and t["window_start"].startswith("2026-10-16T10:00:00")
    assert t["severity"] is None                                     # never invented
    assert t["evidence_summary"]["requests"] == 2 and t["evidence_summary"]["backend_events"] == 1
    assert t["evidence_sync"] == "pending"                           # evidence not in the graph yet - said honestly
    assert "abcdefghijklmnop" not in t["description"]
    payload = (await client.get(f"{API}/tickets/{t['id']}/payload", headers=h)).text
    assert "sk-supersecret" not in payload and "abcdefghijklmnop" not in payload
    again = await client.post(f"{API}/tickets/import/pulselab", headers=h, content=json.dumps(EXPORT))
    assert again.status_code == 200 and again.json()["data"]["outcome"] == "unchanged"
    changed = json.loads(json.dumps(EXPORT))
    changed["ticket"]["title"] = "Mixed cart total is wrong (updated)"
    upd = await client.post(f"{API}/tickets/import/pulselab", headers=h, content=json.dumps(changed))
    assert upd.json()["data"]["outcome"] == "updated" and upd.json()["data"]["ticket"]["revision_number"] == 2
    d = (await client.get(f"{API}/tickets/{t['id']}", headers=h)).json()["data"]
    assert [r["revision_number"] for r in d["revisions"]] == [2, 1]
    allt = (await client.get(f"{API}/tickets?view=all", headers=h)).json()["data"]
    assert len(allt) == 1                                            # deduped on (source, ticket_id)


async def test_import_rejects_garbage_and_oversize(client, org):
    pid, h = await _setup(client, org)
    assert (await client.post(f"{API}/tickets/import/pulselab", headers=h, content=b"not json")).json()["error"]["code"] == "invalid_export"
    assert (await client.post(f"{API}/tickets/import/pulselab", headers=h, content=json.dumps({"hello": 1}))).status_code == 422
    assert (await client.post(f"{API}/tickets/import/pulselab", headers=h, content=json.dumps({"ticket": {"ticket_id": "x"}}))).status_code == 422
    assert (await client.post(f"{API}/tickets/import/pulselab", headers=h, content=b"{" + b" " * (5 * 1024 * 1024 + 1) + b"}")).status_code == 413


async def test_api_external_id_is_idempotent(client, org):
    pid, h = await _setup(client, org)
    body = {"title": "Disk full", "external_id": "ext-1", "severity": "SEV-1"}
    a = await client.post(f"{API}/tickets", headers=h, json=body)
    b = await client.post(f"{API}/tickets", headers=h, json=body)
    assert a.status_code == 201 and b.status_code == 200 and b.json()["data"]["outcome"] == "unchanged"
    assert a.json()["data"]["ticket"]["id"] == b.json()["data"]["ticket"]["id"]


async def test_access_rules(client, org, container):
    pid, h = await _setup(client, org)
    inv, ie = await add_member(container, org["id"], "investigator")
    view, ve = await add_member(container, org["id"], "viewer")
    hi, hv = token_for(inv, ie), token_for(view, ve)
    assert (await client.post(f"{API}/tickets", headers=hv, json={"title": "viewer cannot create"})).status_code == 403
    mine = (await client.post(f"{API}/tickets", headers=hi, json={"title": "triage me"})).json()["data"]["ticket"]
    assert (await client.get(f"{API}/tickets/{mine['id']}", headers=hv)).status_code == 200      # unassigned: org triage
    # assigning requires investigate on the TARGET project - investigator has no grant there
    assert (await client.patch(f"{API}/tickets/{mine['id']}", headers=hi, json={"version": mine["version"], "project_id": pid})).status_code == 404
    await client.put(f"{API}/projects/{pid}/members", headers=h, json={"members": [
        {"user_id": str(org["owner_id"]), "role": "admin"}, {"user_id": str(inv), "role": "investigator"}]})
    ok = await client.patch(f"{API}/tickets/{mine['id']}", headers=hi, json={"version": mine["version"], "project_id": pid})
    assert ok.status_code == 200
    # once assigned, it follows project grants: the viewer without a grant no longer sees it
    assert (await client.get(f"{API}/tickets/{mine['id']}", headers=hv)).status_code == 404
    assert mine["id"] not in [x["id"] for x in (await client.get(f"{API}/tickets?view=all", headers=hv)).json()["data"]]
    assert (await client.get(f"{API}/tickets?project_id={pid}", headers=hv)).status_code == 404
    assert (await client.get(f"{API}/tickets/{uuid.uuid4()}", headers=hi)).status_code == 404


async def test_pagination_and_sort(client, org):
    pid, h = await _setup(client, org)
    for i in range(5):
        await client.post(f"{API}/tickets", headers=h, json={"title": f"T{i}", "severity": "SEV-2" if i % 2 else "SEV-4"})
    p1 = (await client.get(f"{API}/tickets?view=all&limit=2&sort=title", headers=h)).json()
    assert [x["title"] for x in p1["data"]] == ["T0", "T1"] and p1["meta"]["next_cursor"]
    p2 = (await client.get(f"{API}/tickets?view=all&limit=2&sort=title&cursor={p1['meta']['next_cursor']}", headers=h)).json()
    assert [x["title"] for x in p2["data"]] == ["T2", "T3"]
    sev = (await client.get(f"{API}/tickets?view=all&sort=severity", headers=h)).json()["data"]
    assert [x["severity"] for x in sev][:2] == ["SEV-2", "SEV-2"]
    assert (await client.get(f"{API}/tickets?sort=bogus", headers=h)).status_code == 422
