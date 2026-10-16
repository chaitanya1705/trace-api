import httpx
import pytest

from app.auth.local import LocalTokenVerifier, issue_token, user_id_for
from app.cli import bootstrap_org
from app.config import Settings
from app.container import Container
from app.errors import Unauthenticated
from app.main import create_app

SECRET = "x" * 40


def test_local_auth_rejected_in_production():
    with pytest.raises(ValueError, match="not allowed in production"):
        Settings(trace_env="production", auth_mode="local", database_url="d", supabase_url="s", supabase_jwks_url="j",
                 neo4j_uri="n", neo4j_username="u", neo4j_password="p", openai_api_key="k",
                 trace_secret_key="A" * 43)


async def test_token_roundtrip_expiry_and_wrong_secret():
    v = LocalTokenVerifier(SECRET)
    p = await v.verify(issue_token(SECRET, "Ada@Example.com", "Ada"))
    assert p.user_id == user_id_for("ada@example.com") and p.email == "ada@example.com" and p.display_name == "Ada"
    with pytest.raises(Unauthenticated):
        await v.verify(issue_token("y" * 40, "ada@example.com"))
    with pytest.raises(Unauthenticated) as e:
        await v.verify(issue_token(SECRET, "ada@example.com", ttl_seconds=-5))
    assert e.value.code == "session_expired"


@pytest.mark.db
async def test_dev_login_bootstrap_and_me_without_supabase(migrated_dsn, container):
    s = Settings(trace_env="development", auth_mode="local", local_auth_secret=SECRET)
    c = Container(settings=s, pool=container.pool, verifier=LocalTokenVerifier(SECRET))
    app = create_app(s, c)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        tok = (await client.post("/api/v1/dev/token", json={"email": "dev@example.com"})).json()["access_token"]
        h = {"authorization": f"Bearer {tok}"}
        assert (await client.get("/api/v1/me", headers=h)).json()["data"]["memberships"] == []  # sign-in grants nothing
        await bootstrap_org(migrated_dsn, "Dev Org", "dev@example.com", "Dev")
        me = (await client.get("/api/v1/me", headers=h)).json()["data"]
        assert me["memberships"][0]["role"] == "owner"


@pytest.mark.db
async def test_dev_endpoint_absent_in_default_supabase_mode(client):
    assert (await client.post("/api/v1/dev/token", json={"email": "a@b.co"})).status_code == 404
