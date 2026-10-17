"""Investigations (R-005/R-006): an explicit Investigate action on a ticket starts one GraphRAG run over the project's
evidence graph. Runs, steps, hypotheses, findings and answers live in the graph and are read back from it.

The run executes on a background thread of the API process (single-node demo deployment); one active run per
ticket is enforced in-process and in the graph.
"""
from __future__ import annotations

import asyncio
import threading
import uuid
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from psycopg import AsyncConnection
from pydantic import BaseModel, Field

from app.api.v1.deps_graph import neo4j_driver
from app.audit import audit
from app.auth.authz import Cap
from app.auth.deps import OrgContext, ProjectContext, get_conn, load_project_context, org_context, project_access
from app.domain.tickets import service as tsvc
from app.envelope import wrap
from app.errors import Conflict, DependencyUnavailable, NotFound, Unprocessable
from app.investigate import read as R
from app.investigate.runner import TicketIn, investigate

router = APIRouter(tags=["investigations"])

_active: set[str] = set()
_lock = threading.Lock()


class AskIn(BaseModel):
    question: str = Field(min_length=2, max_length=500)


def _start(driver, db: str, settings, pid: str, ticket: TicketIn, control_run_id: str, actor: str) -> None:
    def work():
        try:
            investigate(driver, db, project_id=pid, ticket=ticket, control_run_id=control_run_id, actor_id=actor,
                        api_key=settings.openai_api_key, model=settings.trace_model)
        finally:
            with _lock:
                _active.discard(ticket.id)

    threading.Thread(target=work, name=f"investigate-{control_run_id[:8]}", daemon=True).start()


@router.post("/tickets/{ticket_id}/investigate", status_code=202)
async def start_investigation(ticket_id: UUID, request: Request, org: OrgContext = Depends(org_context), conn: AsyncConnection = Depends(get_conn),
                              driver=Depends(neo4j_driver)):
    """Explicit Investigate / Rerun. Assigning or opening a ticket never calls this."""
    row = await tsvc.get_visible(conn, org, ticket_id)
    if row["project_id"] is None:
        raise Unprocessable("Assign this ticket to a project first.", code="project_required")
    if row["status"] != "open":
        raise Unprocessable("Only open tickets can be investigated.", code="ticket_not_open")
    ctx = await load_project_context(conn, org, row["project_id"])
    ctx.require(Cap.INVESTIGATE)
    pid, tid = str(row["project_id"]), str(ticket_id)
    existing = await asyncio.to_thread(R.active_run, driver[0], driver[1], pid, tid)
    with _lock:
        if existing or tid in _active:
            raise Conflict("An investigation is already running for this ticket.", code="run_active")
        _active.add(tid)
    control_run_id = str(uuid.uuid4())
    t = TicketIn(id=tid, title=row["title"], description=row["description"] or "", severity=row["severity"], environment_key=row["environment_key"],
                 service_hint=row["service_hint"], window_start=row["window_start"], window_end=row["window_end"],
                 opened_at=row["source_created_at"] or row["created_at"], revision_number=row["revision_number"])
    await audit(conn, organization_id=org.organization_id, project_id=row["project_id"], actor=org.principal.user_id, action="investigation.start",
                target_type="ticket", target_id=tid, request=request, details={"run_id": control_run_id})
    await conn.commit()
    _start(driver[0], driver[1], request.app.state.container.settings, pid, t, control_run_id, str(org.principal.user_id))
    return JSONResponse(jsonable_encoder(wrap(request, {"run_id": control_run_id, "project_id": pid, "ticket_id": tid, "status": "running"})), status_code=202)


@router.get("/investigations")
async def list_investigations(request: Request, project_id: UUID | None = None, ticket_id: UUID | None = None, org: OrgContext = Depends(org_context),
                              conn: AsyncConnection = Depends(get_conn), driver=Depends(neo4j_driver)):
    rows = await (await conn.execute(
        "SELECT p.id, p.name FROM projects p WHERE p.organization_id = %s AND p.status = 'active' "
        "AND EXISTS (SELECT 1 FROM project_memberships m WHERE m.project_id = p.id AND m.user_id = %s)",
        (org.organization_id, org.principal.user_id))).fetchall()
    names = {str(r["id"]): r["name"] for r in rows}
    pids = [str(project_id)] if project_id else list(names)
    pids = [p for p in pids if p in names]
    runs = await asyncio.to_thread(R.list_runs, driver[0], driver[1], pids, ticket_id=str(ticket_id) if ticket_id else None)
    tickets = {}
    ids = list({r["ticket_id"] for r in runs})
    if ids:
        for t in await (await conn.execute("SELECT id, number, severity, environment_key, status FROM tickets WHERE id = ANY(%s::uuid[])", (ids,))).fetchall():
            tickets[str(t["id"])] = t
    out = []
    for r in runs:
        t = tickets.get(r["ticket_id"])
        out.append({**r, "project_name": names.get(r["project_id"]), "ticket_display_id": f"TKT-{t['number']}" if t else None,
                    "severity": t and t["severity"], "environment_key": t and t["environment_key"], "ticket_status": t and t["status"]})
    return wrap(request, out)


@router.get("/projects/{project_id}/investigations/{run_id}")
async def get_investigation(run_id: UUID, request: Request, ctx: ProjectContext = Depends(project_access(Cap.READ)), conn: AsyncConnection = Depends(get_conn),
                            driver=Depends(neo4j_driver)):
    d = await asyncio.to_thread(R.run_detail, driver[0], driver[1], str(ctx.project_id), str(run_id))
    if d is None:
        raise NotFound("Investigation not found")
    t = await (await conn.execute("SELECT number, severity, environment_key, status, description, service_hint FROM tickets WHERE id = %s", (d["ticket"]["id"],))).fetchone()
    p = await (await conn.execute("SELECT name FROM projects WHERE id = %s", (ctx.project_id,))).fetchone()
    if t:
        d["ticket"].update(display_id=f"TKT-{t['number']}", severity=t["severity"], environment_key=t["environment_key"], status=t["status"],
                           description=t["description"], service_hint=t["service_hint"])
    d["project"] = {"id": str(ctx.project_id), "name": p and p["name"]}
    return wrap(request, d)


@router.post("/projects/{project_id}/investigations/{run_id}/ask")
async def ask_investigation(run_id: UUID, body: AskIn, request: Request, ctx: ProjectContext = Depends(project_access(Cap.READ)), driver=Depends(neo4j_driver)):
    ctx.require(Cap.CHAT)
    from app.graph import identity as I
    from app.investigate.ask import ask

    s = request.app.state.container.settings
    if not s.openai_api_key:
        raise DependencyUnavailable("The language model is not configured")
    rid = I.entity_id("InvestigationRun", str(ctx.project_id), str(run_id))
    try:
        out = await asyncio.to_thread(ask, driver[0], driver[1], project_id=str(ctx.project_id), run_id=rid, question=body.question, api_key=s.openai_api_key, model=s.trace_model)
    except LookupError as e:
        raise NotFound(str(e)) from e
    return wrap(request, out)


@router.get("/projects/{project_id}/evidence")
async def get_evidence(request: Request, ids: list[str] = Query(..., max_length=50), ctx: ProjectContext = Depends(project_access(Cap.READ)), driver=Depends(neo4j_driver)):
    """Evidence viewer: log events and source chunks by id, scoped to the project."""
    ids = [i for i in ids if i.startswith(("evd_", "chk_"))]
    return wrap(request, await asyncio.to_thread(R.evidence, driver[0], driver[1], str(ctx.project_id), ids))


@router.post("/projects/{project_id}/graph/sync")
async def sync_evidence(request: Request, retry_failed: bool = Query(False), ctx: ProjectContext = Depends(project_access(Cap.INDEX)), conn: AsyncConnection = Depends(get_conn), driver=Depends(neo4j_driver)):
    """Load the project's deployments, PulseLab ticket evidence and ingested log evidence from the control plane into the evidence graph
    (idempotent). Ingested logs are normally applied by the worker as they arrive; this applies whatever is due now (bounded) and reports what
    the graph has acknowledged. `retry_failed=true` re-queues rows the graph refused earlier."""
    from dataclasses import asdict

    from app.domain.ingest import graph_apply as ga
    from app.graph import sync as S
    from app.graph.repository import GraphRepository

    bundle = await S.collect(conn, ctx.project_id)
    repo = GraphRepository(driver[0], driver[1])
    report = await asyncio.to_thread(S.project, repo, bundle)
    if retry_failed:
        await ga.retry_failed(conn, ctx.project_id)
    evidence = await ga.apply_pending(request.app.state.container, ctx.org.organization_id, ctx.project_id, f"api:{uuid.uuid4().hex[:8]}", max_batches=5)
    state = await ga.graph_state(conn, ctx.project_id)
    await audit(conn, organization_id=ctx.org.organization_id, project_id=ctx.project_id, actor=ctx.org.principal.user_id, action="graph.sync",
                target_type="project", target_id=ctx.project_id, request=request, details={"events": report.log_events, "deployments": report.deployments, "ingested_applied": evidence["applied"]})
    await conn.commit()
    return wrap(request, {**asdict(report), "evidence": {**evidence, "state": state}})
