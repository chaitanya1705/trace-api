"""Real Vercel check with the token in .env (VERCEL_TOKEN, VERCEL_TEAM_ID). Prints stages, projects and recent deployments."""
import asyncio
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import dotenv_values  # noqa: E402

from app.connectors import vercel as vc  # noqa: E402


async def main() -> int:
    env = {**dotenv_values(Path(__file__).resolve().parents[1] / ".env"), **os.environ}
    tok, team = env.get("VERCEL_TOKEN"), env.get("VERCEL_TEAM_ID")
    if not tok:
        print("VERCEL_TOKEN missing")
        return 2
    async with httpx.AsyncClient(timeout=20) as http:
        c = vc.VercelClient(http, tok, team)
        r = await vc.verify(c, auth_method="vercel_token", config={})
        print("outcome:", r.outcome)
        for s in r.stages:
            print(f"  [{s.status:>4}] {s.title}: {s.message}")
        projects, _ = await c.projects()
        for p in projects:
            v = vc.project_view(p)
            print("project:", v["name"], "|", v["git_repository"], "|", v["framework"])
            deps, _ = await c.deployments(project_id=p["id"], limit=3)
            for d in deps:
                print("   deploy", d.get("uid"), d.get("readyState"), d.get("target"), (vc.deployment_sha(d.get("meta")) or "no-sha")[:12])
    return 0 if r.outcome != "failed" else 1


raise SystemExit(asyncio.run(main()))
