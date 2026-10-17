"""Project wizard drafts (R-015): Basics > Hosting (Vercel) > Log scope > Code (GitHub) > Review.
Versioned, resumable, with done/skipped stages. Drafts reference connections by id and never hold secrets."""
from __future__ import annotations

import json
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from psycopg import AsyncConnection
from pydantic import BaseModel, Field

from app.api.v1.connections import ConnectionCreate, clean_config
from app.audit import audit
from app.auth.deps import OrgContext, get_conn, get_container, org_context
from app.container import Container
from app.domain.connections import service as csvc
from app.domain.projects import binding as bind
from app.domain.projects import service as psvc
from app.domain.projects.schemas import ProjectCreate
from app.envelope import wrap
from app.errors import Conflict, Forbidden, NotFound, Unprocessable

router = APIRouter(tags=["project-drafts"])
STAGES = ("basics", "hosting", "log_scope", "code", "review")
StageState = Literal["todo", "done", "skipped"]


class DraftData(BaseModel):
    basics: ProjectCreate | None = None
    log_scope: dict[str, list[bind.ScopeResource]] | None = None
    repositories: list[bind.RepoSelection] | None = None


class DraftCreate(BaseModel):
    data: DraftData = Field(default_factory=DraftData)


class DraftPatch(BaseModel):
    version: int
    data: DraftData | None = None  # top-level keys present here replace those keys; absent keys are kept
    stages: dict[str, StageState] | None = None


def view(d: dict) -> dict:
    return {"id": d["id"], "status": d["status"], "data": d["data"], "stages": d["stages"], "project_id": d["project_id"],
            "created_by": d["created_by"], "version": d["version"], "updated_at": d["updated_at"]}


async def _draft(conn: AsyncConnection, org: OrgContext, draft_id: UUID, *, lock: bool = False) -> dict:
    d = await (await conn.execute("SELECT * FROM project_drafts WHERE id=%s AND organization_id=%s" + (" FOR UPDATE" if lock else ""),
                                  (draft_id, org.organization_id))).fetchone()
    if d is None or (d["created_by"] != org.principal.user_id and not org.is_org_admin):
        raise NotFound("Draft not found")
    return d


@router.post("/project-drafts", status_code=201)
async def create_draft(body: DraftCreate, request: Request, org: OrgContext = Depends(org_context), conn: AsyncConnection = Depends(get_conn)):
    if not org.is_org_admin:
        raise Forbidden("Only organization owners and admins create projects")
    d = await (await conn.execute(
        "INSERT INTO project_drafts(organization_id,created_by,data,stages) VALUES (%s,%s,%s::jsonb,%s::jsonb) RETURNING *",
        (org.organization_id, org.principal.user_id, body.data.model_dump_json(exclude_none=True),
         json.dumps({s: "todo" for s in STAGES})))).fetchone()
    await conn.commit()
    return wrap(request, view(d))


@router.get("/project-drafts")
async def list_drafts(request: Request, org: OrgContext = Depends(org_context), conn: AsyncConnection = Depends(get_conn)):
    rows = await (await conn.execute(
        "SELECT * FROM project_drafts WHERE organization_id=%s AND status='open' AND (created_by=%s OR %s) ORDER BY updated_at DESC LIMIT 50",
        (org.organization_id, org.principal.user_id, org.is_org_admin))).fetchall()
    return wrap(request, [view(r) for r in rows])


@router.get("/project-drafts/{draft_id}")
async def get_draft(draft_id: UUID, request: Request, org: OrgContext = Depends(org_context), conn: AsyncConnection = Depends(get_conn)):
    return wrap(request, view(await _draft(conn, org, draft_id)))


@router.patch("/project-drafts/{draft_id}")
async def patch_draft(draft_id: UUID, body: DraftPatch, request: Request, org: OrgContext = Depends(org_context),
                      conn: AsyncConnection = Depends(get_conn)):
    d = await _draft(conn, org, draft_id, lock=True)
    if d["status"] != "open":
        raise Conflict("Draft is no longer open", code="draft_closed")
    if d["version"] != body.version:
        await conn.rollback()
        raise Conflict("Draft was changed elsewhere. Reload it.", code="version_conflict")
    data = dict(d["data"])
    if body.data is not None:
        data.update(json.loads(body.data.model_dump_json(exclude_none=True)))
    stages = dict(d["stages"])
    for k, v in (body.stages or {}).items():
        if k not in STAGES:
            raise Unprocessable(f"Unknown stage: {k}")
        stages[k] = v
    row = await (await conn.execute("UPDATE project_drafts SET data=%s::jsonb, stages=%s::jsonb WHERE id=%s RETURNING *",
                                    (json.dumps(data), json.dumps(stages), draft_id))).fetchone()
    await conn.commit()
    return wrap(request, view(row))


@router.post("/project-drafts/{draft_id}/discard")
async def discard_draft(draft_id: UUID, request: Request, org: OrgContext = Depends(org_context), conn: AsyncConnection = Depends(get_conn)):
    await _draft(conn, org, draft_id)
    # Draft-owned connections are deleted with the draft (cascade); their credentials go with them.
    await conn.execute("UPDATE project_drafts SET status='discarded' WHERE id=%s AND status='open'", (draft_id,))
    await conn.execute("DELETE FROM connections WHERE draft_id=%s", (draft_id,))
    await conn.commit()
    return wrap(request, {"id": draft_id, "status": "discarded"})


@router.get("/project-drafts/{draft_id}/connections")
async def draft_connections(draft_id: UUID, request: Request, org: OrgContext = Depends(org_context), conn: AsyncConnection = Depends(get_conn)):
    await _draft(conn, org, draft_id)
    rows = await (await conn.execute("SELECT * FROM connections WHERE draft_id=%s ORDER BY kind", (draft_id,))).fetchall()
    return wrap(request, [csvc.public(r) for r in rows])


@router.post("/project-drafts/{draft_id}/connections", status_code=202)
async def create_draft_connection(draft_id: UUID, body: ConnectionCreate, request: Request, org: OrgContext = Depends(org_context),
                                  conn: AsyncConnection = Depends(get_conn), c: Container = Depends(get_container)):
    d = await _draft(conn, org, draft_id)
    if d["status"] != "open":
        raise Conflict("Draft is no longer open", code="draft_closed")
    row, ver = await csvc.create_connection(
        conn, c.vault, organization_id=org.organization_id, actor=org.principal.user_id, project_id=None, draft_id=draft_id,
        provider=body.provider, auth_method=body.auth_method, config=clean_config(body.provider, body.config), secret=body.secret,
        secret_expires_at=body.secret_expires_at, request=request)
    job = await csvc.enqueue_verify(conn, row, org.principal.user_id, ver)
    await conn.commit()
    return wrap(request, csvc.accepted(job, row["id"]))


def _review(d: dict, conns: list[dict]) -> list[dict[str, Any]]:
    """What the review step shows: the exact selections and honest per-stage state (verified/skipped/pending)."""
    by_kind = {c["kind"]: c for c in conns}
    out = []
    for key, kind in (("hosting", "hosting"), ("code", "code")):
        c = by_kind.get(kind)
        state = d["stages"].get(key, "todo")
        ok = c is not None and c["status"] in ("connected", "connected_with_warnings")
        out.append({"stage": key, "stage_state": state, "connection": csvc.public(c) if c else None,
                    "ready": ok, "note": None if ok else ("Skipped: finish later in the project." if state == "skipped" else "Not verified yet.")})
    return out


@router.get("/project-drafts/{draft_id}/review")
async def review(draft_id: UUID, request: Request, org: OrgContext = Depends(org_context), conn: AsyncConnection = Depends(get_conn)):
    d = await _draft(conn, org, draft_id)
    conns = await (await conn.execute("SELECT * FROM connections WHERE draft_id=%s", (draft_id,))).fetchall()
    return wrap(request, {"draft": view(d), "stages": _review(d, conns)})


@router.post("/project-drafts/{draft_id}/activate", status_code=201)
async def activate(draft_id: UUID, request: Request, org: OrgContext = Depends(org_context),
                   conn: AsyncConnection = Depends(get_conn), c: Container = Depends(get_container)):
    """Creates the project, adopts the draft's connections, applies log scope and repository selections (re-validated
    against provider discovery). Skipped/unverified stages stay visibly unfinished; nothing is faked as ready.
    `index_job_ids` is empty until code indexing (T-007) exists - repositories are bound, not yet indexed."""
    if not org.is_org_admin:
        raise Forbidden("Only organization owners and admins create projects")
    d = await _draft(conn, org, draft_id, lock=True)
    if d["status"] != "open":
        raise Conflict("Draft is no longer open", code="draft_closed")
    if not d["data"].get("basics"):
        raise Unprocessable("Complete the Basics step first", code="basics_required")
    body = ProjectCreate.model_validate(d["data"]["basics"])
    p = await psvc.create_project(conn, organization_id=org.organization_id, actor=d["created_by"], body=body, request=request)
    conns = await (await conn.execute("SELECT * FROM connections WHERE draft_id=%s FOR UPDATE", (draft_id,))).fetchall()
    await conn.execute("UPDATE connections SET project_id=%s, draft_id=NULL WHERE draft_id=%s", (p["id"], draft_id))
    await conn.execute("UPDATE jobs SET project_id=%s WHERE resource_type='connection' AND resource_id = ANY(%s)", (p["id"], [x["id"] for x in conns]))
    by_kind = {x["kind"]: x for x in conns}
    scope_n, bound = 0, []
    if d["data"].get("log_scope"):
        scope = {k: [bind.ScopeResource.model_validate(r) for r in v] for k, v in d["data"]["log_scope"].items()}
        scope_n = await bind.apply_log_scope(c, conn, organization_id=org.organization_id, project_id=p["id"], draft_id=None,
                                             env_map=scope, connection_row=by_kind.get("hosting"))
    if d["data"].get("repositories"):
        bound = await bind.apply_repositories(c, conn, organization_id=org.organization_id, project_id=p["id"], draft_id=None,
                                              repos=[bind.RepoSelection.model_validate(r) for r in d["data"]["repositories"]],
                                              connection_row=by_kind.get("code"))
    await conn.execute("UPDATE project_drafts SET status='activated', project_id=%s WHERE id=%s", (p["id"], draft_id))
    await audit(conn, organization_id=org.organization_id, project_id=p["id"], actor=org.principal.user_id, action="project.activate_draft",
                target_type="project_draft", target_id=draft_id, request=request,
                details={"log_scope_entries": scope_n, "repositories": len(bound), "connections": len(conns)})
    await conn.commit()
    summary = (await psvc.project_summaries(conn, [p]))[0]
    return wrap(request, {"project": summary, "index_job_ids": [], "repositories_bound": len(bound), "log_scope_entries": scope_n})
