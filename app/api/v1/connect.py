"""'Connect GitHub' without a TRACE login: the project-setup page sends the user to GitHub to install/authorize the
app; GitHub redirects back here with an installation id. A signed, expiring `state` (bound to the draft/project and
the acting user) carries identity through the round trip, and the installation is verified against the GitHub API
before anything is stored. Vercel uses a pasted token (POST .../connections) since its integration isn't provisioned.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from psycopg import AsyncConnection
from pydantic import BaseModel

from app.audit import audit
from app.auth.authz import Cap
from app.auth.deps import OrgContext, get_conn, get_container, load_project_context, org_context
from app.connectors.base import ProviderError
from app.connectors.github import GitHubClient
from app.container import Container
from app.domain.connections import service as csvc
from app.envelope import wrap
from app.errors import Conflict, DependencyUnavailable, NotFound, Unauthenticated, Unprocessable
from app.jobs import queue

router = APIRouter(tags=["connect"])
STATE_TTL = 15 * 60


class StartIn(BaseModel):
    draft_id: UUID | None = None
    project_id: UUID | None = None


def _key(c: Container) -> bytes:
    secret = c.settings.trace_secret_key or c.settings.local_auth_secret
    return hashlib.sha256(b"trace-connect-state|" + secret.encode()).digest()


def sign_state(c: Container, payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps({**payload, "exp": int(time.time()) + STATE_TTL}, separators=(",", ":")).encode()).rstrip(b"=")
    sig = base64.urlsafe_b64encode(hmac.new(_key(c), body, hashlib.sha256).digest()).rstrip(b"=")
    return (body + b"." + sig).decode()


def read_state(c: Container, state: str) -> dict:
    try:
        body, sig = state.encode().split(b".")
        good = base64.urlsafe_b64encode(hmac.new(_key(c), body, hashlib.sha256).digest()).rstrip(b"=")
        if not hmac.compare_digest(sig, good):
            raise ValueError
        data = json.loads(base64.urlsafe_b64decode(body + b"=" * (-len(body) % 4)))
    except Exception:  # noqa: BLE001
        raise Unauthenticated("Invalid or tampered state", code="bad_state") from None
    if data.get("exp", 0) < time.time():
        raise Unauthenticated("The connect link expired. Start again.", code="state_expired")
    return data


def web_origin(c: Container) -> str | None:
    if c.settings.trace_web_origin:
        return c.settings.trace_web_origin.rstrip("/")
    return next((o for o in c.settings.allowed_origins if "localhost" in o or o.startswith("https://")), None)


async def _client(c: Container) -> tuple[GitHubClient, httpx.AsyncClient]:
    http = csvc.http_client(c)
    s = c.settings
    return GitHubClient(http, s.github_app_id, s.github_app_private_key), http


@router.post("/connect/github/start")
async def start(body: StartIn, request: Request, org: OrgContext = Depends(org_context), conn: AsyncConnection = Depends(get_conn),
                c: Container = Depends(get_container)):
    if bool(body.draft_id) == bool(body.project_id):
        raise Unprocessable("Send exactly one of draft_id or project_id")
    if body.project_id:
        (await load_project_context(conn, org, body.project_id)).require(Cap.MANAGE_CONNECTIONS)
    else:
        d = await conn.execute("SELECT created_by FROM project_drafts WHERE id=%s AND organization_id=%s AND status='open'",
                               (body.draft_id, org.organization_id))
        row = await d.fetchone()
        if row is None or (row["created_by"] != org.principal.user_id and not org.is_org_admin):
            raise NotFound("Draft not found")
    gh, http = await _client(c)
    try:
        slug = (await gh.app_info()).get("slug")
    except ProviderError as e:
        raise DependencyUnavailable(e.message, code=e.code) from None
    finally:
        await http.aclose()
    state = sign_state(c, {"org": str(org.organization_id), "user": str(org.principal.user_id),
                           "draft": str(body.draft_id) if body.draft_id else None,
                           "project": str(body.project_id) if body.project_id else None})
    return wrap(request, {"url": f"https://github.com/apps/{slug}/installations/new?state={state}", "expires_in": STATE_TTL})


async def bind_installation(conn: AsyncConnection, c: Container, request: Request, *, org_id: UUID, user_id: UUID, draft_id: UUID | None,
                            project_id: UUID | None, installation_id: int, setup_action: str | None) -> dict:
    """Confirm with GitHub that the installation belongs to OUR app, then create/reuse the code connection and queue verification."""
    gh, http = await _client(c)
    try:
        inst = await gh.installation(installation_id)
    except ProviderError as e:
        raise Conflict(f"GitHub did not confirm that installation: {e.message}", code=e.code) from None
    finally:
        await http.aclose()
    col, owner_id = ("project_id", project_id) if project_id else ("draft_id", draft_id)
    existing = await (await conn.execute(f"SELECT * FROM connections WHERE {col}=%s AND kind='code' AND status<>'disconnected'", (owner_id,))).fetchone()
    if existing is not None and existing["provider"] != "github":
        raise Conflict("This project already has a different code provider connected.", code="connection_exists")
    if existing is not None:
        row = await (await conn.execute(
            "UPDATE connections SET auth_method='github_app', config=config || %s::jsonb, status='draft', last_error=NULL WHERE id=%s RETURNING *",
            (json.dumps({"installation_id": str(installation_id)}), existing["id"]))).fetchone()
    else:
        row, _ = await csvc.create_connection(conn, c.vault, organization_id=org_id, actor=user_id, project_id=project_id, draft_id=draft_id,
                                              provider="github", auth_method="github_app", config={"installation_id": str(installation_id)},
                                              secret=None, secret_expires_at=None, request=request)
    await audit(conn, organization_id=org_id, project_id=project_id, actor=user_id, action="connection.github_installed",
                target_type="connection", target_id=row["id"], request=request,
                details={"installation_id": installation_id, "account": (inst.get("account") or {}).get("login"), "setup_action": setup_action})
    await queue.enqueue(conn, organization_id=org_id, project_id=project_id, kind="connection.verify", requested_by=user_id,
                        payload={"connection_id": str(row["id"]), "credential_version": None}, priority=queue.PRIORITY_HUMAN,
                        resource_type="connection", resource_id=row["id"], idempotency_key=f"install:{installation_id}:{row['version']}")
    await conn.commit()
    return row


@router.get("/connect/github/callback")
async def callback(
    request: Request, installation_id: int = Query(...), state: str = Query(...), setup_action: str | None = None,
    conn: AsyncConnection = Depends(get_conn), c: Container = Depends(get_container),
):
    """Public on purpose: the browser arrives from github.com with no token. `state` is the proof of who started this."""
    st = read_state(c, state)
    draft_id = UUID(st["draft"]) if st.get("draft") else None
    project_id = UUID(st["project"]) if st.get("project") else None
    await bind_installation(conn, c, request, org_id=UUID(st["org"]), user_id=UUID(st["user"]), draft_id=draft_id, project_id=project_id,
                            installation_id=installation_id, setup_action=setup_action)
    origin = web_origin(c)
    if origin:
        target = f"/projects/new/code?draft={draft_id}&connected=github" if draft_id else f"/projects/{project_id}/connectors?connected=github"
        return RedirectResponse(origin + target, status_code=303)
    return JSONResponse({"connected": "github"})


class Installation(BaseModel):
    id: int
    account: str | None
    account_type: str | None
    repository_selection: str | None
    suspended: bool


@router.get("/connect/github/installations")
async def list_installations(request: Request, _: OrgContext = Depends(org_context), c: Container = Depends(get_container)):
    """Existing installations of TRACE's GitHub App, for when the app is already installed (GitHub then does not redirect back).
    POC single-tenant behaviour: every installation of this app is listed. A multi-tenant deployment must not do this."""
    gh, http = await _client(c)
    try:
        items = await gh.list_installations()
    except ProviderError as e:
        raise DependencyUnavailable(e.message, code=e.code) from None
    finally:
        await http.aclose()
    out = [Installation(id=i["id"], account=(i.get("account") or {}).get("login"), account_type=(i.get("account") or {}).get("type"),
                        repository_selection=i.get("repository_selection"), suspended=bool(i.get("suspended_at"))).model_dump() for i in items]
    return wrap(request, out)


class AttachIn(BaseModel):
    installation_id: int
    draft_id: UUID | None = None
    project_id: UUID | None = None


@router.post("/connect/github/attach")
async def attach(body: AttachIn, request: Request, org: OrgContext = Depends(org_context), conn: AsyncConnection = Depends(get_conn),
                 c: Container = Depends(get_container)):
    """Use an existing installation without leaving TRACE."""
    if bool(body.draft_id) == bool(body.project_id):
        raise Unprocessable("Send exactly one of draft_id or project_id")
    if body.project_id:
        (await load_project_context(conn, org, body.project_id)).require(Cap.MANAGE_CONNECTIONS)
    else:
        row = await (await conn.execute("SELECT created_by FROM project_drafts WHERE id=%s AND organization_id=%s AND status='open'",
                                        (body.draft_id, org.organization_id))).fetchone()
        if row is None or (row["created_by"] != org.principal.user_id and not org.is_org_admin):
            raise NotFound("Draft not found")
    row = await bind_installation(conn, c, request, org_id=org.organization_id, user_id=org.principal.user_id, draft_id=body.draft_id,
                                  project_id=body.project_id, installation_id=body.installation_id, setup_action="attach")
    return wrap(request, {"connection_id": row["id"], "status": row["status"]})
