import asyncio
import uuid

import pytest

from app.domain.projects.readiness import compute_readiness
from tests.conftest import add_member, token_for

pytestmark = pytest.mark.db
API = "/api/v1"


async def mk(client, org, name="Payments", envs=("production", "staging"), **extra):
    h = token_for(org["owner_id"], org["owner_email"])
    r = await client.post(f"{API}/projects", headers=h,
                          json={"name": name, "environments": [{"name": e} for e in envs], **extra})
    return r, h


async def test_create_list_detail_readiness_is_per_capability(client, org):
    r, h = await mk(client, org, services=[{"name": "payment-service"}, {"name": "checkout-service"}])
    assert r.status_code == 201, r.text
    p = r.json()["data"]
    assert [e["key"] for e in p["environments"]] == ["production", "staging"]
    states = {i["key"]: i["state"] for i in p["readiness"]}
    assert states["basics"] == "ok" and states["hosting"] == "todo" and states["code"] == "todo"
    lst = await client.get(f"{API}/projects", headers=h)
    assert [x["name"] for x in lst.json()["data"]] == ["Payments"]
    assert lst.json()["meta"]["coverage"]["total_authorized"] == 1
    d = (await client.get(f"{API}/projects/{p['id']}", headers=h)).json()["data"]
    assert len(d["services"]) == 2 and "manage_members" in d["capabilities"]
    assert "approve" not in d["capabilities"]  # creator administers/investigates; never an implied approver


async def test_name_unique_case_insensitive_and_race(client, org):
    r1, h = await mk(client, org, "Checkout")
    assert r1.status_code == 201
    r2, _ = await mk(client, org, "  checkout ")
    assert r2.status_code == 409 and r2.json()["error"]["code"] == "project_name_taken"
    results = await asyncio.gather(*[mk(client, org, "Racer") for _ in range(4)])
    assert sorted(r.status_code for r, _ in results) == [201, 409, 409, 409]


async def test_only_org_admins_create_and_invisible_without_grant(client, org, container):
    inv, inv_email = await add_member(container, org["id"], "investigator")
    r = await client.post(f"{API}/projects", headers=token_for(inv, inv_email), json={"name": "Nope"})
    assert r.status_code == 403
    r, h = await mk(client, org, "Secret")
    pid = r.json()["data"]["id"]
    # org membership alone grants nothing: 404 (existence not disclosed), absent from the list
    assert (await client.get(f"{API}/projects/{pid}", headers=token_for(inv, inv_email))).status_code == 404
    assert (await client.get(f"{API}/projects", headers=token_for(inv, inv_email))).json()["data"] == []


async def test_cross_organization_isolation(client, org, container):
    r, _ = await mk(client, org, "Alpha")
    pid = r.json()["data"]["id"]
    async with container.pool.connection() as conn:
        o2 = await (await conn.execute("INSERT INTO organizations(name) VALUES ('other') RETURNING id")).fetchone()
        u2 = uuid.uuid4()
        await conn.execute(
            "INSERT INTO memberships(organization_id,user_id,email,role) VALUES (%s,%s,'x@y.com','owner')",
            (o2["id"], u2))
        await conn.commit()
    assert (await client.get(f"{API}/projects/{pid}", headers=token_for(u2, "x@y.com"))).status_code == 404


async def test_members_put_rules(client, org, container):
    r, h = await mk(client, org, "Team")
    p = r.json()["data"]
    pid = p["id"]
    u, ue = await add_member(container, org["id"], "viewer")
    outsider = uuid.uuid4()
    prod = p["environments"][0]["id"]
    base = [{"user_id": str(org["owner_id"]), "role": "admin"}]
    bad_user = base + [{"user_id": str(outsider), "role": "viewer"}]
    assert (await client.put(f"{API}/projects/{pid}/members", headers=h, json={"members": bad_user})).status_code == 422
    no_admin = [{"user_id": str(u), "role": "viewer"}]
    assert (await client.put(f"{API}/projects/{pid}/members", headers=h, json={"members": no_admin})).status_code == 422
    scoped = base + [{"user_id": str(u), "role": "investigator", "environment_ids": [prod]}]
    ok = await client.put(f"{API}/projects/{pid}/members", headers=h, json={"members": scoped})
    assert ok.status_code == 200 and len(ok.json()["data"]) == 2
    # env-scoped investigator can read the project but cannot manage anything
    hu = token_for(u, ue)
    assert (await client.get(f"{API}/projects/{pid}", headers=hu)).status_code == 200
    denied = await client.put(f"{API}/projects/{pid}/environments", headers=hu,
                              json={"environments": [{"name": "production"}]})
    assert denied.status_code == 403
    foreign = base + [{"user_id": str(u), "role": "viewer", "environment_ids": [str(uuid.uuid4())]}]
    assert (await client.put(f"{API}/projects/{pid}/members", headers=h, json={"members": foreign})).status_code == 422


async def test_patch_optimistic_version_and_envs_stable_ids(client, org):
    r, h = await mk(client, org, "Vers")
    p = r.json()["data"]
    pid, ver = p["id"], p["version"]
    ok = await client.patch(f"{API}/projects/{pid}", headers=h, json={"version": ver, "description": "d"})
    assert ok.status_code == 200
    stale = await client.patch(f"{API}/projects/{pid}", headers=h, json={"version": ver, "description": "e"})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"
    ids = {e["key"]: e["id"] for e in p["environments"]}
    put = await client.put(f"{API}/projects/{pid}/environments", headers=h, json={
        "environments": [{"name": "Production (renamed)", "key": "production"}, {"name": "dev"}]})
    out = {e["key"]: e for e in put.json()["data"]}
    assert out["production"]["id"] == ids["production"] and out["production"]["name"] == "Production (renamed)"
    assert "staging" not in out and "dev" in out


async def test_scope_projection_enqueued_in_same_transaction(client, org, container):
    r, _ = await mk(client, org, "Outboxed")
    pid = r.json()["data"]["id"]
    async with container.pool.connection() as conn:
        row = await (await conn.execute("SELECT status, payload FROM outbox WHERE project_id=%s", (pid,))).fetchone()
    assert row["status"] == "pending" and row["payload"]["environments"] == ["production", "staging"]


def test_readiness_pure():
    items = compute_readiness(
        has_environments=True, connections=[("hosting", "connected_with_warnings"), ("code", "failed")],
        log_scope_total=3, log_scope_gaps=1, repo_count=1, graph_status=None)
    r = {i.key: i for i in items}
    assert (r["hosting"].state, r["code"].state, r["log_scope"].state, r["code_graph"].state) == (
        "warning", "failed", "warning", "todo")


async def test_delete_project_requires_typed_name_removes_data_and_keeps_audit(client, org, container):
    r, h = await mk(client, org, "Doomed", services=[{"name": "api"}])
    pid = r.json()["data"]["id"]
    t = (await client.post(f"{API}/tickets", headers=h, json={"title": "A report", "project_id": pid})).json()["data"]["ticket"]
    wrong = await client.delete(f"{API}/projects/{pid}?confirm=doomed-ish", headers=h)
    assert wrong.status_code == 422 and wrong.json()["error"]["code"] == "confirmation_mismatch"
    assert (await client.get(f"{API}/projects/{pid}", headers=h)).status_code == 200      # nothing was deleted

    gone = await client.delete(f"{API}/projects/{pid}?confirm=Doomed", headers=h)
    assert gone.status_code == 200 and gone.json()["data"]["deleted"] is True
    assert (await client.get(f"{API}/projects/{pid}", headers=h)).status_code == 404
    async with container.pool.connection() as conn:
        envs = await (await conn.execute("SELECT count(*) AS n FROM environments WHERE project_id=%s", (uuid.UUID(pid),))).fetchone()
        svcs = await (await conn.execute("SELECT count(*) AS n FROM services WHERE project_id=%s", (uuid.UUID(pid),))).fetchone()
        tk = await (await conn.execute("SELECT project_id FROM tickets WHERE id=%s", (uuid.UUID(t["id"]),))).fetchone()
        aud = await (await conn.execute("SELECT count(*) AS n FROM audit_events WHERE project_id=%s AND action='project.delete'", (uuid.UUID(pid),))).fetchone()
    assert envs["n"] == 0 and svcs["n"] == 0
    assert tk["project_id"] is None                       # by default the ticket returns to "Need a project"
    assert aud["n"] == 1                                  # the audit trail is kept


async def test_delete_project_can_also_delete_its_tickets_and_needs_manage_rights(client, org, container):
    r, h = await mk(client, org, "Doomed Too")
    pid = r.json()["data"]["id"]
    t = (await client.post(f"{API}/tickets", headers=h, json={"title": "Another report", "project_id": pid})).json()["data"]["ticket"]
    viewer_id, viewer_email = await add_member(container, org["id"], role="viewer")
    denied = await client.delete(f"{API}/projects/{pid}?confirm=Doomed%20Too", headers=token_for(viewer_id, viewer_email))
    assert denied.status_code in (403, 404)
    assert (await client.get(f"{API}/projects/{pid}", headers=h)).status_code == 200
    ok = await client.delete(f"{API}/projects/{pid}?confirm=Doomed%20Too&delete_tickets=true", headers=h)
    assert ok.status_code == 200 and ok.json()["data"]["tickets_deleted"] == 1
    async with container.pool.connection() as conn:
        assert (await (await conn.execute("SELECT count(*) AS n FROM tickets WHERE id=%s", (uuid.UUID(t["id"]),))).fetchone())["n"] == 0
