"""Operator commands. Signing up grants nothing (invitation-only), so the first owner is created here.

    python -m app.cli migrate
    python -m app.cli bootstrap-org --name "Acme" --owner-email you@example.com [--owner-name "You"]
"""
from __future__ import annotations

import argparse
import asyncio

import psycopg

from app.auth.local import user_id_for
from app.config import load_settings
from app.storage.postgres.migrate import migrate


async def bootstrap_org(dsn: str, name: str, email: str, display_name: str | None, user_id=None) -> tuple:
    """`user_id` must be the identity-provider id (Supabase sub). In local auth mode it is derived from the email."""
    uid = user_id or user_id_for(email)
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        org = await (await conn.execute("INSERT INTO organizations(name) VALUES (%s) RETURNING id", (name,))).fetchone()
        await conn.execute(
            "INSERT INTO memberships(organization_id,user_id,email,display_name,role) VALUES (%s,%s,%s,%s,'owner')",
            (org[0], uid, email.strip().lower(), display_name))
        await conn.execute(
            "INSERT INTO audit_events(organization_id,actor_user_id,action,result,target_type,target_id)"
            " VALUES (%s,%s,'organization.bootstrap','success','organization',%s)", (org[0], uid, str(org[0])))
        await conn.commit()
    return org[0], uid


async def github_check(s) -> int:
    """Real call to GitHub. Prints app name, permissions and installation ids; never prints the key."""
    import httpx

    from app.connectors.base import ProviderError
    from app.connectors.github import GitHubClient, compare_permissions

    if not (s.github_app_id and s.github_app_private_key):
        print("Set GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_PATH (or GITHUB_APP_PRIVATE_KEY) in .env")
        return 2
    async with httpx.AsyncClient(timeout=15) as http:
        gh = GitHubClient(http, s.github_app_id, s.github_app_private_key)
        try:
            app = await gh.app_info()
            print(f"App OK: {app.get('name')} (slug {app.get('slug')}), owner {(app.get('owner') or {}).get('login')}")
            missing, excess = compare_permissions(app.get("permissions") or {})
            print("App permissions:", app.get("permissions"))
            if missing:
                print("  MISSING (set these on the app page):", missing)
            if excess:
                print("  Broader than TRACE needs:", excess)
            insts = await gh.list_installations()
        except ProviderError as e:
            print(f"FAILED [{e.code}]: {e.message}")
            return 1
    if not insts:
        print("No installations yet. Install the app on your account/org (App page > Install App), then re-run.")
    for i in insts:
        print(f"Installation id {i['id']}: {(i.get('account') or {}).get('login')} ({i.get('repository_selection')})")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    sub.add_parser("github-check", help="verify the GitHub App id/key against api.github.com and list installations")
    b = sub.add_parser("bootstrap-org")
    b.add_argument("--name", required=True)
    b.add_argument("--owner-email", required=True)
    b.add_argument("--owner-name")
    b.add_argument("--owner-user-id", help="identity-provider user id (required unless AUTH_MODE=local)")
    a = ap.parse_args()
    s = load_settings()
    if a.cmd == "github-check":
        raise SystemExit(asyncio.run(github_check(s)))
    if not s.database_url:
        raise SystemExit("DATABASE_URL is not set")
    if a.cmd == "migrate":
        print("applied:", asyncio.run(migrate(s.database_url)) or "nothing to do")
        return
    if s.auth_mode != "local" and not a.owner_user_id:
        raise SystemExit("--owner-user-id is required with Supabase auth (use the user's auth.users id)")
    from uuid import UUID
    org_id, uid = asyncio.run(bootstrap_org(s.database_url, a.name, a.owner_email, a.owner_name,
                                            UUID(a.owner_user_id) if a.owner_user_id else None))
    print(f"organization {org_id} created; owner user id {uid}")


if __name__ == "__main__":
    main()
