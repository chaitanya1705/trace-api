import httpx
import pytest

from app.config import Settings
from app.container import Container
from app.main import create_app

pytestmark = pytest.mark.db


async def test_auth_off_needs_no_login_and_first_user_owns_dev_org(container):
    s = Settings(trace_env="development", auth_mode="off")
    app = create_app(s, Container(settings=s, pool=container.pool))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/v1/projects", json={"name": "NoLogin Proj"})  # no Authorization header at all
        assert r.status_code == 201, r.text
        other = await c.get("/api/v1/projects", headers={"x-dev-user": "second@trace.local"})
        assert other.json()["data"] == []  # a second dev user has no grant on it: isolation still applies
        assert [p["name"] for p in (await c.get("/api/v1/projects")).json()["data"]] == ["NoLogin Proj"]


def test_off_is_rejected_in_production():
    with pytest.raises(ValueError, match="not allowed in production"):
        Settings(trace_env="production", auth_mode="off", database_url="d", supabase_url="s", supabase_jwks_url="j",
                 neo4j_uri="n", neo4j_username="u", neo4j_password="p", openai_api_key="k", trace_secret_key="A" * 43)


async def test_open_workspace_flow_without_any_login(container):
    s = Settings(trace_env="development", auth_mode="off")
    app = create_app(s, Container(settings=s, pool=container.pool))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/api/v1/config")).json()["data"]["auth_mode"] == "off"
        me = (await c.get("/api/v1/me")).json()["data"]          # no token: still a usable account
        assert me["user"]["email"] == "dev@trace.local" and me["memberships"][0]["organization_name"] == "Dev Org"


_PROD = dict(trace_env="production", database_url="d", neo4j_uri="n", neo4j_username="u", neo4j_password="p", openai_api_key="k", trace_secret_key="A" * 43)


def test_open_access_in_production_needs_an_explicit_opt_in_and_no_supabase():
    with pytest.raises(ValueError, match="ALLOW_OPEN_ACCESS"):
        Settings(auth_mode="off", **_PROD)
    s = Settings(auth_mode="off", allow_open_access=True, **_PROD)       # supabase settings are not required when there is no login
    assert s.trace_env == "production" and s.auth_mode == "off"
    with pytest.raises(ValueError, match="SUPABASE_URL"):                  # real auth still demands them
        Settings(auth_mode="supabase", **_PROD)


def test_web_origin_prefers_the_explicit_setting():
    from app.api.v1.connect import web_origin
    from app.container import Container

    s = Settings(trace_env="test", trace_allowed_origins="https://a.example,https://b.example", trace_web_origin="https://web.example/")
    assert web_origin(Container(settings=s)) == "https://web.example"
    assert web_origin(Container(settings=Settings(trace_env="test", trace_allowed_origins="https://a.example"))) == "https://a.example"
