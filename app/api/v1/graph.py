"""Read access to a project's evidence graph. The graph project id is the control-plane project UUID as a string, so
the same membership check that guards every other project route guards the graph. Only the published build of each
repository is read (staging, failed and superseded builds are invisible), and responses are bounded.
"""
from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from psycopg import AsyncConnection
from pydantic import BaseModel, Field

from app.api.v1.deps_graph import neo4j_driver
from app.audit import audit
from app.auth.authz import Cap
from app.auth.deps import ProjectContext, get_conn, project_access
from app.envelope import wrap
from app.errors import NotFound, Unprocessable
from app.jobs import queue

router = APIRouter(tags=["graph"])

MAX_SYMBOLS = 400


def _published_builds(session, pid: str) -> list[dict]:
    return [dict(r) for r in session.run(
        "MATCH (b:GraphBuild {project_id: $p, status: 'published'})-[:INDEXES]->(rev:Revision {project_id: $p}) "
        "MATCH (rep:Repository {project_id: $p, id: rev.repository_id}) "
        "RETURN b.id AS build_id, b.counts AS counts, b.extractor_version AS extractor, rev.commit_sha AS commit_sha, rev.id AS revision_id, "
        "rep.provider_repository_id AS repository, toString(b.created_at) AS built_at ORDER BY rep.provider_repository_id", p=pid)]


def _code_graph(driver, database: str, pid: str, limit: int, community: str | None) -> dict:
    with driver.session(database=database) as s:
        builds = _published_builds(s, pid)
        if not builds:
            return {"status": "none", "builds": [], "nodes": [], "edges": [], "communities": [], "truncated": False}
        ids = [b["build_id"] for b in builds]
        comms = [dict(r) for r in s.run(
            "MATCH (c:CodeCommunity {project_id: $p}) WHERE c.build_id IN $b "
            "OPTIONAL MATCH (:CodeSymbol {project_id: $p})-[m:MEMBER_OF]->(c) "
            "WITH c, count(m) AS symbols WHERE symbols > 0 RETURN c.id AS id, c.label AS label, symbols ORDER BY symbols DESC", p=pid, b=ids)]
        # most connected symbols first, so a bounded response keeps the structure that matters
        syms = [dict(r) for r in s.run(
            "MATCH (s:CodeSymbol {project_id: $p})<-[d:DEFINES]-(f:SourceFile {project_id: $p}) WHERE d.build_id IN $b "
            "OPTIONAL MATCH (s)-[mm:MEMBER_OF]->(c:CodeCommunity {project_id: $p}) WHERE mm.build_id IN $b "
            "WITH s, f, c, size([(s)-[x:CALLS|IMPORTS|USES|INHERITS_FROM|CONTAINS]-() WHERE x.build_id IN $b | x]) AS degree "
            "WHERE $comm IS NULL OR c.id = $comm "
            "RETURN s.id AS id, s.qualified_name AS label, s.kind AS kind, f.path AS file, s.start_line AS line, c.id AS community, degree "
            "ORDER BY degree DESC, s.id LIMIT $limit", p=pid, b=ids, limit=limit + 1, comm=community)]
        truncated = len(syms) > limit
        syms = syms[:limit]
        keep = [x["id"] for x in syms]
        edges = [dict(r) for r in s.run(
            "MATCH (a:CodeSymbol {project_id: $p})-[r:CALLS|USES|INHERITS_FROM|CONTAINS]->(b:CodeSymbol {project_id: $p}) "
            "WHERE r.build_id IN $b AND a.id IN $keep AND b.id IN $keep "
            "RETURN a.id AS source, b.id AS target, type(r) AS relation, r.derivation AS derivation, r.source_locator AS locator LIMIT 2000",
            p=pid, b=ids, keep=keep)]
    return {"status": "ready", "builds": builds, "nodes": syms, "edges": edges, "communities": comms, "truncated": truncated}


@router.get("/projects/{project_id}/graph/code")
async def code_graph(
    request: Request,
    limit: int = Query(200, ge=1, le=MAX_SYMBOLS),
    community: str | None = Query(None, max_length=80),
    ctx: ProjectContext = Depends(project_access(Cap.READ)),
    driver=Depends(neo4j_driver),
):
    """Published code graph (symbols and structural relationships). Structure only: static reachability is never runtime proof."""
    data = await asyncio.to_thread(_code_graph, driver[0], driver[1], str(ctx.project_id), limit, community)
    return wrap(request, data)


def _symbol_detail(driver, database: str, pid: str, symbol_id: str) -> dict | None:
    with driver.session(database=database) as s:
        builds = [b["build_id"] for b in _published_builds(s, pid)]
        row = s.run(
            "MATCH (s:CodeSymbol {project_id: $p, id: $id})<-[:DEFINES]-(f:SourceFile {project_id: $p}) "
            "OPTIONAL MATCH (f)-[:HAS_CHUNK]->(c:SourceChunk {project_id: $p}) WHERE c.start_line <= s.end_line AND c.end_line >= s.start_line "
            "RETURN s.qualified_name AS name, s.kind AS kind, s.start_line AS start, s.end_line AS end, f.path AS file, f.revision_id AS revision_id, "
            "collect({start: c.start_line, end: c.end_line, content: c.content, hash: c.content_hash}) AS chunks", p=pid, id=symbol_id).single()
        if row is None:
            return None
        rel = lambda direction: [dict(r) for r in s.run(  # noqa: E731
            f"MATCH (s:CodeSymbol {{project_id: $p, id: $id}}){direction}(o:CodeSymbol {{project_id: $p}}) WHERE r.build_id IN $b "
            "MATCH (f:SourceFile {project_id: $p, id: o.file_id}) "
            "RETURN o.id AS id, o.qualified_name AS name, f.path AS file, type(r) AS relation, r.derivation AS derivation, r.source_locator AS locator "
            "ORDER BY o.qualified_name LIMIT 100", p=pid, id=symbol_id, b=builds)]
        used_by = rel("<-[r:CALLS|USES|INHERITS_FROM]-")
        uses = rel("-[r:CALLS|USES|INHERITS_FROM]->")
    return {**dict(row), "used_by": used_by, "uses": uses, "chunks": sorted((c for c in row["chunks"] if c["start"] is not None), key=lambda c: c["start"])}


@router.get("/projects/{project_id}/graph/symbols/{symbol_id}")
async def symbol_detail(
    request: Request,
    symbol_id: str,
    ctx: ProjectContext = Depends(project_access(Cap.READ)),
    driver=Depends(neo4j_driver),
):
    """One symbol with its callers, callees and the exact pinned source chunks (content hash included)."""
    data = await asyncio.to_thread(_symbol_detail, driver[0], driver[1], str(ctx.project_id), symbol_id)
    if data is None:
        raise NotFound("Symbol not found in this project's published graph")
    return wrap(request, data)


# ---- indexing requests (control plane) --------------------------------------------------------------------------------
class IndexRequest(BaseModel):
    #: Repository bindings to index; empty = every bound repository of the project.
    repository_binding_ids: list[UUID] = Field(default_factory=list, max_length=20)


RUN_COLS = ("id, repository_binding_id, status, stage, requested_ref, commit_sha, build_id, counts, gaps, extractor, error, "
            "created_at, started_at, finished_at")


@router.post("/projects/{project_id}/graph/index", status_code=202)
async def request_index(
    request: Request,
    body: IndexRequest,
    ctx: ProjectContext = Depends(project_access(Cap.INDEX)),
    conn: AsyncConnection = Depends(get_conn),
):
    """Queue indexing of the project's bound repositories at their selected branch/commit. Read-only against GitHub.
    A repository that already has a queued or running request returns that request instead of starting another."""
    sel = body.repository_binding_ids
    rows = await (await conn.execute(
        "SELECT id, selected_ref, full_name, connection_id FROM repository_bindings WHERE project_id=%s AND archived=false"
        + (" AND id = ANY(%s)" if sel else "") + " ORDER BY full_name",
        (ctx.project_id, sel) if sel else (ctx.project_id,))).fetchall()
    if not rows:
        raise Unprocessable("This project has no repositories selected. Choose repositories in the Code tab first.")
    out = []
    for b in rows:
        existing = await (await conn.execute(
            f"SELECT {RUN_COLS} FROM graph_index_runs WHERE repository_binding_id=%s AND status IN ('queued','running')", (b["id"],))).fetchone()
        if existing:
            out.append({**existing, "repository": b["full_name"]})
            continue
        run = await (await conn.execute(
            "INSERT INTO graph_index_runs(organization_id, project_id, repository_binding_id, requested_ref, requested_by) VALUES (%s,%s,%s,%s,%s) "
            f"RETURNING {RUN_COLS}", (ctx.org.organization_id, ctx.project_id, b["id"], b["selected_ref"], ctx.org.principal.user_id))).fetchone()
        job = await queue.enqueue(conn, organization_id=ctx.org.organization_id, project_id=ctx.project_id, kind="graph.index",
                                  payload={"run_id": str(run["id"])}, requested_by=ctx.org.principal.user_id,
                                  resource_type="graph_index_run", resource_id=run["id"])
        await conn.execute("UPDATE graph_index_runs SET job_id=%s WHERE id=%s", (job["id"], run["id"]))
        await audit(conn, organization_id=ctx.org.organization_id, project_id=ctx.project_id, actor=ctx.org.principal.user_id, action="graph.index.request",
                    result="success", target_type="repository_binding", target_id=b["id"], details={"ref": b["selected_ref"]})
        out.append({**run, "repository": b["full_name"]})
    await conn.commit()
    return wrap(request, out)


@router.get("/projects/{project_id}/graph/index")
async def list_index_runs(
    request: Request,
    ctx: ProjectContext = Depends(project_access(Cap.READ)),
    conn: AsyncConnection = Depends(get_conn),
):
    """Latest run per repository binding, newest first. The UI polls this while a run is queued or running."""
    rows = await (await conn.execute(
        "SELECT DISTINCT ON (r.repository_binding_id) r.id, r.repository_binding_id, b.full_name AS repository, r.status, r.stage, r.requested_ref, "
        "r.commit_sha, r.build_id, r.counts, r.gaps, r.extractor, r.error, r.created_at, r.started_at, r.finished_at "
        "FROM graph_index_runs r JOIN repository_bindings b ON b.id=r.repository_binding_id WHERE r.project_id=%s "
        "ORDER BY r.repository_binding_id, r.created_at DESC", (ctx.project_id,))).fetchall()
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return wrap(request, rows)
