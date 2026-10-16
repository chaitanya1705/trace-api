"""Append-only audit writer. Details must already be free of secrets and raw evidence."""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import Request
from psycopg import AsyncConnection

_FORBIDDEN_KEYS = ("secret", "token", "password", "private_key", "authorization")


def _scrub(details: dict[str, Any]) -> dict[str, Any]:
    return {k: ("[redacted]" if any(f in k.lower() for f in _FORBIDDEN_KEYS) else v) for k, v in details.items()}


async def audit(
    conn: AsyncConnection,
    *,
    organization_id: UUID,
    actor: UUID | None,
    action: str,
    result: str = "success",
    target_type: str | None = None,
    target_id: str | UUID | None = None,
    project_id: UUID | None = None,
    request: Request | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        "INSERT INTO audit_events(organization_id, project_id, actor_user_id, action, target_type, target_id,"
        " result, correlation_id, details) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
        (
            organization_id,
            project_id,
            actor,
            action,
            target_type,
            None if target_id is None else str(target_id),
            result,
            getattr(request.state, "request_id", None) if request else None,
            json.dumps(_scrub(details or {}), default=str),
        ),
    )
