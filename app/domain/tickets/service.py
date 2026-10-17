from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

from app.audit import audit
from app.auth.authz import Cap
from app.auth.deps import OrgContext, load_project_context
from app.domain.tickets.pulselab import TicketFields
from app.errors import Forbidden, NotFound, Unprocessable
from app.redact import redact_json, redact_text

EDIT_ORG_ROLES = ("owner", "admin", "investigator")

# Becomes true when the orchestrator (T-012) exists. Until then the Investigate action is visibly unavailable (PRD:
# later-phase actions show a reason) rather than creating runs that nothing will ever execute.
INVESTIGATOR_AVAILABLE = False


def investigate_state(row: dict) -> dict[str, Any]:
    if row["project_id"] is None:
        return {"available": False, "reason": "Assign this ticket to a project first."}
    if row["status"] != "open":
        return {"available": False, "reason": "Ticket is not open."}
    if not INVESTIGATOR_AVAILABLE:
        return {"available": False, "reason": "The investigator is not available in this build yet."}
    return {"available": True, "reason": None}


def content_hash(f: TicketFields, payload: Any) -> str:
    core = {"title": f.title, "description": f.description, "severity": f.severity, "environment": f.environment_key,
            "service": f.service_hint, "ws": f.window_start, "we": f.window_end, "payload": payload}
    return hashlib.sha256(json.dumps(core, sort_keys=True, default=str).encode()).hexdigest()


def view(row: dict, project_name: str | None = None, *, detail: bool = False) -> dict[str, Any]:
    out = {
        "id": row["id"], "number": row["number"], "display_id": f"TKT-{row['number']}", "project_id": row["project_id"],
        "project_name": project_name, "source": row["source"], "external_id": row["external_id"], "title": row["title"],
        "severity": row["severity"], "environment_key": row["environment_key"], "service_hint": row["service_hint"],
        "owner_user_id": row["owner_user_id"], "status": row["status"], "source_created_at": row["source_created_at"],
        "opened_at": row["source_created_at"] or row["created_at"], "window_start": row["window_start"],
        "window_end": row["window_end"], "evidence_sync": row["evidence_sync"], "revision_number": row["revision_number"],
        "evidence_summary": row["evidence_summary"], "investigate": investigate_state(row),
        "version": row["version"], "updated_at": row["updated_at"],
    }
    if detail:
        out["description"] = row["description"]
    return out


async def _project_names(conn: AsyncConnection, ids: set[UUID]) -> dict[UUID, str]:
    if not ids:
        return {}
    return {r["id"]: r["name"] for r in await (await conn.execute("SELECT id, name FROM projects WHERE id = ANY(%s)", (list(ids),))).fetchall()}


async def views(conn: AsyncConnection, rows: list[dict], *, detail: bool = False) -> list[dict]:
    names = await _project_names(conn, {r["project_id"] for r in rows if r["project_id"]})
    return [view(r, names.get(r["project_id"]), detail=detail) for r in rows]


async def require_create_rights(conn: AsyncConnection, org: OrgContext, project_id: UUID | None) -> None:
    if project_id is None:
        if org.org_role not in EDIT_ORG_ROLES:
            raise Forbidden("Your role cannot create tickets")
        return
    (await load_project_context(conn, org, project_id)).require(Cap.INVESTIGATE)


async def upsert_ticket(
    conn: AsyncConnection, org: OrgContext, *, source: str, f: TicketFields, payload: dict | None, project_id: UUID | None,
    request=None,
) -> tuple[dict, str]:
    """Idempotent on (organization, source, external_id): same content -> 'unchanged'; changed -> new revision.
    Creating or updating never enqueues an investigation (R-004)."""
    desc = redact_text(f.description)
    f.description = desc
    clean_payload = redact_json(payload) if payload is not None else None
    h = content_hash(f, clean_payload)
    if project_id is not None:
        await require_create_rights(conn, org, project_id)
    existing = None
    if f.external_id:
        existing = await (await conn.execute(
            "SELECT * FROM tickets WHERE organization_id=%s AND source=%s AND external_id=%s FOR UPDATE",
            (org.organization_id, source, f.external_id))).fetchone()
    snapshot = {"title": f.title, "description": f.description, "severity": f.severity, "environment_key": f.environment_key,
                "service_hint": f.service_hint, "window_start": f.window_start, "window_end": f.window_end}
    if existing is None:
        row = await (await conn.execute(
            "INSERT INTO tickets(organization_id,project_id,source,external_id,title,description,severity,environment_key,service_hint,"
            " source_created_at,window_start,window_end,content_hash,evidence_summary,payload,created_by,owner_user_id,evidence_sync)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s) RETURNING *",
            (org.organization_id, project_id, source, f.external_id, f.title, f.description, f.severity, f.environment_key,
             f.service_hint, f.source_created_at, f.window_start, f.window_end, h, json.dumps(f.evidence_summary, default=str),
             json.dumps(clean_payload, default=str) if clean_payload is not None else None, org.principal.user_id, None,
             "pending" if clean_payload and (f.evidence_summary.get("requests") or f.evidence_summary.get("backend_events")) else "not_required"))).fetchone()
        await conn.execute("INSERT INTO ticket_revisions(ticket_id,revision_number,content_hash,fields) VALUES (%s,1,%s,%s::jsonb)",
                           (row["id"], h, json.dumps(snapshot, default=str)))
        outcome = "created"
    elif existing["content_hash"] == h:
        return existing, "unchanged"
    else:
        rev = existing["revision_number"] + 1
        row = await (await conn.execute(
            "UPDATE tickets SET title=%s, description=%s, severity=COALESCE(%s, severity), environment_key=COALESCE(%s, environment_key),"
            " service_hint=COALESCE(%s, service_hint), window_start=COALESCE(%s, window_start), window_end=COALESCE(%s, window_end),"
            " content_hash=%s, revision_number=%s, evidence_summary=%s::jsonb, payload=COALESCE(%s::jsonb, payload),"
            " evidence_sync=CASE WHEN %s THEN 'pending' ELSE evidence_sync END WHERE id=%s RETURNING *",
            (f.title, f.description, f.severity, f.environment_key, f.service_hint, f.window_start, f.window_end, h, rev,
             json.dumps(f.evidence_summary, default=str), json.dumps(clean_payload, default=str) if clean_payload is not None else None,
             bool(clean_payload), existing["id"]))).fetchone()
        await conn.execute("INSERT INTO ticket_revisions(ticket_id,revision_number,content_hash,fields) VALUES (%s,%s,%s,%s::jsonb)",
                           (row["id"], rev, h, json.dumps(snapshot, default=str)))
        outcome = "updated"
    await audit(conn, organization_id=org.organization_id, project_id=row["project_id"], actor=org.principal.user_id,
                action=f"ticket.{outcome}", target_type="ticket", target_id=row["id"], request=request, details={"source": source})
    return row, outcome


async def get_visible(conn: AsyncConnection, org: OrgContext, ticket_id: UUID, *, lock: bool = False) -> dict:
    """Unassigned tickets are triage items visible to the whole organization; assigned ones follow project grants."""
    row = await (await conn.execute("SELECT * FROM tickets WHERE id=%s AND organization_id=%s" + (" FOR UPDATE" if lock else ""),
                                    (ticket_id, org.organization_id))).fetchone()
    if row is None:
        raise NotFound("Ticket not found")
    if row["project_id"] is not None:
        await load_project_context(conn, org, row["project_id"])  # 404 when the caller has no grant
    return row


async def require_edit_rights(conn: AsyncConnection, org: OrgContext, row: dict) -> None:
    if row["project_id"] is None:
        if org.org_role not in EDIT_ORG_ROLES:
            raise Forbidden("Your role cannot edit tickets")
        return
    (await load_project_context(conn, org, row["project_id"])).require(Cap.INVESTIGATE)


def validate_sort(sort: str) -> str:
    cols = {"opened": "COALESCE(t.source_created_at, t.created_at) DESC, t.id",
            "opened_asc": "COALESCE(t.source_created_at, t.created_at) ASC, t.id",
            "severity": "t.severity ASC NULLS LAST, COALESCE(t.source_created_at, t.created_at) DESC, t.id",
            "title": "lower(t.title), t.id", "status": "t.status, COALESCE(t.source_created_at, t.created_at) DESC, t.id"}
    if sort not in cols:
        raise Unprocessable("Unknown sort")
    return cols[sort]
