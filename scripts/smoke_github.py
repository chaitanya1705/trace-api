"""Real-account smoke test: project -> GitHub App connection -> verify job -> discovery, against live api.github.com.

Uses a throwaway embedded Postgres (no DATABASE_URL needed). Reads GITHUB_APP_ID / GITHUB_APP_PRIVATE_KEY_PATH from .env.
    python scripts/smoke_github.py <installation_id>
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.jobs.handlers  # noqa: E402,F401
from app.config import load_settings  # noqa: E402
from app.container import Container  # noqa: E402
from app.jobs import worker  # noqa: E402
from app.main import create_app  # noqa: E402
from app.storage.postgres.migrate import migrate  # noqa: E402
from app.storage.postgres.pool import open_pool  # noqa: E402
from app.storage.secrets import Vault  # noqa: E402


async def main(installation_id: str) -> int:
    import pgserver

    s = load_settings()
    s.auth_mode = "off"
    if not (s.github_app_id and s.github_app_private_key):
        print("GITHUB_APP_ID / GITHUB_APP_PRIVATE_KEY_PATH not configured")
        return 2
    srv = pgserver.get_server(Path(tempfile.mkdtemp(prefix="trace-smoke-")), cleanup_mode="stop")
    try:
        dsn = srv.get_uri()
        await migrate(dsn)
        c = Container(settings=s, pool=await open_pool(dsn), vault=Vault.from_settings(s))
        app = create_app(s, c)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as api:
            p = (await api.post("/api/v1/projects", json={"name": "Smoke", "environments": [{"name": "production"}]})).json()["data"]
            r = await api.post(f"/api/v1/projects/{p['id']}/connections",
                               json={"provider": "github", "auth_method": "github_app", "config": {"installation_id": installation_id}})
            cid = r.json()["data"]["resource_id"]
            await worker.run_one(c, "smoke", ["connection.verify"])  # real calls to api.github.com
            conn = (await api.get(f"/api/v1/connections/{cid}")).json()["data"]
            print("connection status:", conn["status"], "| last_error:", conn["last_error"])
            for st in (await api.get(f"/api/v1/connections/{cid}/checks")).json()["data"][0]["stages"]:
                print(f"  [{st['status']:>4}] {st['title']}: {st['message']}")
            res = (await api.get(f"/api/v1/connections/{cid}/resources")).json()
            print("repositories:", [x["full_name"] for x in res["data"]][:30], res["meta"]["coverage"])
        await c.pool.close()
        return 0 if conn["status"].startswith("connected") else 1
    finally:
        srv.cleanup()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1])))
