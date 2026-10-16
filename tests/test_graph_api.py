"""Graph read routes: membership boundary and honest unavailability (no Neo4j needed)."""
import httpx
import pytest

from app.config import Settings
from app.container import Container
from app.main import create_app

pytestmark = pytest.mark.db


def _app(container, driver=None):
    s = Settings(trace_env="development", auth_mode="off")
    c = Container(settings=s, pool=container.pool)
    if driver is not None:
        c.extras["neo4j"] = driver
    return create_app(s, c)


async def test_graph_is_unavailable_not_empty_when_neo4j_is_not_configured(container):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(container)), base_url="http://t") as c:
        pid = (await c.post("/api/v1/projects", json={"name": "Graph Proj A"})).json()["data"]["id"]
        r = await c.get(f"/api/v1/projects/{pid}/graph/code")
        assert r.status_code == 503 and r.json()["error"]["code"] == "dependency_unavailable"


async def test_another_user_cannot_read_a_project_graph(container):
    class _Boom:  # a driver that must never be touched: authorization has to fail first
        def session(self, **_):
            raise AssertionError("graph queried without project access")

    app = _app(container, driver=_Boom())
    app.state.container.settings.neo4j_database = "x"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        pid = (await c.post("/api/v1/projects", json={"name": "Graph Proj B"})).json()["data"]["id"]
        stranger = {"x-dev-user": "stranger@trace.local"}
        assert (await c.get(f"/api/v1/projects/{pid}/graph/code", headers=stranger)).status_code in (403, 404)
        assert (await c.get(f"/api/v1/projects/{pid}/graph/symbols/sym_x", headers=stranger)).status_code in (403, 404)
