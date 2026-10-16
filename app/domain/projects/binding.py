"""Log scope, repository bindings and mappings. Selections are validated against live provider discovery at write
time (A5: 'no client-supplied IDs trusted without scope lookup'), so a stale or forged id cannot widen scope."""
from __future__ import annotations

from uuid import UUID

from psycopg import AsyncConnection
from pydantic import BaseModel, Field

from app.connectors.base import ProviderError
from app.container import Container
from app.domain.connections import service as csvc
from app.errors import Conflict, DependencyUnavailable, Unprocessable


class ScopeResource(BaseModel):
    provider_resource_id: str = Field(min_length=1, max_length=200)
    service_key: str | None = None


class RepoSelection(BaseModel):
    provider_repository_id: str = Field(min_length=1, max_length=100)
    selected_ref: str | None = Field(None, max_length=200)  # branch or explicit revision; defaults to default branch


class ServiceMap(BaseModel):
    service_key: str
    path_prefix: str = Field("", max_length=300)


class PathMap(BaseModel):
    runtime_prefix: str = Field(min_length=1, max_length=300)
    repo_prefix: str = Field(max_length=300)


# Honest availability: until a log drain / runtime-log read exists, log presence is *unknown*, never 'available'.
def availability_for(provider: str) -> tuple[str, str]:
    if provider == "vercel":
        return "unknown", "Log drain not configured yet; TRACE currently sees no runtime logs for this project."
    return "unknown", "Log availability not verified."


async def _connection(conn: AsyncConnection, project_id: UUID | None, draft_id: UUID | None, kind: str) -> dict | None:
    col, val = ("project_id", project_id) if project_id else ("draft_id", draft_id)
    return await (await conn.execute(f"SELECT * FROM connections WHERE {col}=%s AND kind=%s AND status<>'disconnected'", (val, kind))).fetchone()


async def validated_discovery(c: Container, conn: AsyncConnection, row: dict | None, what: str) -> dict[str, dict]:
    if row is None:
        raise Unprocessable(f"Connect {what} before selecting from it")
    try:
        items, _, truncated = await csvc.discover(c, conn, row, include_archived=True)
    except ProviderError as e:
        raise (DependencyUnavailable if e.retryable else Conflict)(e.message, code=e.code) from None
    key = "provider_repository_id" if row["provider"] == "github" else "provider_resource_id"
    return {i[key]: i for i in items}


async def apply_log_scope(c: Container, conn: AsyncConnection, *, organization_id: UUID, project_id: UUID, draft_id: UUID | None,
                          env_map: dict[str, list[ScopeResource]], connection_row: dict | None = None) -> int:
    row = connection_row or await _connection(conn, project_id, draft_id, "hosting")
    if not env_map:
        return 0
    known = await validated_discovery(c, conn, row, "a hosting provider")
    envs = {r["environment_key"]: r["id"] for r in await (await conn.execute(
        "SELECT id, environment_key FROM environments WHERE project_id=%s", (project_id,))).fetchall()}
    services = {r["service_key"]: r["id"] for r in await (await conn.execute(
        "SELECT id, service_key FROM services WHERE project_id=%s", (project_id,))).fetchall()}
    avail, note = availability_for(row["provider"])
    n = 0
    for env_key, resources in env_map.items():
        if env_key not in envs:
            raise Unprocessable(f"Unknown environment: {env_key}")
        await conn.execute("DELETE FROM log_scope_entries WHERE environment_id=%s AND connection_id=%s", (envs[env_key], row["id"]))
        for r in resources:
            item = known.get(r.provider_resource_id)
            if item is None:
                raise Unprocessable(f"Resource {r.provider_resource_id} is not visible to this connection")
            if r.service_key and r.service_key not in services:
                raise Unprocessable(f"Unknown service: {r.service_key}")
            await conn.execute(
                "INSERT INTO log_scope_entries(organization_id,project_id,environment_id,connection_id,provider_resource_id,"
                " resource_type,name,service_id,log_availability,availability_note) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (organization_id, project_id, envs[env_key], row["id"], r.provider_resource_id, item["resource_type"],
                 item["name"], services.get(r.service_key), avail, note))
            n += 1
    return n


async def apply_repositories(c: Container, conn: AsyncConnection, *, organization_id: UUID, project_id: UUID,
                             draft_id: UUID | None, repos: list[RepoSelection], connection_row: dict | None = None) -> list[dict]:
    row = connection_row or await _connection(conn, project_id, draft_id, "code")
    if not repos:
        return []
    known = await validated_discovery(c, conn, row, "a code provider")
    out = []
    for sel in repos:
        item = known.get(sel.provider_repository_id)
        if item is None:
            raise Unprocessable(f"Repository {sel.provider_repository_id} is not visible to this connection")
        b = await (await conn.execute(
            "INSERT INTO repository_bindings(organization_id,project_id,connection_id,provider_repository_id,full_name,"
            " default_branch,selected_ref,visibility,archived) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (project_id,connection_id,provider_repository_id) DO UPDATE SET selected_ref=EXCLUDED.selected_ref,"
            " full_name=EXCLUDED.full_name, default_branch=EXCLUDED.default_branch, archived=EXCLUDED.archived RETURNING *",
            (organization_id, project_id, row["id"], item["provider_repository_id"], item["full_name"], item["default_branch"],
             sel.selected_ref or item["default_branch"], "private" if item["private"] else "public", item["archived"]))).fetchone()
        out.append(b)
    return out
