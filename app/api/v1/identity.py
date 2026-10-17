"""/me and invitation-only organization membership (R-001, R-019 essentials)."""
from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from psycopg import AsyncConnection
from pydantic import BaseModel, EmailStr, Field

from app.audit import audit
from app.auth.authz import can_invite_role
from app.auth.deps import current_principal, get_conn
from app.auth.jwt import Principal
from app.envelope import Envelope, wrap
from app.errors import Conflict, Forbidden, NotFound, Unprocessable

router = APIRouter(tags=["identity"])
Role = Literal["owner", "admin", "approver", "investigator", "viewer"]
INVITE_TTL = timedelta(days=7)


class InviteIn(BaseModel):
    email: EmailStr
    role: Role


class MembershipOut(BaseModel):
    organization_id: UUID
    organization_name: str
    role: str


class UserOut(BaseModel):
    id: UUID
    email: str | None
    display_name: str | None


class MeOut(BaseModel):
    user: UserOut
    memberships: list[MembershipOut]


class InvitationCreated(BaseModel):
    id: UUID
    email: str
    role: str
    expires_at: datetime
    token: str  # one-time; shown once, only its hash is stored


class InvitationState(BaseModel):
    id: UUID
    status: str


class AcceptedOut(BaseModel):
    organization_id: UUID
    role: str


class AcceptIn(BaseModel):
    token: str = Field(min_length=20, max_length=200)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class PublicConfig(BaseModel):
    auth_mode: str  # 'off' => the web app should skip login and open the workspace directly


@router.get("/config", response_model=Envelope[PublicConfig])
async def public_config(request: Request):
    """Unauthenticated, non-secret. The landing page's 'Open workspace' goes straight in when auth_mode is 'off'."""
    return wrap(request, {"auth_mode": request.app.state.container.settings.auth_mode})


@router.get("/me", response_model=Envelope[MeOut])
async def me(request: Request, p: Principal = Depends(current_principal), conn: AsyncConnection = Depends(get_conn)):
    if request.app.state.container.settings.auth_mode == "off":
        from app.auth.deps import _dev_membership  # no-login dev mode: first visit joins/creates the Dev Org

        has = await (await conn.execute("SELECT 1 FROM memberships WHERE user_id=%s AND status='active' LIMIT 1", (p.user_id,))).fetchone()
        if not has:
            await _dev_membership(conn, p)
    rows = await (
        await conn.execute(
            "SELECT m.id, m.organization_id, o.name AS organization_name, m.role FROM memberships m"
            " JOIN organizations o ON o.id = m.organization_id WHERE m.user_id = %s AND m.status = 'active'"
            " ORDER BY o.name",
            (p.user_id,),
        )
    ).fetchall()
    return wrap(
        request,
        {
            "user": {"id": p.user_id, "email": p.email, "display_name": p.display_name},
            "memberships": [
                {"organization_id": r["organization_id"], "organization_name": r["organization_name"], "role": r["role"]}
                for r in rows
            ],
        },
    )


async def _org_admin(conn: AsyncConnection, org_id: UUID, user_id: UUID) -> str:
    row = await (
        await conn.execute(
            "SELECT role FROM memberships WHERE organization_id = %s AND user_id = %s AND status = 'active'",
            (org_id, user_id),
        )
    ).fetchone()
    if row is None:
        raise NotFound("Organization not found")  # non-members learn nothing
    if row["role"] not in ("owner", "admin"):
        raise Forbidden("Only organization owners and admins manage invitations")
    return row["role"]


@router.post("/organizations/{org_id}/invitations", status_code=201, response_model=Envelope[InvitationCreated])
async def create_invitation(
    org_id: UUID,
    body: InviteIn,
    request: Request,
    p: Principal = Depends(current_principal),
    conn: AsyncConnection = Depends(get_conn),
):
    actor_role = await _org_admin(conn, org_id, p.user_id)
    if not can_invite_role(actor_role, body.role):
        raise Forbidden("Admins cannot grant the owner role")
    token = secrets.token_urlsafe(32)
    try:
        row = await (
            await conn.execute(
                "INSERT INTO invitations(organization_id, email, role, token_hash, invited_by, expires_at)"
                " VALUES (%s,%s,%s,%s,%s,%s) RETURNING id, email, role, expires_at",
                (org_id, body.email, body.role, _hash(token), p.user_id, datetime.now(UTC) + INVITE_TTL),
            )
        ).fetchone()
    except Exception as exc:  # unique partial index: one pending invite per email
        await conn.rollback()
        if "invitations_one_pending" in str(exc):
            raise Conflict("A pending invitation for this email already exists") from None
        raise
    await audit(conn, organization_id=org_id, actor=p.user_id, action="invitation.create",
                target_type="invitation", target_id=row["id"], request=request,
                details={"role": body.role, "email": body.email})
    await conn.commit()
    # No mail service is configured: the one-time token is returned to the inviter to deliver. It is never stored.
    return wrap(request, {**row, "token": token})


@router.post("/organizations/{org_id}/invitations/{invitation_id}/revoke", response_model=Envelope[InvitationState])
async def revoke_invitation(
    org_id: UUID,
    invitation_id: UUID,
    request: Request,
    p: Principal = Depends(current_principal),
    conn: AsyncConnection = Depends(get_conn),
):
    await _org_admin(conn, org_id, p.user_id)
    row = await (
        await conn.execute(
            "UPDATE invitations SET status = 'revoked' WHERE id = %s AND organization_id = %s AND status = 'pending'"
            " RETURNING id",
            (invitation_id, org_id),
        )
    ).fetchone()
    if row is None:
        raise NotFound("Pending invitation not found")
    await audit(conn, organization_id=org_id, actor=p.user_id, action="invitation.revoke",
                target_type="invitation", target_id=invitation_id, request=request)
    await conn.commit()
    return wrap(request, {"id": invitation_id, "status": "revoked"})


@router.post("/invitations/accept", response_model=Envelope[AcceptedOut])
async def accept_invitation(
    body: AcceptIn,
    request: Request,
    p: Principal = Depends(current_principal),
    conn: AsyncConnection = Depends(get_conn),
):
    generic = "Invitation is invalid or has expired"  # one message for every failure: no account/invite oracle
    if not p.email:
        raise Unprocessable(generic)
    inv = await (
        await conn.execute(
            "SELECT id, organization_id, email, role, status, expires_at FROM invitations WHERE token_hash = %s FOR UPDATE",
            (_hash(body.token),),
        )
    ).fetchone()
    if (
        inv is None
        or inv["status"] != "pending"
        or inv["expires_at"] < datetime.now(UTC)
        or inv["email"].lower() != p.email.lower()
    ):
        raise Unprocessable(generic)
    await conn.execute(
        "INSERT INTO memberships(organization_id, user_id, email, display_name, role) VALUES (%s,%s,%s,%s,%s)"
        " ON CONFLICT (organization_id, user_id) DO UPDATE SET role = EXCLUDED.role, status = 'active',"
        " revoked_at = NULL, authz_version = memberships.authz_version + 1",
        (inv["organization_id"], p.user_id, p.email, p.display_name, inv["role"]),
    )
    await conn.execute(
        "UPDATE invitations SET status = 'accepted', accepted_by = %s WHERE id = %s", (p.user_id, inv["id"])
    )
    await audit(conn, organization_id=inv["organization_id"], actor=p.user_id, action="invitation.accept",
                target_type="invitation", target_id=inv["id"], request=request, details={"role": inv["role"]})
    await conn.commit()
    return wrap(request, {"organization_id": inv["organization_id"], "role": inv["role"]})
