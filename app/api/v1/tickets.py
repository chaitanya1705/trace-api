"""Tickets (R-004): intake (manual form, API, PulseLab export), views, assignment, resolve/close.
Assigning, refreshing or opening a ticket NEVER starts an investigation; that is a separate explicit action."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from psycopg import AsyncConnection
from pydantic import BaseModel, Field, ValidationError

from app.audit import audit
from app.auth.authz import Cap
from app.auth.deps import OrgContext, get_conn, get_container, load_project_context, org_context
from app.container import Container
from app.domain.tickets import service as svc
from app.domain.tickets.pulselab import TicketFields, parse_export
from app.envelope import wrap
from app.errors import Conflict, TraceError, Unprocessable
from app.jobs.triggers import request_graph_sync
from app.pagination import clamp_limit, decode_cursor, encode_cursor
from app.redact import redact_text

router = APIRouter(tags=["tickets"])
Severity = Literal["SEV-1", "SEV-2", "SEV-3", "SEV-4"]
IMPORT_MAX_BYTES = 5 * 1024 * 1024


class PayloadTooLarge(TraceError):
    status, code = 413, "payload_too_large"


class TicketCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field("", max_length=20000)
    severity: Severity | None = None
    environment_key: str | None = Field(None, max_length=40)
    service_hint: str | None = Field(None, max_length=200)
    project_id: UUID | None = None
    external_id: str | None = Field(None, max_length=200)   # lets an API client dedupe/re-send idempotently
    source_created_at: datetime | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None


class TicketPatch(BaseModel):
    version: int
    project_id: UUID | None = None          # set to assign; send null explicitly to unassign
    environment_key: str | None = None
    owner_user_id: UUID | None = None
    status: Literal["open", "resolved", "closed"] | None = None
    severity: Severity | None = None
    title: str | None = Field(None, min_length=1, max_length=300)
    description: str | None = Field(None, max_length=20000)
    window_start: datetime | None = None
    window_end: datetime | None = None


class PulseLabReview(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(..., max_length=20000)
    severity: Severity | None = Field(...)
    environment_key: str | None = Field(..., max_length=40)
    service_hint: str | None = Field(..., max_length=200)
    project_id: UUID | None = Field(...)


class PulseLabConfirmation(BaseModel):
    export: dict[str, Any]
    fields: PulseLabReview


def _vis(org: OrgContext) -> tuple[str, list]:
    return ("t.organization_id = %s AND (t.project_id IS NULL OR EXISTS (SELECT 1 FROM project_memberships m"
            " WHERE m.project_id = t.project_id AND m.user_id = %s))", [org.organization_id, org.principal.user_id])


@router.get("/tickets")
async def list_tickets(
    request: Request,
    view: Literal["need_project", "open", "resolved", "all"] = "open",
    q: str | None = Query(None, max_length=100),
    severity: Severity | None = None,
    environment: str | None = Query(None, max_length=40),
    project_id: UUID | None = None,
    sort: str = "opened",
    limit: int | None = Query(None, ge=1, le=100),
    cursor: str | None = None,
    org: OrgContext = Depends(org_context),
    conn: AsyncConnection = Depends(get_conn),
):
    """Server-side search/filter/sort with bounded pages. meta.coverage.counts has the per-view totals (within the
    current scope filters) so the UI can show 'Need a project 2' without a second request."""
    n = clamp_limit(limit)
    order = svc.validate_sort(sort)
    off = int((decode_cursor(cursor) or {}).get("o", 0))
    base, params = _vis(org)
    if project_id:
        await load_project_context(conn, org, project_id)  # 404 if not authorized
        base += " AND t.project_id = %s"
        params.append(project_id)
    if severity:
        base += " AND t.severity = %s"
        params.append(severity)
    if environment:
        base += " AND t.environment_key = %s"
        params.append(environment.strip().lower())
    if q:
        like = "%" + q.strip().lower().replace("%", r"\%").replace("_", r"\_") + "%"
        base += (" AND (lower(t.title) LIKE %s OR lower(coalesce(t.service_hint,'')) LIKE %s OR lower(coalesce(t.external_id,'')) LIKE %s"
                 " OR ('tkt-' || t.number::text) LIKE %s)")
        params += [like, like, like, like]
    counts = await (await conn.execute(
        f"SELECT count(*) FILTER (WHERE t.project_id IS NULL AND t.status='open') AS need_project,"
        f" count(*) FILTER (WHERE t.status='open') AS open, count(*) FILTER (WHERE t.status IN ('resolved','closed')) AS resolved,"
        f" count(*) AS all FROM tickets t WHERE {base}", params)).fetchone()
    cond = {"need_project": " AND t.project_id IS NULL AND t.status='open'", "open": " AND t.status='open'",
            "resolved": " AND t.status IN ('resolved','closed')", "all": ""}[view]
    rows = await (await conn.execute(
        f"SELECT t.* FROM tickets t WHERE {base}{cond} ORDER BY {order} LIMIT %s OFFSET %s", (*params, n + 1, off))).fetchall()
    more = len(rows) > n
    return wrap(request, await svc.views(conn, rows[:n]), next_cursor=encode_cursor({"o": off + n}) if more else None,
                coverage={"counts": dict(counts), "view": view, "empty_dataset": counts["all"] == 0 and not (q or severity or environment or project_id)})


@router.post("/tickets", status_code=201)
async def create_ticket(body: TicketCreate, request: Request, org: OrgContext = Depends(org_context), conn: AsyncConnection = Depends(get_conn)):
    """Manual form and authenticated API. Re-sending the same external_id is idempotent (unchanged / new revision)."""
    f = TicketFields(external_id=body.external_id, title=body.title.strip(), description=body.description, severity=body.severity,
                     environment_key=(body.environment_key or "").strip().lower() or None, service_hint=body.service_hint,
                     source_created_at=body.source_created_at, window_start=body.window_start, window_end=body.window_end)
    await svc.require_create_rights(conn, org, body.project_id)
    row, outcome = await svc.upsert_ticket(conn, org, source="api" if body.external_id else "manual", f=f, payload=None,
                                           project_id=body.project_id, request=request)
    await request_graph_sync(conn, organization_id=org.organization_id, project_id=row["project_id"])
    await conn.commit()
    return await _ticket_response(request, conn, row, outcome)


async def _ticket_response(request: Request, conn: AsyncConnection, row: dict, outcome: str) -> JSONResponse:
    data = {"ticket": (await svc.views(conn, [row], detail=True))[0], "outcome": outcome}
    return JSONResponse(jsonable_encoder(wrap(request, data)), status_code=201 if outcome == "created" else 200)


@router.post("/tickets/import/pulselab")
async def import_pulselab(
    request: Request, project_id: UUID | None = None, org: OrgContext = Depends(org_context),
    conn: AsyncConnection = Depends(get_conn), c: Container = Depends(get_container),
):
    """Accepts a PulseLab incident export (JSON body). The ticket arrives unassigned unless project_id is given.
    Backend events/requests stay in the redacted payload until evidence ingestion (T-009) writes them to the graph;
    meanwhile evidence_sync='pending' says so honestly."""
    raw = await request.body()
    if len(raw) > IMPORT_MAX_BYTES:
        raise PayloadTooLarge("Export is larger than 5 MB")
    try:
        doc = json.loads(raw)
    except ValueError:
        raise Unprocessable("Body is not valid JSON", code="invalid_export") from None
    f = parse_export(doc)
    await svc.require_create_rights(conn, org, project_id)
    row, outcome = await svc.upsert_ticket(conn, org, source="pulselab", f=f, payload=doc, project_id=project_id, request=request)
    await request_graph_sync(conn, organization_id=org.organization_id, project_id=row["project_id"])
    await conn.commit()
    return await _ticket_response(request, conn, row, outcome)


@router.post("/tickets/import/pulselab/preview")
async def preview_pulselab(request: Request):
    """Parse a reporter-supplied incident export for review without writing a ticket or graph job."""
    raw = await request.body()
    if len(raw) > IMPORT_MAX_BYTES:
        raise PayloadTooLarge("Export is larger than 5 MB")
    try:
        doc = json.loads(raw)
    except ValueError:
        raise Unprocessable("Body is not valid JSON", code="invalid_export") from None
    f = parse_export(doc)
    return wrap(request, {
        "fields": {
            "title": f.title,
            "description": redact_text(f.description),
            "severity": f.severity,
            "environment_key": f.environment_key,
            "service_hint": f.service_hint,
            "project_id": None,
        },
        "provenance": "Reporter-supplied PulseLab incident export",
    })


@router.post("/tickets/import/pulselab/confirm")
async def confirm_pulselab(
    request: Request, org: OrgContext = Depends(org_context), conn: AsyncConnection = Depends(get_conn),
):
    """Store an incident export only after the caller submits the fields reviewed in New Ticket."""
    raw = await request.body()
    if len(raw) > IMPORT_MAX_BYTES:
        raise PayloadTooLarge("Export is larger than 5 MB")
    try:
        data = json.loads(raw)
        confirmed = PulseLabConfirmation.model_validate(data)
    except (ValueError, ValidationError):
        raise Unprocessable("Invalid PulseLab confirmation", code="invalid_export") from None
    f = parse_export(confirmed.export)
    reviewed = confirmed.fields
    f.title = reviewed.title.strip()
    f.description = reviewed.description
    f.severity = reviewed.severity
    f.environment_key = (reviewed.environment_key or "").strip().lower() or None
    f.service_hint = (reviewed.service_hint or "").strip() or None
    existing = None
    if f.external_id:
        match = await (await conn.execute(
            "SELECT id FROM tickets WHERE organization_id=%s AND source=%s AND external_id=%s",
            (org.organization_id, "pulselab", f.external_id),
        )).fetchone()
        if match is not None:
            existing = await svc.get_visible(conn, org, match["id"], lock=True)
    prior_project_id = existing["project_id"] if existing is not None else None
    if existing is None:
        row, outcome = await svc.upsert_ticket(conn, org, source="pulselab", f=f, payload=confirmed.export,
                                               project_id=reviewed.project_id, request=request)
    else:
        row, outcome = await svc.update_reviewed_ticket(
            conn, org, existing, title=f.title, description=f.description, severity=f.severity,
            environment_key=f.environment_key, service_hint=f.service_hint, project_id=reviewed.project_id,
            request=request,
        )
    await request_graph_sync(conn, organization_id=org.organization_id, project_id=row["project_id"])
    if prior_project_id is not None and prior_project_id != row["project_id"]:
        await request_graph_sync(conn, organization_id=org.organization_id, project_id=prior_project_id)
    await conn.commit()
    return await _ticket_response(request, conn, row, outcome)


@router.get("/tickets/{ticket_id}")
async def get_ticket(ticket_id: UUID, request: Request, org: OrgContext = Depends(org_context), conn: AsyncConnection = Depends(get_conn)):
    row = await svc.get_visible(conn, org, ticket_id)
    d = (await svc.views(conn, [row], detail=True))[0]
    d["revisions"] = [dict(r) for r in await (await conn.execute(
        "SELECT revision_number, created_at, content_hash FROM ticket_revisions WHERE ticket_id=%s ORDER BY revision_number DESC",
        (ticket_id,))).fetchall()]
    d["has_payload"] = row["payload"] is not None
    return wrap(request, d)


@router.get("/tickets/{ticket_id}/payload")
async def get_ticket_payload(ticket_id: UUID, request: Request, org: OrgContext = Depends(org_context), conn: AsyncConnection = Depends(get_conn)):
    """The redacted original export (requests, backend events, gaps) for the ticket detail 'raw evidence' view."""
    row = await svc.get_visible(conn, org, ticket_id)
    return wrap(request, row["payload"])


@router.patch("/tickets/{ticket_id}")
async def patch_ticket(ticket_id: UUID, body: TicketPatch, request: Request, org: OrgContext = Depends(org_context),
                       conn: AsyncConnection = Depends(get_conn)):
    row = await svc.get_visible(conn, org, ticket_id, lock=True)
    if row["version"] != body.version:
        await conn.rollback()
        raise Conflict("Ticket was changed by someone else. Reload and retry.", code="version_conflict")
    await svc.require_edit_rights(conn, org, row)
    given = body.model_fields_set - {"version"}
    if not given:
        raise Unprocessable("Nothing to change")
    sets: list[str] = []
    params: list[Any] = []
    assigned = False
    if "project_id" in given:
        if body.project_id is not None:
            (await load_project_context(conn, org, body.project_id)).require(Cap.INVESTIGATE)
            if row["owner_user_id"] is None and "owner_user_id" not in given:
                owner = (await (await conn.execute("SELECT owner_user_id FROM projects WHERE id=%s", (body.project_id,))).fetchone())["owner_user_id"]
                sets.append("owner_user_id=%s")
                params.append(owner)
        sets += ["project_id=%s", "assignment_version=assignment_version+1"]
        params.append(body.project_id)
        assigned = True
    if "owner_user_id" in given:
        if body.owner_user_id is not None:
            ok = await (await conn.execute("SELECT 1 FROM memberships WHERE organization_id=%s AND user_id=%s AND status='active'",
                                           (org.organization_id, body.owner_user_id))).fetchone()
            if ok is None:
                raise Unprocessable("Owner must be an active member of the organization")
        sets.append("owner_user_id=%s")
        params.append(body.owner_user_id)
    for col in ("status", "severity", "title", "description", "window_start", "window_end"):
        if col in given:
            if col in ("title", "status") and getattr(body, col) is None:
                raise Unprocessable(f"{col} cannot be null")
            sets.append(f"{col}=%s")
            params.append(getattr(body, col))
    if "environment_key" in given:
        sets.append("environment_key=%s")
        params.append((body.environment_key or "").strip().lower() or None)
    out = await (await conn.execute(f"UPDATE tickets SET {', '.join(sets)} WHERE id=%s RETURNING *", (*params, ticket_id))).fetchone()
    # Assignment/refresh/open never start a run (R-004, D-01). Only the ticket itself is projected into the evidence graph.
    await request_graph_sync(conn, organization_id=org.organization_id, project_id=out["project_id"])
    await audit(conn, organization_id=org.organization_id, project_id=out["project_id"], actor=org.principal.user_id,
                action="ticket.assign" if assigned else "ticket.update", target_type="ticket", target_id=ticket_id, request=request,
                details={"fields": sorted(given)})
    await conn.commit()
    return wrap(request, (await svc.views(conn, [out], detail=True))[0])

