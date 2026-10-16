"""Wizard end to end on fixture provider responses: draft -> connect -> select -> review -> activate."""
import httpx
import pytest

import app.jobs.handlers  # noqa: F401
from app.jobs import worker
from tests.conftest import add_member, token_for
from tests.test_connections import Provider, _pem

pytestmark = pytest.mark.db
API = "/api/v1"


@pytest.fixture
def provider(container):
    p = Provider()
    container.extras["http_transport"] = httpx.MockTransport(p.handler)
    container.settings.github_app_id = "123"
    container.settings.github_app_private_key = _pem()
    return p


async def verify_all(container):
    while await worker.run_one(container, "t", ["connection.verify"]):
        pass


async def test_full_wizard_with_resume_skip_and_activation(client, container, org, provider):
    h = token_for(org["owner_id"], org["owner_email"])
    d = (await client.post(f"{API}/project-drafts", headers=h, json={})).json()["data"]
    did, ver = d["id"], d["version"]
    assert d["stages"] == {s: "todo" for s in ("basics", "hosting", "log_scope", "code", "review")}

    # Basics, then "reload": the draft survives with its data
    p1 = await client.patch(f"{API}/project-drafts/{did}", headers=h, json={
        "version": ver, "stages": {"basics": "done"},
        "data": {"basics": {"name": "Wizard Proj", "environments": [{"name": "production"}], "services": [{"name": "web"}]}}})
    assert p1.status_code == 200
    stale = await client.patch(f"{API}/project-drafts/{did}", headers=h, json={"version": ver, "stages": {"hosting": "done"}})
    assert stale.status_code == 409
    resumed = (await client.get(f"{API}/project-drafts/{did}", headers=h)).json()["data"]
    assert resumed["data"]["basics"]["name"] == "Wizard Proj" and resumed["stages"]["basics"] == "done"

    # Hosting + code connect inside the draft, before any project exists
    vc = (await client.post(f"{API}/project-drafts/{did}/connections", headers=h, json={
        "provider": "vercel", "auth_method": "vercel_token", "secret": "vcp_good_token"})).json()["data"]["resource_id"]
    gc = (await client.post(f"{API}/project-drafts/{did}/connections", headers=h, json={
        "provider": "github", "auth_method": "github_app", "config": {"installation_id": "99"}})).json()["data"]["resource_id"]
    review = (await client.get(f"{API}/project-drafts/{did}/review", headers=h)).json()["data"]["stages"]
    assert [s["ready"] for s in review] == [False, False]                      # unverified is not "ready"
    await verify_all(container)

    # Selections come from discovery
    vres = (await client.get(f"{API}/connections/{vc}/resources", headers=h)).json()["data"]
    gres = (await client.get(f"{API}/connections/{gc}/resources", headers=h)).json()["data"]
    cur = (await client.get(f"{API}/project-drafts/{did}", headers=h)).json()["data"]
    ok = await client.patch(f"{API}/project-drafts/{did}", headers=h, json={"version": cur["version"], "stages": {"hosting": "done", "log_scope": "done", "code": "done"},
        "data": {"log_scope": {"production": [{"provider_resource_id": vres[0]["provider_resource_id"], "service_key": "web"}]},
                 "repositories": [{"provider_repository_id": gres[0]["provider_repository_id"]}]}})
    assert ok.status_code == 200
    assert all(s["ready"] for s in (await client.get(f"{API}/project-drafts/{did}/review", headers=h)).json()["data"]["stages"])

    act = await client.post(f"{API}/project-drafts/{did}/activate", headers=h)
    assert act.status_code == 201, act.text
    out = act.json()["data"]
    pid = out["project"]["id"]
    assert out["index_job_ids"] == [] and out["repositories_bound"] == 1 and out["log_scope_entries"] == 1
    states = {i["key"]: i["state"] for i in out["project"]["readiness"]}
    assert states["hosting"] == "warning" and states["code"] == "ok"            # vercel token warns; log scope has no drain
    assert states["log_scope"] == "warning" and states["code_graph"] == "todo"   # unknown logs are a gap, not green

    conns = (await client.get(f"{API}/projects/{pid}/connections", headers=h)).json()["data"]
    assert {c["provider"] for c in conns} == {"vercel", "github"} and all(c["draft_id"] is None for c in conns)
    scope = (await client.get(f"{API}/projects/{pid}/log-scope", headers=h)).json()
    assert scope["data"][0]["log_availability"] == "unknown" and scope["meta"]["coverage"]["without_usable_logs"] == 1
    repos = (await client.get(f"{API}/projects/{pid}/repositories", headers=h)).json()["data"]
    assert repos[0]["full_name"] == "acme/payments" and repos[0]["selected_ref"] == "main"
    assert (await client.post(f"{API}/project-drafts/{did}/activate", headers=h)).status_code == 409   # not twice

    # one repo -> many services via explicit mappings
    await client.put(f"{API}/projects/{pid}/services", headers=h, json={"services": [{"name": "web"}, {"name": "api"}]})
    m = await client.put(f"{API}/projects/{pid}/repositories/{repos[0]['id']}/mappings", headers=h, json={
        "services": [{"service_key": "web", "path_prefix": "apps/web"}, {"service_key": "api", "path_prefix": "apps/api"}],
        "paths": [{"runtime_prefix": "/var/task/", "repo_prefix": "src/"}]})
    assert m.status_code == 200 and len(m.json()["data"]["services"]) == 2
    bad = await client.put(f"{API}/projects/{pid}/repositories/{repos[0]['id']}/mappings", headers=h, json={
        "services": [{"service_key": "web", "path_prefix": "../etc"}]})
    assert bad.status_code == 422


async def test_forged_ids_skipped_stage_and_draft_privacy(client, container, org, provider):
    h = token_for(org["owner_id"], org["owner_email"])
    did = (await client.post(f"{API}/project-drafts", headers=h, json={})).json()["data"]["id"]
    d = (await client.get(f"{API}/project-drafts/{did}", headers=h)).json()["data"]
    # activation without Basics is refused with a precise code
    assert (await client.post(f"{API}/project-drafts/{did}/activate", headers=h)).json()["error"]["code"] == "basics_required"
    await client.patch(f"{API}/project-drafts/{did}", headers=h, json={"version": d["version"], "stages": {"hosting": "skipped", "code": "skipped"},
        "data": {"basics": {"name": "Skippy", "environments": [{"name": "production"}]}}})
    out = (await client.post(f"{API}/project-drafts/{did}/activate", headers=h)).json()["data"]["project"]
    states = {i["key"]: i["state"] for i in out["readiness"]}
    assert states["hosting"] == "todo" and states["code"] == "todo"      # skipped stays unfinished, not green
    pid = out["id"]
    # forged repository id (not visible to the connection) is rejected
    await client.post(f"{API}/projects/{pid}/connections", headers=h, json={"provider": "github", "auth_method": "github_app", "config": {"installation_id": "5"}})
    await verify_all(container)
    forged = await client.put(f"{API}/projects/{pid}/repositories", headers=h, json={"repositories": [{"provider_repository_id": "424242"}]})
    assert forged.status_code == 422

    # another org member cannot see or touch my draft
    other, oe = await add_member(container, org["id"], "admin")
    d2 = (await client.post(f"{API}/project-drafts", headers=h, json={})).json()["data"]["id"]
    assert (await client.get(f"{API}/project-drafts/{d2}", headers=token_for(other, oe))).status_code == 200   # org admins may (spec: creator/Admin)
    viewer, ve = await add_member(container, org["id"], "viewer")
    assert (await client.get(f"{API}/project-drafts/{d2}", headers=token_for(viewer, ve))).status_code == 404
    assert (await client.post(f"{API}/project-drafts", headers=token_for(viewer, ve), json={})).status_code == 403
    discard = await client.post(f"{API}/project-drafts/{d2}/discard", headers=h)
    assert discard.json()["data"]["status"] == "discarded"
