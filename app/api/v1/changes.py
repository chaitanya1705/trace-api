"""Changes and patch proposals (R-011, R-012). A change is a fix an investigation proposes. Each version is an immutable diff with a
hash and the exact base commit; approvals bind to a hash, so a new version starts without approvals. TRACE never applies a patch
and never writes to a repository from here: opening a draft PR needs a separately authorized write connection, which this build
does not have, so that step reports why it is unavailable instead of pretending."""
from __future__ import annotations

import asyncio
import json
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from psycopg import AsyncConnection
from pydantic import BaseModel, Field

from app.api.v1.deps_graph import neo4j_driver
from app.audit import audit
from app.auth.authz import Cap
from app.auth.deps import OrgContext, ProjectContext, get_conn, load_project_context, org_context, project_access
from app.domain.changes import propose as P
from app.envelope import wrap
from app.errors import Conflict, DependencyUnavailable, Forbidden, NotFound, TraceError, Unprocessable
from app.graph import identity as I
from app.graph.evidence import revision_manifest
from app.graph.repository import GraphRepository, prepare_node, prepare_rel
from app.investigate import read as R

router = APIRouter(tags=["changes"])


class ProposalFailed(TraceError):
    status, code = 422, "proposal_failed"


class ReviseIn(BaseModel):
    instruction: str = Field(min_length=3, max_length=500)


class ApprovalIn(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    patch_hash: str = Field(min_length=64, max_length=64)
    reason: str | None = Field(None, max_length=500)


class PolicyIn(BaseModel):
    required_approvals: int = Field(1, ge=1, le=5)
    author_can_approve: bool = False


DEFAULT_POLICY = {"required_approvals": 1, "author_can_approve": False}


async def _policy(conn: AsyncConnection, project_id: UUID) -> dict:
    row = await (await conn.execute("SELECT required_approvals, author_can_approve FROM change_policies WHERE project_id=%s", (project_id,))).fetchone()
    return dict(row) if row else dict(DEFAULT_POLICY)


def _persist_patch(driver, db: str, pid: str, control_run_id: str, prop: P.Proposal, version: int, patch_row_id: str) -> str:
    """The immutable PatchVersion in the graph, attached to the run's ResolutionPlan (created if the run had no actions)."""
    repo = GraphRepository(driver, db)
    run_id = I.entity_id("InvestigationRun", pid, control_run_id)
    with driver.session(database=db) as s:
        row = s.run("MATCH (pl:Entity:ResolutionPlan {project_id: $p, run_id: $r}) RETURN pl.id AS id ORDER BY pl.version DESC LIMIT 1", p=pid, r=run_id).single()
        fnd = s.run("MATCH (:Entity:InvestigationRun {id: $r})-[:PRODUCES]->(f:Entity:Finding) RETURN f.id AS id LIMIT 1", r=run_id).single()
    plan_id = row["id"] if row else None
    if not plan_id:
        if not fnd:
            raise ProposalFailed("The investigation has no finding to attach a change to")
        plan_id = I.entity_id("ResolutionPlan", pid, fnd["id"], 1)
        repo.write_nodes([prepare_node("ResolutionPlan", plan_id, pid, {"run_id": run_id, "finding_id": fnd["id"], "version": 1, "actions_json": "[]", "preconditions_json": "[]", "criteria_json": "[]"})])
        repo.write_relationships([prepare_rel("ADDRESSES", "ResolutionPlan", plan_id, "Finding", fnd["id"], pid, I.edge_key("ADDRESSES", plan_id, fnd["id"]), {"version": 1})])
    pv = I.entity_id("PatchVersion", pid, plan_id, prop.content_hash)
    repo.write_nodes([prepare_node("PatchVersion", pv, pid, {"plan_id": plan_id, "base_manifest_id": revision_manifest(repo, pid, prop.base_revision_id) or prop.base_revision_id,
                                                                "content_hash": prop.content_hash, "artifact_ref": f"postgres:patch_versions/{patch_row_id}", "test_status": "not_run"})])
    repo.write_relationships([prepare_rel("HAS_PATCH", "ResolutionPlan", plan_id, "PatchVersion", pv, pid, I.edge_key("HAS_PATCH", plan_id, pv), {"version": version})])
    return pv


async def _insert_version(conn: AsyncConnection, change_id: UUID, version: int, prop: P.Proposal, user: UUID, instruction: str | None) -> dict:
    return await (await conn.execute(
        "INSERT INTO patch_versions(change_id, version, base_commit, base_revision_id, files, diff, content_hash, summary, risk_reason, tests, rollback, checks, instruction, created_by) "
        "VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s) RETURNING *",
        (change_id, version, prop.base_commit, prop.base_revision_id, json.dumps(prop.files), prop.diff, prop.content_hash, prop.rationale, prop.risk_reason,
         json.dumps(prop.tests), prop.rollback, json.dumps(prop.checks), instruction, user))).fetchone()


@router.post("/projects/{project_id}/investigations/{run_id}/changes", status_code=201)
async def propose_change(run_id: UUID, request: Request, ctx: ProjectContext = Depends(project_access(Cap.PROPOSE)), conn: AsyncConnection = Depends(get_conn),
                         driver=Depends(neo4j_driver)):
    """Explicit action on a finished investigation. Needs an identified root cause and the indexed source of the files it points at."""
    s = request.app.state.container.settings
    pid = str(ctx.project_id)
    try:
        prop = await asyncio.to_thread(P.propose, driver[0], driver[1], pid, str(run_id), api_key=s.openai_api_key, model=s.trace_model)
    except P.ProposalError as e:
        raise (DependencyUnavailable if e.code == "model_unavailable" else ProposalFailed)(e.message, code=e.code) from None
    detail = await asyncio.to_thread(R.run_detail, driver[0], driver[1], pid, str(run_id))
    ticket = detail["ticket"]["id"] if detail else None
    t = await (await conn.execute("SELECT environment_key FROM tickets WHERE id=%s", (ticket,))).fetchone() if ticket else None
    ch = await (await conn.execute(
        "INSERT INTO change_proposals(organization_id, project_id, ticket_id, run_id, title, rationale, risk, environment_key, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        (ctx.org.organization_id, ctx.project_id, ticket, run_id, prop.title, prop.rationale, prop.risk, t and t["environment_key"], ctx.org.principal.user_id))).fetchone()
    pv = await _insert_version(conn, ch["id"], 1, prop, ctx.org.principal.user_id, None)
    gid = await asyncio.to_thread(_persist_patch, driver[0], driver[1], pid, str(run_id), prop, 1, str(pv["id"]))
    await conn.execute("UPDATE patch_versions SET graph_patch_id=%s WHERE id=%s", (gid, pv["id"]))
    await audit(conn, organization_id=ctx.org.organization_id, project_id=ctx.project_id, actor=ctx.org.principal.user_id, action="change.propose", target_type="change",
                target_id=ch["id"], request=request, details={"hash": prop.content_hash, "files": [f["path"] for f in prop.files]})
    await conn.commit()
    return wrap(request, {"id": str(ch["id"]), "display_id": f"CHG-{ch['number']}"})


def _version_view(v: dict, with_diff: bool = True) -> dict:
    out = {"version": v["version"], "content_hash": v["content_hash"], "base_commit": v["base_commit"], "files": v["files"], "summary": v["summary"], "risk_reason": v["risk_reason"],
           "tests": v["tests"], "rollback": v["rollback"], "checks": v["checks"], "test_status": v["test_status"], "instruction": v["instruction"], "created_at": v["created_at"]}
    return out


async def _load(conn: AsyncConnection, org: OrgContext, change_id: UUID, *, lock: bool = False) -> tuple[dict, ProjectContext]:
    row = await (await conn.execute("SELECT * FROM change_proposals WHERE id=%s AND organization_id=%s" + (" FOR UPDATE" if lock else ""), (change_id, org.organization_id))).fetchone()
    if row is None:
        raise NotFound("Change not found")
    return dict(row), await load_project_context(conn, org, row["project_id"])


def _eligibility(ctx: ProjectContext, user: UUID, ch: dict, policy: dict, cur_hash: str, approvals: list[dict]) -> dict:
    if ch["status"] not in ("proposed", "approved"):
        return {"can_approve": False, "reason": f"This change is {ch['status'].replace('_', ' ')}."}
    if Cap.APPROVE not in ctx.caps:
        return {"can_approve": False, "reason": "You do not have the approver role on this project. Ask a project admin to add you in Team."}
    if ch["created_by"] == user and not policy["author_can_approve"]:
        return {"can_approve": False, "reason": "You proposed this change, and this project requires an independent approver."}
    if any(a["actor_user_id"] == user and a["patch_hash"] == cur_hash and a["revoked_at"] is None for a in approvals):
        return {"can_approve": False, "reason": "You have already decided on this version."}
    return {"can_approve": True, "reason": None}


async def _detail(conn: AsyncConnection, org: OrgContext, ch: dict, ctx: ProjectContext, driver=None) -> dict:
    versions = [dict(r) for r in await (await conn.execute("SELECT * FROM patch_versions WHERE change_id=%s ORDER BY version", (ch["id"],))).fetchall()]
    cur = next(v for v in versions if v["version"] == ch["current_version"])
    approvals = [dict(r) for r in await (await conn.execute(
        "SELECT a.*, m.email, m.display_name FROM change_approvals a LEFT JOIN memberships m ON m.user_id=a.actor_user_id AND m.organization_id=%s WHERE a.change_id=%s ORDER BY a.created_at",
        (org.organization_id, ch["id"]))).fetchall()]
    policy = await _policy(conn, ch["project_id"])
    pn = await (await conn.execute("SELECT name FROM projects WHERE id=%s", (ch["project_id"],))).fetchone()
    tk = await (await conn.execute("SELECT number FROM tickets WHERE id=%s", (ch["ticket_id"],))).fetchone() if ch["ticket_id"] else None
    live = [a for a in approvals if a["patch_hash"] == cur["content_hash"] and a["revoked_at"] is None]
    return {
        "id": str(ch["id"]), "display_id": f"CHG-{ch['number']}", "project_id": str(ch["project_id"]), "project_name": pn and pn["name"], "run_id": str(ch["run_id"]),
        "ticket_id": str(ch["ticket_id"]) if ch["ticket_id"] else None, "ticket_display_id": f"TKT-{tk['number']}" if tk else None, "title": ch["title"], "rationale": ch["rationale"],
        "risk": ch["risk"], "environment_key": ch["environment_key"], "status": ch["status"], "current_version": ch["current_version"], "created_at": ch["created_at"], "updated_at": ch["updated_at"],
        "version": ch["version"], "current": _version_view(cur), "versions": [{"version": v["version"], "content_hash": v["content_hash"], "created_at": v["created_at"], "instruction": v["instruction"]} for v in versions],
        "policy": policy, "approvals": [{"actor": a["display_name"] or a["email"] or str(a["actor_user_id"])[:8], "decision": a["decision"], "reason": a["reason"], "patch_version": a["patch_version"],
                                         "current": a["patch_hash"] == cur["content_hash"], "revoked": a["revoked_at"] is not None, "at": a["created_at"], "mine": a["actor_user_id"] == org.principal.user_id} for a in approvals],
        "approved_count": sum(1 for a in live if a["decision"] == "approve"), "rejected_count": sum(1 for a in live if a["decision"] == "reject"),
        "eligibility": _eligibility(ctx, org.principal.user_id, ch, policy, cur["content_hash"], approvals),
        "draft_pr": {"available": False, "reason": "TRACE's GitHub connection is read-only. Opening a draft pull request needs a separately authorized write connection, which is not configured. Download the patch and apply it yourself."},
    }


@router.get("/changes")
async def list_changes(request: Request, project_id: UUID | None = None, org: OrgContext = Depends(org_context), conn: AsyncConnection = Depends(get_conn)):
    rows = await (await conn.execute(
        "SELECT c.*, p.name AS project_name, v.content_hash, v.files, "
        "(SELECT count(*) FROM change_approvals a WHERE a.change_id=c.id AND a.patch_hash=v.content_hash AND a.decision='approve' AND a.revoked_at IS NULL) AS approved, "
        "COALESCE((SELECT required_approvals FROM change_policies q WHERE q.project_id=c.project_id), 1) AS needed "
        "FROM change_proposals c JOIN projects p ON p.id=c.project_id JOIN patch_versions v ON v.change_id=c.id AND v.version=c.current_version "
        "WHERE c.organization_id=%s AND EXISTS (SELECT 1 FROM project_memberships m WHERE m.project_id=c.project_id AND m.user_id=%s)"
        + (" AND c.project_id=%s" if project_id else "") + " ORDER BY c.updated_at DESC LIMIT 200",
        (org.organization_id, org.principal.user_id, *([project_id] if project_id else [])))).fetchall()
    out = [{"id": str(r["id"]), "display_id": f"CHG-{r['number']}", "project_id": str(r["project_id"]), "project_name": r["project_name"], "title": r["title"], "risk": r["risk"], "status": r["status"],
            "environment_key": r["environment_key"], "approved": r["approved"], "needed": r["needed"], "files": [f["path"] for f in r["files"]], "version": r["current_version"],
            "run_id": str(r["run_id"]), "updated_at": r["updated_at"], "created_at": r["created_at"]} for r in rows]
    return wrap(request, out)


@router.get("/changes/{change_id}")
async def get_change(change_id: UUID, request: Request, org: OrgContext = Depends(org_context), conn: AsyncConnection = Depends(get_conn)):
    ch, ctx = await _load(conn, org, change_id)
    return wrap(request, await _detail(conn, org, ch, ctx))


@router.post("/changes/{change_id}/versions", status_code=201)
async def revise_change(change_id: UUID, body: ReviseIn, request: Request, org: OrgContext = Depends(org_context), conn: AsyncConnection = Depends(get_conn), driver=Depends(neo4j_driver)):
    """An edit is a new immutable version: the reviewer's instruction goes back to the model with the current diff. Earlier approvals do not carry over."""
    ch, ctx = await _load(conn, org, change_id, lock=True)
    ctx.require(Cap.PROPOSE)
    if ch["status"] not in ("proposed", "approved"):
        raise Conflict(f"A {ch['status'].replace('_', ' ')} change cannot be edited.", code="change_closed")
    cur = await (await conn.execute("SELECT * FROM patch_versions WHERE change_id=%s AND version=%s", (change_id, ch["current_version"]))).fetchone()
    s = request.app.state.container.settings
    pid = str(ch["project_id"])
    paths = [f["path"] for f in cur["files"]]
    try:
        prop = await asyncio.to_thread(P.propose, driver[0], driver[1], pid, str(ch["run_id"]), api_key=s.openai_api_key, model=s.trace_model, instruction=body.instruction,
                                       previous_diff=cur["diff"], only_paths=paths)
    except P.ProposalError as e:
        await conn.rollback()
        raise (DependencyUnavailable if e.code == "model_unavailable" else ProposalFailed)(e.message, code=e.code) from None
    if prop.content_hash == cur["content_hash"]:
        await conn.rollback()
        raise Unprocessable("That instruction produced the same patch, so no new version was created.", code="no_change")
    nv = ch["current_version"] + 1
    pv = await _insert_version(conn, change_id, nv, prop, org.principal.user_id, body.instruction)
    gid = await asyncio.to_thread(_persist_patch, driver[0], driver[1], pid, str(ch["run_id"]), prop, nv, str(pv["id"]))
    await conn.execute("UPDATE patch_versions SET graph_patch_id=%s WHERE id=%s", (gid, pv["id"]))
    await conn.execute("UPDATE change_proposals SET current_version=%s, title=%s, rationale=%s, risk=%s, status='proposed', version=version+1 WHERE id=%s", (nv, prop.title, prop.rationale, prop.risk, change_id))
    await audit(conn, organization_id=ctx.org.organization_id, project_id=ctx.project_id, actor=org.principal.user_id, action="change.revise", target_type="change", target_id=change_id,
                request=request, details={"version": nv, "hash": prop.content_hash})
    await conn.commit()
    ch, ctx = await _load(conn, org, change_id)
    return wrap(request, await _detail(conn, org, ch, ctx))


@router.post("/changes/{change_id}/approvals")
async def decide(change_id: UUID, body: ApprovalIn, request: Request, org: OrgContext = Depends(org_context), conn: AsyncConnection = Depends(get_conn)):
    ch, ctx = await _load(conn, org, change_id, lock=True)
    cur = await (await conn.execute("SELECT content_hash, version FROM patch_versions WHERE change_id=%s AND version=%s", (change_id, ch["current_version"]))).fetchone()
    if body.patch_hash != cur["content_hash"]:
        await conn.rollback()
        raise Conflict("This change was edited after you opened it. Review the new version.", code="stale_patch")
    policy = await _policy(conn, ch["project_id"])
    approvals = [dict(r) for r in await (await conn.execute("SELECT * FROM change_approvals WHERE change_id=%s", (change_id,))).fetchall()]
    el = _eligibility(ctx, org.principal.user_id, ch, policy, cur["content_hash"], approvals)
    if not el["can_approve"]:
        await conn.rollback()
        raise Forbidden(el["reason"])
    await conn.execute("INSERT INTO change_approvals(change_id, patch_version, patch_hash, actor_user_id, decision, reason) VALUES (%s,%s,%s,%s,%s,%s)",
                       (change_id, cur["version"], cur["content_hash"], org.principal.user_id, body.decision, body.reason))
    n = (await (await conn.execute("SELECT count(*) AS n FROM change_approvals WHERE change_id=%s AND patch_hash=%s AND decision='approve' AND revoked_at IS NULL", (change_id, cur["content_hash"]))).fetchone())["n"]
    status = "rejected" if body.decision == "reject" else ("approved" if n >= policy["required_approvals"] else ch["status"])
    await conn.execute("UPDATE change_proposals SET status=%s, version=version+1 WHERE id=%s", (status, change_id))
    await audit(conn, organization_id=ctx.org.organization_id, project_id=ctx.project_id, actor=org.principal.user_id, action=f"change.{body.decision}", target_type="change", target_id=change_id,
                request=request, details={"hash": cur["content_hash"], "version": cur["version"]})
    await conn.commit()
    ch, ctx = await _load(conn, org, change_id)
    return wrap(request, await _detail(conn, org, ch, ctx))


@router.post("/changes/{change_id}/approvals/revoke")
async def revoke(change_id: UUID, request: Request, org: OrgContext = Depends(org_context), conn: AsyncConnection = Depends(get_conn)):
    """Withdraw your own decision on the current version; an approved change goes back to waiting."""
    ch, ctx = await _load(conn, org, change_id, lock=True)
    if ch["status"] in ("draft_pr_opened", "merged", "withdrawn"):
        await conn.rollback()
        raise Conflict("This change has already moved on.", code="change_closed")
    cur = await (await conn.execute("SELECT content_hash FROM patch_versions WHERE change_id=%s AND version=%s", (change_id, ch["current_version"]))).fetchone()
    got = await (await conn.execute("UPDATE change_approvals SET revoked_at=now() WHERE change_id=%s AND patch_hash=%s AND actor_user_id=%s AND revoked_at IS NULL RETURNING id",
                                    (change_id, cur["content_hash"], org.principal.user_id))).fetchone()
    if got is None:
        await conn.rollback()
        raise NotFound("You have no active decision on this version")
    policy = await _policy(conn, ch["project_id"])
    n = (await (await conn.execute("SELECT count(*) AS n FROM change_approvals WHERE change_id=%s AND patch_hash=%s AND decision='approve' AND revoked_at IS NULL", (change_id, cur["content_hash"]))).fetchone())["n"]
    await conn.execute("UPDATE change_proposals SET status=%s, version=version+1 WHERE id=%s", ("approved" if n >= policy["required_approvals"] else "proposed", change_id))
    await audit(conn, organization_id=ctx.org.organization_id, project_id=ctx.project_id, actor=org.principal.user_id, action="change.revoke", target_type="change", target_id=change_id, request=request)
    await conn.commit()
    ch, ctx = await _load(conn, org, change_id)
    return wrap(request, await _detail(conn, org, ch, ctx))


@router.post("/changes/{change_id}/withdraw")
async def withdraw(change_id: UUID, request: Request, org: OrgContext = Depends(org_context), conn: AsyncConnection = Depends(get_conn)):
    ch, ctx = await _load(conn, org, change_id, lock=True)
    ctx.require(Cap.PROPOSE)
    if ch["status"] not in ("proposed", "approved", "rejected"):
        await conn.rollback()
        raise Conflict("This change has already moved on.", code="change_closed")
    await conn.execute("UPDATE change_proposals SET status='withdrawn', version=version+1 WHERE id=%s", (change_id,))
    await audit(conn, organization_id=ctx.org.organization_id, project_id=ctx.project_id, actor=org.principal.user_id, action="change.withdraw", target_type="change", target_id=change_id, request=request)
    await conn.commit()
    ch, ctx = await _load(conn, org, change_id)
    return wrap(request, await _detail(conn, org, ch, ctx))


@router.get("/changes/{change_id}/patch")
async def download_patch(change_id: UUID, org: OrgContext = Depends(org_context), conn: AsyncConnection = Depends(get_conn)):
    """The exact diff as a patch file. Nothing is applied by TRACE; `git apply` it at the base commit named in the header."""
    ch, _ = await _load(conn, org, change_id)
    v = await (await conn.execute("SELECT * FROM patch_versions WHERE change_id=%s AND version=%s", (change_id, ch["current_version"]))).fetchone()
    head = f"# CHG-{ch['number']} v{v['version']} · {ch['title']}\n# base commit {v['base_commit']}\n# sha256 {v['content_hash']}\n# Proposed only. Not applied, not tested.\n"
    return PlainTextResponse(head + v["diff"], headers={"content-disposition": f'attachment; filename="CHG-{ch["number"]}-v{v["version"]}.patch"'})


@router.post("/changes/{change_id}/draft-pr")
async def draft_pr(change_id: UUID, org: OrgContext = Depends(org_context), conn: AsyncConnection = Depends(get_conn)):
    ch, _ = await _load(conn, org, change_id)
    if ch["status"] != "approved":
        raise Conflict("The change must be approved first.", code="not_approved")
    raise DependencyUnavailable("Opening a draft pull request needs a separately authorized GitHub write connection. TRACE's GitHub connection is read-only, so nothing was written.", code="write_connection_missing")


@router.get("/projects/{project_id}/change-policy")
async def get_policy(request: Request, ctx: ProjectContext = Depends(project_access(Cap.READ)), conn: AsyncConnection = Depends(get_conn)):
    return wrap(request, await _policy(conn, ctx.project_id))


@router.put("/projects/{project_id}/change-policy")
async def put_policy(body: PolicyIn, request: Request, ctx: ProjectContext = Depends(project_access(Cap.MANAGE_PROJECT)), conn: AsyncConnection = Depends(get_conn)):
    await conn.execute(
        "INSERT INTO change_policies(project_id, required_approvals, author_can_approve, updated_by) VALUES (%s,%s,%s,%s) ON CONFLICT (project_id) DO UPDATE SET "
        "required_approvals=EXCLUDED.required_approvals, author_can_approve=EXCLUDED.author_can_approve, updated_by=EXCLUDED.updated_by, updated_at=now()",
        (ctx.project_id, body.required_approvals, body.author_can_approve, ctx.org.principal.user_id))
    await audit(conn, organization_id=ctx.org.organization_id, project_id=ctx.project_id, actor=ctx.org.principal.user_id, action="change.policy.update", target_type="project",
                target_id=ctx.project_id, request=request, details=body.model_dump())
    await conn.commit()
    return wrap(request, await _policy(conn, ctx.project_id))

