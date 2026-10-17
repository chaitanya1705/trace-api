"""FastAPI dependencies: authenticated principal -> organization membership -> project capabilities."""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, Request
from psycopg import AsyncConnection

from app.auth.authz import ORG_ADMIN_ROLES, Cap, effective_caps, project_level_caps
from app.auth.jwt import Principal
from app.container import Container
from app.errors import DependencyUnavailable, Forbidden, NotFound, Unauthenticated


async def get_container(request: Request) -> Container:
    return request.app.state.container


async def get_conn(request: Request):
    """Pooled connection per request. Command handlers commit explicitly *before* returning, so a client never
    observes a response for a write that has not committed; anything uncommitted is rolled back on release."""
    c: Container = request.app.state.container
    if c.pool is None:
        raise DependencyUnavailable("Database is not configured")
    async with c.pool.connection() as conn:
        yield conn


DEV_EMAIL = "dev@trace.local"


async def current_principal(request: Request, c: Container = Depends(get_container)) -> Principal:
    if c.settings.auth_mode == "off":  # development only (config forbids it in production): no login screen
        from app.auth.local import user_id_for

        email = (request.headers.get("x-dev-user") or DEV_EMAIL).strip().lower()
        return Principal(user_id_for(email), email, email.split("@")[0])
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthenticated("Sign in required")
    if c.verifier is None:
        raise DependencyUnavailable("Authentication is not configured")
    return await c.verifier.verify(token)


def c_settings(request: Request):
    return request.app.state.container.settings


async def _dev_membership(conn: AsyncConnection, p: Principal) -> dict:
    """auth_mode=off only: join (or create) the single 'Dev Org'. The first user owns it, others are investigators."""
    org = await (await conn.execute("SELECT id FROM organizations WHERE name='Dev Org' ORDER BY created_at LIMIT 1")).fetchone()
    first = org is None
    if first:
        org = await (await conn.execute("INSERT INTO organizations(name) VALUES ('Dev Org') RETURNING id")).fetchone()
    row = await (await conn.execute(
        "INSERT INTO memberships(organization_id,user_id,email,display_name,role) VALUES (%s,%s,%s,%s,%s)"
        " ON CONFLICT (organization_id,user_id) DO UPDATE SET status='active' RETURNING id, organization_id, role",
        (org["id"], p.user_id, p.email, p.display_name, "owner" if first else "admin"))).fetchone()
    await conn.commit()
    return row


@dataclass(frozen=True)
class OrgContext:
    principal: Principal
    organization_id: UUID
    org_role: str
    membership_id: UUID

    @property
    def is_org_admin(self) -> bool:
        return self.org_role in ORG_ADMIN_ROLES


async def org_context(
    request: Request,
    principal: Principal = Depends(current_principal),
    conn: AsyncConnection = Depends(get_conn),
) -> OrgContext:
    """Active membership required. The organization is chosen with X-Organization-Id, or implied when the user
    belongs to exactly one. A revoked/disabled member is rejected here on every request."""
    wanted = request.headers.get("x-organization-id")
    sql = (
        "SELECT id, organization_id, role FROM memberships WHERE user_id = %s AND status = 'active'"
        + (" AND organization_id = %s" if wanted else "")
        + " ORDER BY created_at"
    )
    try:
        params: tuple = (principal.user_id, UUID(wanted)) if wanted else (principal.user_id,)
    except ValueError:
        raise NotFound("Organization not found") from None
    rows = await (await conn.execute(sql, params)).fetchall()
    if not rows and c_settings(request).auth_mode == "off":
        rows = [await _dev_membership(conn, principal)]
    if not rows:
        raise Forbidden("No active organization membership. Ask an organization owner for an invitation.")
    if len(rows) > 1:
        raise Forbidden(
            "Multiple organizations: send X-Organization-Id", code="organization_required"
        )
    r = rows[0]
    return OrgContext(principal, r["organization_id"], r["role"], r["id"])


@dataclass(frozen=True)
class ProjectContext:
    org: OrgContext
    project_id: UUID
    caps: frozenset[Cap]

    def require(self, cap: Cap) -> None:
        if cap not in self.caps:
            raise Forbidden(f"Missing permission: {cap.value}")


async def load_project_context(
    conn: AsyncConnection, org: OrgContext, project_id: UUID, environment_id: str | None = None
) -> ProjectContext:
    """404 for absent *or inaccessible* projects (A5): existence is not disclosed to non-members."""
    proj = await (
        await conn.execute(
            "SELECT id FROM projects WHERE id = %s AND organization_id = %s AND status = 'active'",
            (project_id, org.organization_id),
        )
    ).fetchone()
    if proj is None:
        raise NotFound("Project not found")
    grants = await (
        await conn.execute(
            "SELECT role, environment_ids FROM project_memberships WHERE project_id = %s AND user_id = %s",
            (project_id, org.principal.user_id),
        )
    ).fetchall()
    parsed = [(g["role"], None if g["environment_ids"] is None else [str(e) for e in g["environment_ids"]])
              for g in grants]
    caps = effective_caps(parsed, environment_id) if environment_id else project_level_caps(parsed)
    if Cap.READ not in caps:
        raise NotFound("Project not found")
    return ProjectContext(org, project_id, caps)


def project_access(cap: Cap = Cap.READ):
    """Dependency factory: `ctx: ProjectContext = Depends(project_access(Cap.MANAGE_CONNECTIONS))`."""

    async def dep(
        project_id: UUID,
        org: OrgContext = Depends(org_context),
        conn: AsyncConnection = Depends(get_conn),
    ) -> ProjectContext:
        ctx = await load_project_context(conn, org, project_id)
        ctx.require(cap)
        return ctx

    return dep
