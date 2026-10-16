"""Read access to a project's evidence graph. The graph project id is the control-plane project UUID as a string, so
the same membership check that guards every other project route guards the graph. Only the published build of each
repository is read (staging, failed and superseded builds are invisible), and responses are bounded.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query, Request

from app.api.v1.deps_graph import neo4j_driver
from app.auth.authz import Cap
from app.auth.deps import ProjectContext, project_access
from app.envelope import wrap
from app.errors import NotFound

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
            "RETURN c.id AS id, c.label AS label, count(m) AS symbols ORDER BY symbols DESC", p=pid, b=ids)]
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
