import pytest

from app.config import Settings
from tests.conftest import token_for

pytestmark = pytest.mark.db


async def test_unset_placeholders_report_not_configured(client, org):
    h = token_for(org["owner_id"], org["owner_email"])
    d = (await client.get("/api/v1/integrations/demo", headers=h)).json()["data"]
    assert d["pulselab"] == {"url": None, "configured": False} and d["shopflow_api"]["configured"] is False


async def test_configured_urls_are_normalized_and_pulselab_gets_cors(client, org, container):
    container.settings.pulselab_url = "https://pulselab.example.vercel.app"
    h = token_for(org["owner_id"], org["owner_email"])
    assert (await client.get("/api/v1/integrations/demo", headers=h)).json()["data"]["pulselab"]["configured"] is True
    assert "https://pulselab.example.vercel.app" in container.settings.allowed_origins


def test_url_validation_and_blank_means_unset():
    assert Settings(trace_env="test", pulselab_url="").pulselab_url is None
    assert Settings(trace_env="test", shopflow_api_url="https://x.app/").shopflow_api_url == "https://x.app"
    with pytest.raises(ValueError):
        Settings(trace_env="test", pulselab_url="javascript:alert(1)")


def test_private_key_can_be_read_from_file(tmp_path):
    f = tmp_path / "k.pem"
    f.write_text("-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n")
    s = Settings(trace_env="test", github_app_id="1", github_app_private_key_path=str(f))
    assert s.github_app_private_key.startswith("-----BEGIN")
    with pytest.raises(ValueError, match="not readable"):
        Settings(trace_env="test", github_app_private_key_path=str(tmp_path / "missing.pem"))


async def test_dev_cors_allows_any_localhost_port_but_not_other_sites(client):
    async def allowed(origin):
        r = await client.options("/api/v1/config", headers={"origin": origin, "access-control-request-method": "GET"})
        return r.headers.get("access-control-allow-origin") == origin

    assert await allowed("http://127.0.0.1:3001") and await allowed("http://localhost:4000")
    assert not await allowed("https://evil.example.com")
