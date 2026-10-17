"""Graph explorer: the project's evidence graph exactly as it is stored in Neo4j (labels, properties, typed relationships),
for a Neo4j-Browser-style view in the web app.

Read only and project scoped: every MATCH carries the project id, labels and relationship types are allowlisted from the
ontology before they reach Cypher, and every response is bounded. People never send Cypher; each response instead carries
the equivalent Cypher (`cypher`) so the same graph can be opened in Neo4j Browser / Aura to check it.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from psycopg import AsyncConnection

from app.api.v1.deps_graph import neo4j_driver
from app.auth.authz import Cap
from app.auth.deps import ProjectContext, get_conn, project_access
from app.envelope import wrap
from app.errors import NotFound, Unprocessable
from app.graph import ontology as O
from app.graph.queries import escape_lucene

router = APIRouter(tags=["graph"])

MAX_NODES = 300
MAX_RELS = 3000
MAX_STR = 2000
#: Shown by default: what a ticket, an investigation and a deployment trace through. Step/ToolExecution bookkeeping and the
#: code body (files, chunks, symbols) are one expand away so the first view stays readable.
TRACE_LABELS = (
    "Project", "Environment", "Service", "Resource", "Journey", "SourceConnection", "Repository", "Revision", "GraphBuild", "Deployment",
    "Change", "PullRequest", "Ticket", "InvestigationRun", "Hypothesis", "Assessment", "Finding", "Claim", "ImpactAssessment", "HumanCheck",
    "ResolutionPlan", "PatchVersion", "VerificationRun", "EvidenceGap", "ExternalChangeTicket", "Review",
)
ERROR_LEVELS = ["error", "fatal", "critical", "err"]
#: Caption = first present property, per label (Neo4j Browser's "caption" setting).
CAPTION_KEYS = ("display_id", "name", "qualified_name", "title", "statement", "summary", "text", "message", "operation", "label", "path",
                "commit_sha", "provider_id", "metric", "question", "kind", "tool_name", "status")
_ID = re.compile(r"^[A-Za-z0-9_:.\-]{1,120}$")


# ---- serialisation -------------------------------------------------------------------------------------------------------
def _value(v: Any) -> Any:
    if hasattr(v, "iso_format"):
        return v.iso_format()
    if isinstance(v, str) and len(v) > MAX_STR:
        return v[:MAX_STR] + f"… [{len(v) - MAX_STR} more characters]"
    if isinstance(v, list):
        return [_value(x) for x in v[:200]]
    return v


def primary_label(labels: list[str]) -> str:
    specific = [x for x in labels if x not in (O.BASE_LABEL, "EvidenceRecord")]
    return specific[0] if specific else ("EvidenceRecord" if "EvidenceRecord" in labels else labels[0] if labels else "Entity")


def caption(label: str, props: dict) -> str:
    if label == "Ticket" and props.get("display_id"):
        return f"{props['display_id']} {props.get('title', '')}".strip()
    if label == "Revision" and props.get("commit_sha"):
        return str(props["commit_sha"])[:7]
    if label in ("SourceFile",) and props.get("path"):
        return str(props["path"]).rsplit("/", 1)[-1]
    if label == "Repository" and props.get("canonical_url"):
        return str(props["canonical_url"]).rstrip("/").split("github.com/")[-1]
    if label == "Step":
        return f"{props.get('sequence')}. {props.get('kind')}"
    if label == "SourceChunk":
        return f"L{props.get('start_line')}–{props.get('end_line')}"
    for k in CAPTION_KEYS:
        v = props.get(k)
        if v not in (None, ""):
            s = str(v)
            return s if len(s) <= 60 else s[:57] + "…"
    return str(props.get("id", ""))[:12]


def node_out(n) -> dict:
    labels = sorted(n.labels, key=lambda x: (x == O.BASE_LABEL, x == "EvidenceRecord", x))
    props = {k: _value(v) for k, v in dict(n).items() if not k.startswith("_")}
    lab = primary_label(labels)
    return {"id": props.get("id"), "label": lab, "labels": [x for x in labels if x != O.BASE_LABEL], "caption": caption(lab, props), "properties": props}


def rel_out(r, start: str, end: str) -> dict:
    """`start`/`end` are the endpoint ids: the driver does not hydrate a relationship's endpoint properties on its own."""
    return {"id": r.element_id, "type": r.type, "start": start, "end": end,
            "properties": {k: _value(v) for k, v in dict(r).items() if k not in ("project_id",)}}


def _lit(v: Any) -> str:
    """Cypher literal for the display query (JSON string escaping is valid Cypher string syntax)."""
    return json.dumps(v)


def _check_id(node_id: str) -> str:
    if not _ID.match(node_id):
        raise Unprocessable("Not a graph node id")
    return node_id


def _check_label(label: str) -> str:
    if label not in O.NODES:
        raise Unprocessable(f"Unknown label {label!r}. Labels come from the TRACE ontology.")
    return label


def _check_type(t: str | None) -> str | None:
    if t is not None and t not in O.RELATIONSHIPS:
        raise Unprocessable(f"Unknown relationship type {t!r}")
    return t


def _connect(s, pid: str, ids: list[str]) -> list[dict]:
    """Every relationship between the given nodes (Neo4j Browser's 'connect result nodes')."""
    if not ids:
        return []
    return [rel_out(r["r"], r["a"], r["b"]) for r in s.run(
        "MATCH (a:Entity {project_id: $p})-[r]->(b:Entity {project_id: $p}) WHERE a.id IN $ids AND b.id IN $ids RETURN r, a.id AS a, b.id AS b LIMIT $lim",
        p=pid, ids=ids, lim=MAX_RELS)]


# ---- queries (run in a worker thread) -----------------------------------------------------------------------------------
def _schema(driver, db: str, pid: str) -> dict:
    with driver.session(database=db) as s:
        labels = [dict(r) for r in s.run(
            "MATCH (n:Entity {project_id: $p}) UNWIND labels(n) AS label WITH label WHERE label <> 'Entity' RETURN label, count(*) AS count ORDER BY count DESC, label", p=pid)]
        rels = [dict(r) for r in s.run(
            "MATCH (:Entity {project_id: $p})-[r]->() RETURN type(r) AS type, count(*) AS count ORDER BY count DESC, type", p=pid)]
        keys = [r["k"] for r in s.run(
            "MATCH (n:Entity {project_id: $p}) WITH n LIMIT 5000 UNWIND keys(n) AS k WITH DISTINCT k WHERE NOT k STARTS WITH '_' RETURN k ORDER BY k", p=pid)]
    return {"labels": labels, "relationship_types": rels, "property_keys": keys}


def _explore(driver, db: str, pid: str, view: str, label: str | None, limit: int, rel_type: str | None = None) -> dict:
    with driver.session(database=db) as s:
        if view == "type":
            rows = list(s.run(f"MATCH (a:Entity {{project_id: $p}})-[r:`{rel_type}`]->(b:Entity {{project_id: $p}}) RETURN a, r, b LIMIT $lim", p=pid, lim=min(limit, 100) + 1))
            truncated = len(rows) > min(limit, 100)
            rows = rows[: min(limit, 100)]
            seen: dict[str, dict] = {}
            for r in rows:
                for n in (r["a"], r["b"]):
                    seen.setdefault(n["id"], node_out(n))
            return {"nodes": list(seen.values()), "relationships": _connect(s, pid, list(seen)), "truncated": truncated,
                    "cypher": f"MATCH p = (a {{project_id: {_lit(pid)}}})-[r:{rel_type}]->(b)\nRETURN p LIMIT {min(limit, 100)}"}
        if view == "label":
            nodes = [node_out(r["n"]) for r in s.run(
                f"MATCH (n:Entity:`{label}` {{project_id: $p}}) RETURN n ORDER BY coalesce(n.event_time, n.created_at) DESC LIMIT $lim", p=pid, lim=limit + 1)]
            cypher = f"MATCH (n:{label} {{project_id: {_lit(pid)}}})\nRETURN n LIMIT {limit}"
        else:
            nodes = [node_out(r["n"]) for r in s.run(
                "MATCH (n:Entity {project_id: $p}) WHERE any(l IN labels(n) WHERE l IN $labels) "
                "RETURN n ORDER BY CASE WHEN n:Project THEN 0 WHEN n:Environment OR n:Service THEN 1 ELSE 2 END, coalesce(n.event_time, n.created_at) DESC LIMIT $lim",
                p=pid, labels=list(TRACE_LABELS), lim=limit + 1)]
            errors = [node_out(r["n"]) for r in s.run(
                "MATCH (n:Entity:LogEvent {project_id: $p}) WHERE toLower(n.severity) IN $lv RETURN n ORDER BY n.event_time DESC LIMIT 25", p=pid, lv=ERROR_LEVELS)]
            nodes = nodes + [e for e in errors if e["id"] not in {n["id"] for n in nodes}]
            cypher = (f"MATCH (n {{project_id: {_lit(pid)}}})\nWHERE any(l IN labels(n) WHERE l IN {_lit(list(TRACE_LABELS))})\n"
                      f"   OR (n:LogEvent AND toLower(n.severity) IN {_lit(ERROR_LEVELS)})\nRETURN n LIMIT {limit + 25}")
        truncated = len(nodes) > limit + (25 if view != "label" else 0)
        nodes = nodes[: limit + (25 if view != "label" else 0)]
        rels = _connect(s, pid, [n["id"] for n in nodes])
    return {"nodes": nodes, "relationships": rels, "truncated": truncated, "cypher": cypher}


def _node_detail(driver, db: str, pid: str, node_id: str) -> dict | None:
    with driver.session(database=db) as s:
        row = s.run("MATCH (n:Entity {project_id: $p, id: $id}) RETURN n", p=pid, id=node_id).single()
        if row is None:
            return None
        degree = [dict(r) for r in s.run(
            "MATCH (n:Entity {project_id: $p, id: $id})-[r]-(m:Entity {project_id: $p}) "
            "RETURN type(r) AS type, CASE WHEN startNode(r) = n THEN 'out' ELSE 'in' END AS direction, "
            "[l IN labels(m) WHERE l <> 'Entity' AND l <> 'EvidenceRecord'][0] AS label, count(*) AS count ORDER BY count DESC",
            p=pid, id=node_id)]
    return {"node": node_out(row["n"]), "degree": degree}


def _neighbors(driver, db: str, pid: str, node_id: str, rel_type: str | None, direction: str, limit: int) -> dict | None:
    pattern = {"out": "-[r]->", "in": "<-[r]-", "both": "-[r]-"}[direction]
    typed = f":`{rel_type}`" if rel_type else ""
    pattern = pattern.replace("[r]", f"[r{typed}]")
    with driver.session(database=db) as s:
        row = s.run("MATCH (n:Entity {project_id: $p, id: $id}) RETURN n", p=pid, id=node_id).single()
        if row is None:
            return None
        res = list(s.run(
            f"MATCH (n:Entity {{project_id: $p, id: $id}}){pattern}(m:Entity {{project_id: $p}}) RETURN DISTINCT m, count(r) AS k "
            "ORDER BY k DESC, m.id LIMIT $lim", p=pid, id=node_id, lim=limit + 1))
        total = s.run(f"MATCH (n:Entity {{project_id: $p, id: $id}}){pattern}(m:Entity {{project_id: $p}}) RETURN count(DISTINCT m) AS c",
                      p=pid, id=node_id).single()["c"]
        nodes = [node_out(row["n"])] + [node_out(r["m"]) for r in res[:limit]]
        rels = _connect(s, pid, [n["id"] for n in nodes])
    cy_pat = pattern.replace(f"[r{typed}]", f"[r{':' + rel_type if rel_type else ''}]")
    return {"nodes": nodes, "relationships": rels, "truncated": len(res) > limit, "total": total,
            "cypher": f"MATCH (n {{project_id: {_lit(pid)}, id: {_lit(node_id)}}}){cy_pat}(m)\nRETURN n, r, m LIMIT {limit}"}


def _search(driver, db: str, pid: str, q: str, limit: int) -> list[dict]:
    with driver.session(database=db) as s:
        exact = [node_out(r["n"]) for r in s.run("MATCH (n:Entity {project_id: $p}) WHERE n.id = $q OR n.id STARTS WITH $q RETURN n LIMIT 5", p=pid, q=q)] if _ID.match(q) and "_" in q else []
        # The index analyzer splits on punctuation ("TKT-7" is indexed as tkt, 7), so the query is split the same way.
        terms = [escape_lucene(t) for t in re.findall(r"\w+", q)][:6]
        if not terms:
            return exact
        lucene = " AND ".join(f"{t}*" if len(t) > 1 else t for t in terms)
        try:
            hits = [node_out(r["node"]) for r in s.run(
                "CALL db.index.fulltext.queryNodes('entity_caption', $q) YIELD node, score WHERE node.project_id = $p RETURN node ORDER BY score DESC LIMIT $lim",
                q=lucene, p=pid, lim=limit)]
        except Exception:  # noqa: BLE001 - index not migrated yet: bounded substring fallback
            hits = [node_out(r["node"]) for r in s.run(
                "MATCH (node:Entity {project_id: $p}) WHERE any(k IN ['name','title','qualified_name','path','message','display_id','statement','summary'] "
                "WHERE toLower(toString(node[k])) CONTAINS toLower($q)) RETURN node LIMIT $lim", p=pid, q=q, lim=limit)]
    seen: set[str] = set()
    return [n for n in exact + hits if not (n["id"] in seen or seen.add(n["id"]))][:limit]


def _path(driver, db: str, pid: str, a: str, b: str, hops: int) -> dict | None:
    with driver.session(database=db) as s:
        row = s.run(
            "MATCH (a:Entity {project_id: $p, id: $a}), (b:Entity {project_id: $p, id: $b}) "
            f"MATCH path = shortestPath((a)-[*..{hops}]-(b)) WHERE all(n IN nodes(path) WHERE n.project_id = $p) RETURN path",
            p=pid, a=a, b=b).single()
    if row is None:
        return None
    p = row["path"]
    ids = {n.element_id: n["id"] for n in p.nodes}
    return {"nodes": [node_out(n) for n in p.nodes], "relationships": [rel_out(r, ids[r.start_node.element_id], ids[r.end_node.element_id]) for r in p.relationships], "truncated": False,
            "cypher": f"MATCH (a {{id: {_lit(a)}}}), (b {{id: {_lit(b)}}})\nMATCH p = shortestPath((a)-[*..{hops}]-(b))\nRETURN p"}


# ---- routes -------------------------------------------------------------------------------------------------------------
@router.get("/projects/{project_id}/graph/schema")
async def graph_schema(request: Request, ctx: ProjectContext = Depends(project_access(Cap.READ)), conn: AsyncConnection = Depends(get_conn),
                       driver=Depends(neo4j_driver)):
    """Labels and relationship types present in this project's graph, with counts (Neo4j Browser's database overview), plus the
    ontology they come from and how many stored events have not reached the graph yet."""
    data = await asyncio.to_thread(_schema, driver[0], driver[1], str(ctx.project_id))
    pending = (await (await conn.execute("SELECT count(*) FILTER (WHERE graph_status='pending') AS pending, count(*) FILTER (WHERE graph_status='failed') AS failed "
                                         "FROM evidence_inbox WHERE project_id=%s", (ctx.project_id,))).fetchone())
    data["ontology"] = {"version": O.ONTOLOGY_VERSION, "labels": {k: v.doc for k, v in O.NODES.items()},
                        "relationships": {k: {"from": sorted(v.sources), "to": sorted(v.targets), "doc": v.doc} for k, v in O.RELATIONSHIPS.items()}}
    data["stored_events"] = {"pending": pending["pending"], "failed": pending["failed"]}
    data["database"] = driver[1]
    return wrap(request, data)


@router.get("/projects/{project_id}/graph/explore")
async def graph_explore(request: Request, view: Literal["trace", "label", "type"] = "trace", label: str | None = Query(None, max_length=40),
                        type: str | None = Query(None, max_length=40),
                        limit: int = Query(150, ge=1, le=MAX_NODES), ctx: ProjectContext = Depends(project_access(Cap.READ)), driver=Depends(neo4j_driver)):
    """`trace`: the project's backbone (scope, sources, revisions, deployments, tickets, investigations, findings) plus recent
    error events. `label`: the newest nodes of one label. `type`: up to 100 relationships of one type with their endpoints.
    Relationships among the returned nodes are always included."""
    if view == "label":
        _check_label(label or "")
    if view == "type" and not _check_type(type):
        raise Unprocessable("type is required for view=type")
    return wrap(request, await asyncio.to_thread(_explore, driver[0], driver[1], str(ctx.project_id), view, label, limit, type))


@router.get("/projects/{project_id}/graph/nodes/{node_id}")
async def graph_node(node_id: str, request: Request, ctx: ProjectContext = Depends(project_access(Cap.READ)), driver=Depends(neo4j_driver)):
    """One node with all its properties and its relationship counts by type, direction and neighbour label."""
    data = await asyncio.to_thread(_node_detail, driver[0], driver[1], str(ctx.project_id), _check_id(node_id))
    if data is None:
        raise NotFound("Node not found in this project's graph")
    return wrap(request, data)


@router.get("/projects/{project_id}/graph/nodes/{node_id}/neighbors")
async def graph_neighbors(node_id: str, request: Request, type: str | None = Query(None, max_length=40), direction: Literal["out", "in", "both"] = "both",
                          limit: int = Query(50, ge=1, le=200), ctx: ProjectContext = Depends(project_access(Cap.READ)), driver=Depends(neo4j_driver)):
    """Expand a node (double-click in Neo4j Browser): its neighbours, optionally of one relationship type and direction."""
    data = await asyncio.to_thread(_neighbors, driver[0], driver[1], str(ctx.project_id), _check_id(node_id), _check_type(type), direction, limit)
    if data is None:
        raise NotFound("Node not found in this project's graph")
    return wrap(request, data)


@router.get("/projects/{project_id}/graph/search")
async def graph_search(request: Request, q: str = Query(..., min_length=1, max_length=120), limit: int = Query(20, ge=1, le=50),
                       ctx: ProjectContext = Depends(project_access(Cap.READ)), driver=Depends(neo4j_driver)):
    """Find nodes by name, title, path, message, commit or id."""
    return wrap(request, await asyncio.to_thread(_search, driver[0], driver[1], str(ctx.project_id), q.strip(), limit))


@router.get("/projects/{project_id}/graph/path")
async def graph_path(request: Request, from_id: str = Query(..., alias="from", max_length=120), to_id: str = Query(..., alias="to", max_length=120),
                     max_hops: int = Query(8, ge=1, le=12), ctx: ProjectContext = Depends(project_access(Cap.READ)), driver=Depends(neo4j_driver)):
    """Trace: the shortest chain of relationships between two nodes (any type, either direction), inside the project."""
    data = await asyncio.to_thread(_path, driver[0], driver[1], str(ctx.project_id), _check_id(from_id), _check_id(to_id), max_hops)
    if data is None:
        raise NotFound(f"No path of at most {max_hops} relationships connects these nodes")
    return wrap(request, data)
