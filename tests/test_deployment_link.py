"""App events name their Vercel deployment. When TRACE has not synced that deployment yet, the applier fetches it (if its Vercel project
is in log scope) so the events are tied to the deployment's service; events applied before that are re-linked later. Provider responses
are FIXTURES (httpx.MockTransport); the graph write is the recording double from test_evidence_intake."""
from __future__ import annotations

from uuid import UUID

import httpx
import pytest

from app.domain.ingest import graph_apply as ga
from tests.ingest_helpers import ev, push, rows
from tests.test_evidence_intake import graph  # noqa: F401  (fixture)
from tests.test_logs import RuntimeLogs, setup

pytestmark = pytest.mark.db
API = "/api/v1"


class Deployments(RuntimeLogs):
    """FIXTURE Vercel API: dpl_NEW belongs to the scoped project prj_1, dpl_OUT to a project outside the log scope."""

    def __init__(self):
        super().__init__()
        self.available = True
        self.reads: list[str] = []

    def handler(self, req):
        if req.url.host == "api.vercel.com" and req.url.path.startswith("/v13/deployments/"):
            dpl = req.url.path.rsplit("/", 1)[1]
            self.reads.append(dpl)
            if not self.available:
                return httpx.Response(503, json={"error": {"code": "unavailable"}})
            project = {"dpl_NEW": "prj_1", "dpl_OUT": "prj_OTHER"}.get(dpl)
            if project is None:
                return httpx.Response(404, json={"error": {"code": "not_found"}})
            return httpx.Response(200, json={"id": dpl, "projectId": project, "name": "api", "target": "production", "readyState": "READY",
                                             "createdAt": 1760000000000, "meta": {}})
        return super().handler(req)


async def _source(client, container, org, provider):
    pid, _, h, _ = await setup(client, container, org, provider)
    made = (await client.post(f"{API}/projects/{pid}/log-source", headers=h, json={"environment_key": "production"})).json()["data"]
    return pid, made["connection_id"], made["secret"]


async def _scopes(container, pid):
    return {r["source_record_key"]: r["graph_scope"] for r in await rows(container, "SELECT source_record_key, graph_scope FROM evidence_inbox WHERE project_id=%s", UUID(pid))}


async def test_unknown_deployment_is_fetched_and_events_get_its_service(client, container, org, graph):  # noqa: F811
    provider = Deployments()
    pid, cid, secret = await _source(client, container, org, provider)
    await push(client, cid, secret, [ev(1, deployment_id="dpl_NEW"), ev(2, deployment_id="dpl_OUT")])
    await ga.apply_pending(container, org["id"], UUID(pid), "w1")
    deps = {r["provider_deployment_id"]: r for r in await rows(container, "SELECT provider_deployment_id, project_name FROM deployments WHERE project_id=%s", UUID(pid))}
    assert "dpl_NEW" in deps and "dpl_OUT" not in deps                                   # outside the log scope: never stored
    sc = await _scopes(container, pid)
    assert sc["evt-1"]["service_key"] == "api" and sc["evt-1"]["service_via"] == "deployment_metadata" and not sc["evt-1"].get("relink")
    assert sc["evt-2"]["service_key"] is None
    reads = len(provider.reads)
    await push(client, cid, secret, [ev(3, deployment_id="dpl_NEW")])
    await ga.apply_pending(container, org["id"], UUID(pid), "w1")
    assert provider.reads.count("dpl_NEW") == 1 and len(provider.reads) - reads <= 1   # a known deployment is not fetched again


async def test_events_applied_before_their_deployment_was_known_are_relinked_once(client, container, org, graph):  # noqa: F811
    provider = Deployments()
    provider.available = False
    pid, cid, secret = await _source(client, container, org, provider)
    await push(client, cid, secret, [ev(1, deployment_id="dpl_NEW")])
    out = await ga.apply_pending(container, org["id"], UUID(pid), "w1")
    assert out["applied"] == 1 and (await _scopes(container, pid))["evt-1"]["service_key"] is None   # provider down: applied without a service

    provider.available = True
    out = await ga.apply_pending(container, org["id"], UUID(pid), "w1")
    assert out["relinked"] == 1 and out["applied"] == 1
    sc = (await _scopes(container, pid))["evt-1"]
    assert sc["service_key"] == "api" and sc["relink"] is True
    assert graph.bundles[-1].rows[0]["graph_scope"]["relink"] is True                  # the projection keeps the first-written node, adds edges
    again = await ga.apply_pending(container, org["id"], UUID(pid), "w1")
    assert again["relinked"] == 0 and again["applied"] == 0
