from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx
from fastapi import Request
from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation

from app.audit import audit
from app.config import Settings
from app.connectors import github as gh
from app.connectors import vercel as vc
from app.container import Container
from app.errors import Conflict, DependencyUnavailable, NotFound, Unprocessable
from app.jobs import queue
from app.storage.secrets import Sealed, Vault, mask

# provider -> (kind, allowed auth methods)
PROVIDERS = {"github": ("code", {"github_app", "github_pat"}), "vercel": ("hosting", {"vercel_token", "vercel_integration"}),
             "generic": ("hosting", {"signed_webhook"})}
SECRET_METHODS = {"github_pat", "vercel_token"}


def providers_catalog(s: Settings) -> list[dict[str, Any]]:
    """Versioned capability descriptors (A13). The web app gates unsupported setup modes from this, so a mode is
    listed as unavailable with a reason rather than silently missing. Nothing here is a verified claim until the
    deployment's own verify step passes."""
    app_ok = bool(s.github_app_id and s.github_app_private_key)
    return [
        {"provider": "github", "kind": "code", "descriptor_version": 1, "auth_modes": [
            {"id": "github_app", "label": "TRACE GitHub App", "recommended": True, "available": app_ok,
             "reason": None if app_ok else "The GitHub App is not configured on this TRACE deployment."},
            {"id": "github_pat", "label": "Fine-grained personal access token", "recommended": False, "available": True,
             "reason": "Trial mode: tied to one person and may expire."},
            {"id": "github_enterprise_server", "label": "GitHub Enterprise Server", "recommended": False, "available": False,
             "reason": "Not supported yet; self-hosted URLs need allowlisting and SSRF controls."}],
         "operations": ["read_repository", "list_repositories"], "writes": ["draft_pr (phase 3, separate remediation app)"]},
        {"provider": "vercel", "kind": "hosting", "descriptor_version": 1, "auth_modes": [
            {"id": "vercel_token", "label": "Access token", "recommended": False, "available": True,
             "reason": "Vercel tokens cannot be read-only; TRACE only sends read requests."},
            {"id": "vercel_integration", "label": "TRACE integration (read-only)", "recommended": True, "available": False,
             "reason": "The TRACE Vercel integration is not provisioned on this deployment."}],
         "operations": ["list_projects", "list_deployments"], "writes": []},
        {"provider": "generic", "kind": "hosting", "descriptor_version": 1, "auth_modes": [
            {"id": "signed_webhook", "label": "Signed webhook / upload", "recommended": True, "available": True, "reason": None}],
         "operations": ["ingest_events"], "writes": []},
    ]


def public(row: dict[str, Any], cred: dict | None = None) -> dict[str, Any]:
    """Masked metadata only. There is no code path that returns plaintext secrets."""
    return {
        "id": row["id"], "project_id": row["project_id"], "draft_id": row["draft_id"], "provider": row["provider"],
        "kind": row["kind"], "auth_method": row["auth_method"], "status": row["status"], "config": row["config"],
        "effective_scopes": row["effective_scopes"], "last_verified_at": row["last_verified_at"],
        "last_error": row["last_error"], "credential_expires_at": row["credential_expires_at"],
        "stores_secret": row["auth_method"] in SECRET_METHODS, "secret_hint": (cred or {}).get("hint"),
        "version": row["version"], "updated_at": row["updated_at"],
    }


def http_client(c: Container) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=c.extras.get("http_transport"), timeout=15.0, follow_redirects=False)


def build_adapter(c: Container, row: dict, secret: str | None):
    """Returns (client, closer). GitHub app creds come from env and are the READ app only."""
    http = http_client(c)
    s = c.settings
    if row["provider"] == "github":
        return gh.GitHubClient(http, s.github_app_id, s.github_app_private_key), http
    if row["provider"] == "vercel":
        return vc.VercelClient(http, secret or "", (row["config"] or {}).get("team_id")), http
    raise Unprocessable("Provider has no API client")


async def active_secret(conn: AsyncConnection, vault: Vault | None, row: dict, version: int | None = None) -> str | None:
    if row["auth_method"] not in SECRET_METHODS:
        return None
    if vault is None:
        raise DependencyUnavailable("Credential vault is not configured")
    q = ("SELECT * FROM credential_versions WHERE connection_id=%s AND "
         + ("version=%s" if version is not None else "status='active'"))
    cred = await (await conn.execute(q, (row["id"], version) if version is not None else (row["id"],))).fetchone()
    if cred is None:
        return None
    return vault.open(Sealed(cred["key_version"], bytes(cred["nonce"]), bytes(cred["ciphertext"])),
                      connection_id=str(row["id"]), version=cred["version"], kind=cred["secret_kind"])


async def store_secret(conn: AsyncConnection, vault: Vault, *, organization_id: UUID, connection_id: UUID, secret: str,
                       kind: str, actor: UUID, expires_at: datetime | None) -> int:
    ver = (await (await conn.execute("SELECT COALESCE(max(version),0)+1 AS v FROM credential_versions WHERE connection_id=%s",
                                     (connection_id,))).fetchone())["v"]
    sealed = vault.seal(secret, connection_id=str(connection_id), version=ver, kind=kind)
    await conn.execute(
        "INSERT INTO credential_versions(organization_id,connection_id,version,key_version,nonce,ciphertext,secret_kind,"
        " status,expires_at,created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s)",
        (organization_id, connection_id, ver, sealed.key_version, sealed.nonce, sealed.ciphertext, kind, expires_at, actor))
    return ver


async def create_connection(
    conn: AsyncConnection, vault: Vault | None, *, organization_id: UUID, actor: UUID, project_id: UUID | None,
    draft_id: UUID | None, provider: str, auth_method: str, config: dict, secret: str | None,
    secret_expires_at: datetime | None, request: Request | None,
) -> tuple[dict, int | None]:
    kind, methods = PROVIDERS.get(provider, (None, set()))
    if kind is None or auth_method not in methods:
        raise Unprocessable("Unsupported provider or auth method for this build (supported: GitHub, Vercel)")
    if auth_method in SECRET_METHODS and not secret:
        raise Unprocessable("This auth method needs a token")
    if auth_method not in SECRET_METHODS and secret:
        raise Unprocessable("This auth method does not take a stored secret")
    if secret and vault is None:
        raise DependencyUnavailable("Credential vault is not configured")
    try:
        row = await (await conn.execute(
            "INSERT INTO connections(organization_id,project_id,draft_id,provider,kind,auth_method,config,created_by)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s) RETURNING *",
            (organization_id, project_id, draft_id, provider, kind, auth_method, json.dumps(config), actor))).fetchone()
    except UniqueViolation:
        await conn.rollback()
        raise Conflict(f"This project already has a {kind} connection. Disconnect it first to switch provider.",
                       code="connection_exists") from None
    ver = None
    if secret:
        ver = await store_secret(conn, vault, organization_id=organization_id, connection_id=row["id"], secret=secret,
                                 kind=auth_method, actor=actor, expires_at=secret_expires_at)
    await audit(conn, organization_id=organization_id, project_id=project_id, actor=actor, action="connection.create",
                target_type="connection", target_id=row["id"], request=request,
                details={"provider": provider, "auth_method": auth_method, "draft": draft_id is not None})
    return row, ver


async def enqueue_verify(conn: AsyncConnection, row: dict, actor: UUID, credential_version: int | None = None) -> dict:
    return await queue.enqueue(
        conn, organization_id=row["organization_id"], project_id=row["project_id"], kind="connection.verify",
        payload={"connection_id": str(row["id"]), "credential_version": credential_version}, requested_by=actor,
        priority=queue.PRIORITY_HUMAN, resource_type="connection", resource_id=row["id"],
        idempotency_key=f"verify:{row['id']}:v{row['version']}:c{credential_version}")


def accepted(job: dict, resource_id: UUID) -> dict:
    """202 body for async commands (A5)."""
    return {"job_id": job["id"], "resource_id": resource_id, "status": job["status"], "events_url": f"/api/v1/jobs/{job['id']}"}


async def get_connection_row(conn: AsyncConnection, org_id: UUID, connection_id: UUID) -> dict:
    row = await (await conn.execute("SELECT * FROM connections WHERE id=%s AND organization_id=%s", (connection_id, org_id))).fetchone()
    if row is None:
        raise NotFound("Connection not found")
    return row


def hint_for(secret_plain: str | None) -> str | None:
    return mask(secret_plain) if secret_plain else None


async def discover(c: Container, conn: AsyncConnection, row: dict, *, include_archived: bool = False) -> tuple[list[dict], int, bool]:
    """Provider discovery (credentials stay server-side). Raises ProviderError; callers map it. Bounded to 5 pages."""
    if row["status"] not in ("connected", "connected_with_warnings"):
        raise Conflict("Connection is not connected. Verify it first.", code="connection_not_ready")
    secret = await active_secret(conn, c.vault, row)
    client, http = build_adapter(c, row, secret)
    try:
        if row["provider"] == "github":
            if row["auth_method"] == "github_app":
                token, _ = await client.installation_token(row["config"]["installation_id"])
                repos, total, truncated = await client.list_installation_repos(token)
            else:
                repos, truncated = await client.list_user_repos(secret)
                total = len(repos)
            items = [gh.repo_view(r) for r in repos]
            if not include_archived:
                items = [i for i in items if not i["archived"]]
            return items, total, truncated
        if row["provider"] == "vercel":
            projects, truncated = await client.projects()
            return [vc.project_view(p) for p in projects], len(projects), truncated
        return [], 0, False
    finally:
        await http.aclose()
