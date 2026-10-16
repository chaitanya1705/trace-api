"""Real end-to-end log check against the live ShopFlow backend on Vercel (needs the API and worker running, VERCEL_TOKEN/VERCEL_TEAM_ID in .env).

Creates a project, connects Vercel, scopes shopflow-backend to production, captures live logs for N seconds while sending real checkout
requests to the deployed backend, then searches the stored logs. Prints a summary; never prints the token.
    python scripts/smoke_logs.py [--api http://localhost:8000] [--seconds 25]
"""
import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
SHOP = "https://shopflow-backend-gray.vercel.app"


async def wait_job(api: httpx.AsyncClient, job_id: str, timeout: float = 90) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = (await api.get(f"/jobs/{job_id}")).json()["data"]
        if j["status"] in ("succeeded", "failed", "dead", "cancelled"):
            return j
        await asyncio.sleep(1)
    raise SystemExit("job did not finish in time (is the worker running?)")


async def main(base: str, seconds: int) -> int:
    env = {**dotenv_values(ROOT / ".env"), **os.environ}
    tok, team = env.get("VERCEL_TOKEN"), env.get("VERCEL_TEAM_ID")
    if not tok:
        print("VERCEL_TOKEN missing in .env")
        return 2
    async with httpx.AsyncClient(base_url=base + "/api/v1", timeout=60) as api, httpx.AsyncClient(timeout=30) as shop:
        name = f"ShopFlow logs {int(time.time()) % 100000}"
        pid = (await api.post("/projects", json={"name": name, "environments": [{"name": "production"}]})).json()["data"]["id"]
        r = await api.post(f"/projects/{pid}/connections", json={"provider": "vercel", "auth_method": "vercel_token", "secret": tok, "config": {"team_id": team} if team else {}})
        cid = r.json()["data"]["resource_id"]
        await wait_job(api, r.json()["data"]["job_id"])
        res = (await api.get(f"/connections/{cid}/resources")).json()["data"]
        mine = next((x for x in res if x["name"] == "shopflow-backend"), None)
        if not mine:
            print("shopflow-backend not visible to this token:", [x["name"] for x in res])
            return 1
        await api.put(f"/projects/{pid}/log-scope", json={"environments": {"production": [{"provider_resource_id": mine["provider_resource_id"]}]}})
        print("project", pid, "| vercel project", mine["provider_resource_id"])
        cap = await api.post(f"/projects/{pid}/logs/capture", json={"seconds": seconds})
        print("capture queued:", cap.status_code)
        await asyncio.sleep(6)  # let the tail connect before sending traffic
        body = {"items": [{"product_id": "notebook", "quantity": 1}, {"product_id": "mug", "quantity": 1}], "coupon": "SAVE10", "fulfilment": "delivery", "address": {"postal_code": "560001"}, "payment_token": "demo-approved"}
        for i in range(5):
            x = await shop.post(f"{SHOP}/api/checkout", json=body, headers={"X-Demo-Run-ID": f"trace-smoke-{i}"})
            print(f"  checkout {i} -> HTTP {x.status_code}")
        j = await wait_job(api, cap.json()["data"]["job_id"], timeout=seconds + 60)
        print("capture:", j["status"], j.get("result_ref"), j.get("error"))
        lg = (await api.get(f"/projects/{pid}/logs?limit=100")).json()
        print("coverage:", lg["meta"]["coverage"])
        for row in lg["data"][:8]:
            print(" ", (row["event_time"] or "")[:19], (row["level"] or "-").ljust(7), (row["extra"].get("operation") or row["service_hint"] or "")[:22].ljust(22), (row["request_id"] or "")[:8], "|", row["message"][:70])
        st = (await api.get(f"/projects/{pid}/logs/status")).json()["data"]
        print("freshness:", [(r["name"], r["log_availability"], r["stored"]) for r in st["resources"]])
        return 0 if lg["meta"]["coverage"]["stored_total"] else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--seconds", type=int, default=25)
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.api, a.seconds)))
