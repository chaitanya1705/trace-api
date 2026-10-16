from urllib.parse import parse_qs, urlparse

import httpx
import pytest

import app.jobs.handlers  # noqa: F401
from app.api.v1.connect import read_state, sign_state
from app.errors import Unauthenticated
from app.jobs import worker
from tests.conftest import token_for
from tests.test_connections import Provider, _pem

pytestmark = pytest.mark.db
API = "/api/v1"


@pytest.fixture
def provider(container):
    p = Provider()
    base = p.handler

    def handler(req):
        if req.url.host == "api.github.com" and req.url.path == "/app":
            return httpx.Response(200, json={"slug": "traceai-app", "name": "traceai-app", "permissions": p.gh_perms})
        return base(req)

    container.extras["http_transport"] = httpx.MockTransport(handler)
    container.settings.github_app_id = "123"
    container.settings.github_app_private_key = _pem()
    return p


def test_state_is_signed_expiring_and_tamper_proof(container):
    s = sign_state(container, {"user": "u"})
    assert read_state(container, s)["user"] == "u"
    body, sig = s.split(".")
    with pytest.raises(Unauthenticated):
        read_state(container, body + "." + sig[:-2] + "AA")
    with pytest.raises(Unauthenticated):
        read_state(container, "garbage")
    import app.api.v1.connect as m
    real = m.time.time
    m.time.time = lambda: real() + 3600
    try:
        with pytest.raises(Unauthenticated) as e:
            read_state(container, s)
        assert e.value.code == "state_expired"
    finally:
        m.time.time = real


async def test_connect_flow_from_draft_without_a_trace_login(client, container, org, provider):
    h = token_for(org["owner_id"], org["owner_email"])
    did = (await client.post(f"{API}/project-drafts", headers=h, json={})).json()["data"]["id"]
    start = await client.post(f"{API}/connect/github/start", headers=h, json={"draft_id": did})
    url = start.json()["data"]["url"]
    assert url.startswith("https://github.com/apps/traceai-app/installations/new?state=")
    state = parse_qs(urlparse(url).query)["state"][0]
    # GitHub redirects the browser back with NO Authorization header - the signed state is the proof
    cb = await client.get(f"{API}/connect/github/callback", params={"installation_id": 4821, "state": state, "setup_action": "install"},
                          follow_redirects=False)
    assert cb.status_code == 303 and "connected=github" in cb.headers["location"] and f"draft={did}" in cb.headers["location"]
    while await worker.run_one(container, "t", ["connection.verify"]):
        pass
    conns = (await client.get(f"{API}/project-drafts/{did}/connections", headers=h)).json()["data"]
    assert conns[0]["provider"] == "github" and conns[0]["status"] == "connected" and conns[0]["config"]["installation_id"] == "4821"
    repos = (await client.get(f"{API}/connections/{conns[0]['id']}/resources", headers=h)).json()["data"]
    assert [r["full_name"] for r in repos] == ["acme/payments"]
    # reinstalling / re-running the callback reuses the same connection instead of failing or duplicating
    again = await client.get(f"{API}/connect/github/callback", params={"installation_id": 4821, "state": state}, follow_redirects=False)
    assert again.status_code == 303
    assert len((await client.get(f"{API}/project-drafts/{did}/connections", headers=h)).json()["data"]) == 1


async def test_callback_rejects_bad_state_and_foreign_installation(client, container, org, provider):
    assert (await client.get(f"{API}/connect/github/callback", params={"installation_id": 1, "state": "x.y"})).status_code == 401
    state = sign_state(container, {"org": str(org["id"]), "user": str(org["owner_id"]), "draft": None, "project": None})
    h = token_for(org["owner_id"], org["owner_email"])
    did = (await client.post(f"{API}/project-drafts", headers=h, json={})).json()["data"]["id"]
    good = sign_state(container, {"org": str(org["id"]), "user": str(org["owner_id"]), "draft": did, "project": None})
    orig = container.extras["http_transport"]
    container.extras["http_transport"] = httpx.MockTransport(lambda r: httpx.Response(404, json={}))   # GitHub: not our installation
    r = await client.get(f"{API}/connect/github/callback", params={"installation_id": 999, "state": good})
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_found"
    container.extras["http_transport"] = orig
    assert state  # a state with neither draft nor project is unusable
    s = await client.post(f"{API}/connect/github/start", headers=h, json={})
    assert s.status_code == 422
    s2 = await client.post(f"{API}/connect/github/start", headers=h, json={"draft_id": "00000000-0000-0000-0000-000000000000"})
    assert s2.status_code == 404
