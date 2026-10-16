"""Pure authorization rules (R-019). No IO so every rule is unit-testable.

Capabilities are unioned across a person's scoped grants. Roles are deliberately NOT a ladder:
- an Admin administers sources/members but is not thereby an eligible patch approver (separation of duty),
- an Approver cannot administer connectors,
- organization membership never grants project data access on its own.
"""
from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class Cap(StrEnum):
    READ = "read"
    MANAGE_PROJECT = "manage_project"
    MANAGE_CONNECTIONS = "manage_connections"
    MANAGE_MEMBERS = "manage_members"
    INDEX = "index"
    INVESTIGATE = "investigate"
    ANSWER_REQUESTS = "answer_requests"
    CHAT = "chat"
    PROPOSE = "propose"
    APPROVE = "approve"
    EXPORT = "export"


PROJECT_ROLE_CAPS: dict[str, frozenset[Cap]] = {
    "viewer": frozenset({Cap.READ}),
    "investigator": frozenset(
        {Cap.READ, Cap.INDEX, Cap.INVESTIGATE, Cap.ANSWER_REQUESTS, Cap.CHAT, Cap.PROPOSE, Cap.EXPORT}
    ),
    "approver": frozenset({Cap.READ, Cap.APPROVE}),
    "admin": frozenset(
        {Cap.READ, Cap.MANAGE_PROJECT, Cap.MANAGE_CONNECTIONS, Cap.MANAGE_MEMBERS, Cap.INDEX, Cap.EXPORT}
    ),
}

ORG_ROLES = ("owner", "admin", "approver", "investigator", "viewer")
ORG_ADMIN_ROLES = frozenset({"owner", "admin"})


def caps_for(roles: Iterable[str]) -> frozenset[Cap]:
    out: set[Cap] = set()
    for r in roles:
        out |= PROJECT_ROLE_CAPS.get(r, frozenset())
    return frozenset(out)


def environment_allowed(grant_env_ids: list[str] | None, environment_id: str | None) -> bool:
    """grant_env_ids None = all environments. A grant limited to some environments never covers project-wide
    (environment_id None) administrative actions."""
    if grant_env_ids is None:
        return True
    return environment_id is not None and environment_id in grant_env_ids


def effective_caps(grants: Iterable[tuple[str, list[str] | None]], environment_id: str | None) -> frozenset[Cap]:
    """grants: (role, environment_ids|None). Only grants covering the environment contribute."""
    return caps_for(role for role, envs in grants if environment_allowed(envs, environment_id))


def project_level_caps(grants: Iterable[tuple[str, list[str] | None]]) -> frozenset[Cap]:
    """Project-wide view: capabilities from unrestricted grants only, plus READ for any grant at all.
    An environment-limited grant lets the holder see the project shell (names, environments they hold) but its
    investigate/approve/etc. capabilities apply only when an environment is supplied (effective_caps)."""
    grants = list(grants)
    caps = effective_caps(grants, None)
    return caps | {Cap.READ} if grants else caps


def can_invite_role(actor_org_role: str, target_role: str) -> bool:
    """Owners may grant anything; Admins may not mint Owners."""
    if actor_org_role == "owner":
        return True
    return actor_org_role == "admin" and target_role != "owner"
