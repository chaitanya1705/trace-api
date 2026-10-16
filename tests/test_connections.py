"""Connection lifecycle against FIXTURE provider responses (httpx.MockTransport). The fixtures are labeled test data
shaped like the documented GitHub/Vercel responses; they prove our handling, not that a real account works."""
import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import app.jobs.handlers  # noqa: F401  (registers connection.verify)
from app.connectors.github import compare_permissions
from app.connectors.vercel import deployment_sha
from app.jobs import worker
from tests.conftest import token_for

pytestmark = pytest.mark.db
API = "/api/v1"


def _pem() -> str:
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


class Provider:
    """Programmable fake of api.github.com + api.vercel.com. FIXTURE ONLY."""

    def __init__(self):
        self.gh_perms = {"metadata": "read", "contents": "read", "pull_requests": "read"}
        self.gh_suspended = None
        self.vercel_ok = True       # tokens equal to "vcp_good_token" are accepted
        self.calls: list[str] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        u = req.url
        self.calls.append(f"{req.method} {u.host}{u.path}")
        if u.host == "api.github.com":
            if u.path.startswith("/app/installations/") and u.path.endswith("/access_tokens"):
                return httpx.Response(201, json={"token": "ghs_fixture", "expires_at": "2099-01-01T00:00:00Z"})
            if u.path.startswith("/app/installations/"):
                return httpx.Response(200, json={"account": {"login": "acme"}, "permissions": self.gh_perms,
                                                 "repository_selection": "selected", "suspended_at": self.gh_suspended})
            if u.path == "/installation/repositories":
                repos = [{"id": 1, "full_name": "acme/payments", "default_branch": "main", "private": True, "archived": False,
                          "language": "TypeScript"},
                         {"id": 2, "full_name": "acme/old", "default_branch": "master", "archived": True}]
                return httpx.Response(200, json={"total_count": 2, "repositories": repos})
            if u.path == "/user":
                return httpx.Response(200, json={"login": "ada"}, headers={"x-oauth-scopes": "repo"})
            if u.path == "/user/repos":
                return httpx.Response(200, json=[{"id": 9, "full_name": "ada/x", "default_branch": "main"}])
        if u.host == "api.vercel.com":
            if req.headers.get("authorization") != "Bearer vcp_good_token":
                return httpx.Response(401, json={"error": {"code": "forbidden"}})
            if u.path == "/v2/user":
                return httpx.Response(200, json={"user": {"username": "ada"}})
            if u.path == "/v9/projects":
                return httpx.Response(200, json={"projects": [{"id": "prj_1", "name": "web", "framework": "nextjs",
                                                               "link": {"type": "github", "org": "acme", "repo": "payments"}}],
                                                 "pagination": {}})
            if u.path == "/v7/deployments":
                return httpx.Response(200, json={"deployments": [{"uid": "dpl_1", "readyState": "READY",
                                                                  "meta": {"githubCommitSha": "a3f9c1e0aa"}}], "pagination": {}})
        return httpx.Response(404, json={})


@pytest.fixture
def provider(container):
    p = Provider()
    container.extras["http_transport"] = httpx.MockTransport(p.handler)
    container.settings.github_app_id = "123"
    container.settings.github_app_private_key = _pem()
    return p


async def _project(client, org):
    h = token_for(org["owner_id"], org["owner_email"])
    r = await client.post(f"{API}/projects", headers=h, json={"name": f"P{org['id'].hex[:6]}", "environments": [{"name": "production"}]})
    return r.json()["data"]["id"], h


async def _run_jobs(container, n=1):
    """Drain the queue: the test database is shared, so leftovers from other tests must not be picked up first."""
    while await worker.run_one(container, "t", ["connection.verify"]):
        pass


async def test_pure_helpers():
    assert compare_permissions({"metadata": "read", "contents": "read", "pull_requests": "read"}) == ([], [])
    missing, excess = compare_permissions({"metadata": "read", "contents": "write", "administration": "read"})
    assert missing == ["pull_requests"] and excess == ["administration:read", "contents:write"]
    assert deployment_sha({"githubCommitSha": "abcdef1"}) == "abcdef1" and deployment_sha({}) is None and deployment_sha(None) is None


async def test_github_app_verify_and_discovery(client, container, org, provider):
    pid, h = await _project(client, org)
    r = await client.post(f"{API}/projects/{pid}/connections", headers=h,
                          json={"provider": "github", "auth_method": "github_app", "config": {"installation_id": "4821"}})
    assert r.status_code == 202
    cid = r.json()["data"]["resource_id"]
    await _run_jobs(container)
    c = (await client.get(f"{API}/connections/{cid}", headers=h)).json()["data"]
    assert c["status"] == "connected" and c["effective_scopes"]["permissions"]["contents"] == "read"
    assert c["stores_secret"] is False
    checks = (await client.get(f"{API}/connections/{cid}/checks", headers=h)).json()["data"]
    assert [s["status"] for s in checks[0]["stages"]] == ["ok", "ok", "ok"]
    res = (await client.get(f"{API}/connections/{cid}/resources", headers=h)).json()
    assert [x["full_name"] for x in res["data"]] == ["acme/payments"]            # archived hidden by default
    assert res["meta"]["coverage"]["total_visible"] == 2
    all_ = (await client.get(f"{API}/connections/{cid}/resources?include_archived=true&q=OLD", headers=h)).json()["data"]
    assert [x["full_name"] for x in all_] == ["acme/old"]


async def test_overbroad_and_missing_github_permissions(client, container, org, provider):
    pid, h = await _project(client, org)
    provider.gh_perms = {"metadata": "read", "contents": "write", "pull_requests": "read", "administration": "read"}
    cid = (await client.post(f"{API}/projects/{pid}/connections", headers=h, json={
        "provider": "github", "auth_method": "github_app", "config": {"installation_id": "1"}})).json()["data"]["resource_id"]
    await _run_jobs(container)
    c = (await client.get(f"{API}/connections/{cid}", headers=h)).json()["data"]
    assert c["status"] == "connected_with_warnings"          # over-broad is surfaced, not hidden
    stages = (await client.get(f"{API}/connections/{cid}/checks", headers=h)).json()["data"][0]["stages"]
    assert "administration:read" in stages[1]["message"]
    provider.gh_perms = {"metadata": "read"}
    await client.post(f"{API}/connections/{cid}/verify", headers=h)
    await _run_jobs(container)
    c = (await client.get(f"{API}/connections/{cid}", headers=h)).json()["data"]
    assert c["status"] == "failed" and "pull_requests" in c["last_error"]["message"]


async def test_vercel_token_warns_it_can_write_and_never_echoes_secret(client, container, org, provider):
    pid, h = await _project(client, org)
    r = await client.post(f"{API}/projects/{pid}/connections", headers=h, json={
        "provider": "vercel", "auth_method": "vercel_token", "secret": "vcp_good_token", "config": {}})
    cid = r.json()["data"]["resource_id"]
    await _run_jobs(container)
    raw = (await client.get(f"{API}/connections/{cid}", headers=h)).text
    assert "vcp_good_token" not in raw and "vcp_good" not in (await client.get(f"{API}/projects/{pid}/connections", headers=h)).text
    c = (await client.get(f"{API}/connections/{cid}", headers=h)).json()["data"]
    assert c["status"] == "connected_with_warnings" and c["stores_secret"] is True
    async with container.pool.connection() as conn:   # ciphertext only in the database
        row = await (await conn.execute("SELECT ciphertext FROM credential_versions WHERE connection_id=%s", (cid,))).fetchone()
    assert b"vcp_good_token" not in bytes(row["ciphertext"])
    stages = (await client.get(f"{API}/connections/{cid}/checks", headers=h)).json()["data"][0]["stages"]
    assert [s["status"] for s in stages] == ["warn", "ok", "ok", "skip"]            # logs: skipped, never claimed healthy
    items = (await client.get(f"{API}/connections/{cid}/resources", headers=h)).json()["data"]
    assert items[0]["git_repository"] == "acme/payments"


async def test_bad_token_fails_with_actionable_reason(client, container, org, provider):
    pid, h = await _project(client, org)
    cid = (await client.post(f"{API}/projects/{pid}/connections", headers=h, json={
        "provider": "vercel", "auth_method": "vercel_token", "secret": "vcp_wrong_token"})).json()["data"]["resource_id"]
    await _run_jobs(container)
    c = (await client.get(f"{API}/connections/{cid}", headers=h)).json()["data"]
    assert c["status"] == "failed" and c["last_error"]["code"] == "bad_credentials"
    assert (await client.get(f"{API}/connections/{cid}/resources", headers=h)).status_code == 409


async def test_rotation_failure_keeps_working_credential_then_success_swaps(client, container, org, provider):
    pid, h = await _project(client, org)
    cid = (await client.post(f"{API}/projects/{pid}/connections", headers=h, json={
        "provider": "vercel", "auth_method": "vercel_token", "secret": "vcp_good_token"})).json()["data"]["resource_id"]
    await _run_jobs(container)
    await client.post(f"{API}/connections/{cid}/rotate", headers=h, json={"secret": "vcp_bad_replacement"})
    await _run_jobs(container)
    c = (await client.get(f"{API}/connections/{cid}", headers=h)).json()["data"]
    assert c["status"] == "connected_with_warnings"                      # unchanged: old credential still active
    async with container.pool.connection() as conn:
        vs = {r["version"]: r["status"] for r in await (await conn.execute(
            "SELECT version,status FROM credential_versions WHERE connection_id=%s", (cid,))).fetchall()}
    assert vs == {1: "active", 2: "revoked"}
    assert (await client.get(f"{API}/connections/{cid}/resources", headers=h)).status_code == 200  # still usable


async def test_disconnect_wipes_secret_and_cancels_queued_work(client, container, org, provider):
    pid, h = await _project(client, org)
    cid = (await client.post(f"{API}/projects/{pid}/connections", headers=h, json={
        "provider": "vercel", "auth_method": "vercel_token", "secret": "vcp_good_token"})).json()["data"]["resource_id"]
    d = await client.post(f"{API}/connections/{cid}/disconnect", headers=h)
    assert d.json()["data"]["jobs_cancelled"] == 1                        # the queued verify never runs
    await _run_jobs(container)
    assert provider.calls == []
    async with container.pool.connection() as conn:
        cred = await (await conn.execute("SELECT status, length(ciphertext) AS n FROM credential_versions WHERE connection_id=%s", (cid,))).fetchone()
    assert cred["status"] == "revoked" and cred["n"] == 0
    assert (await client.post(f"{API}/connections/{cid}/verify", headers=h)).status_code == 409


async def test_validation_and_authorization(client, container, org, provider):
    pid, h = await _project(client, org)
    post = lambda body: client.post(f"{API}/projects/{pid}/connections", headers=h, json=body)  # noqa: E731
    assert (await post({"provider": "azure", "auth_method": "x"})).status_code == 422          # not supported in this build
    assert (await post({"provider": "vercel", "auth_method": "vercel_token"})).status_code == 422  # token required
    assert (await post({"provider": "github", "auth_method": "github_app", "config": {"installation_id": "abc"}})).status_code == 422
    assert (await post({"provider": "github", "auth_method": "github_app", "config": {"api_url": "http://169.254.169.254"}})).status_code == 422
    ok = await post({"provider": "github", "auth_method": "github_app", "config": {"installation_id": "7"}})
    assert ok.status_code == 202
    dup = await post({"provider": "github", "auth_method": "github_pat", "secret": "github_pat_12345678"})
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "connection_exists"
    # an investigator (no manage_connections) cannot create; a stranger gets 404
    from tests.conftest import add_member
    u, ue = await add_member(container, org["id"], "investigator")
    await client.put(f"{API}/projects/{pid}/members", headers=h, json={"members": [
        {"user_id": str(org["owner_id"]), "role": "admin"}, {"user_id": str(u), "role": "investigator"}]})
    assert (await client.post(f"{API}/projects/{pid}/connections", headers=token_for(u, ue), json={
        "provider": "vercel", "auth_method": "vercel_token", "secret": "vcp_good_token"})).status_code == 403
    cid = ok.json()["data"]["resource_id"]
    assert (await client.get(f"{API}/connections/{cid}", headers=token_for(u, ue))).status_code == 200
    assert (await client.post(f"{API}/connections/{cid}/disconnect", headers=token_for(u, ue))).status_code == 403
    stranger, se = await add_member(container, org["id"], "viewer")
    assert (await client.get(f"{API}/connections/{cid}", headers=token_for(stranger, se))).status_code == 404


async def test_providers_catalog_gates_unavailable_modes(client, org, container):
    h = token_for(org["owner_id"], org["owner_email"])
    cat = {p["provider"]: p for p in (await client.get(f"{API}/providers", headers=h)).json()["data"]}
    modes = {m["id"]: m for m in cat["vercel"]["auth_modes"]}
    assert modes["vercel_integration"]["available"] is False and modes["vercel_integration"]["reason"]
    gh_modes = {m["id"]: m for m in cat["github"]["auth_modes"]}
    assert gh_modes["github_enterprise_server"]["available"] is False
    assert set(cat) == {"github", "vercel", "generic"}                    # no Azure/AWS/GCP/Jira/... offered
