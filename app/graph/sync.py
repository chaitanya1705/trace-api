"""Project evidence from the control plane (Postgres) into the evidence graph (Neo4j).

Neo4j is the investigator's only retrieval source, so everything an investigation may reason about has to be loaded
here first: scope (environments, deployed services), deployments with the revision they ran, and runtime evidence
(log events, request observations, counts). Nothing is invented: a deployment whose commit is not an indexed revision is
written without a revision (the mapping stays unknown), and an operation name only locates code when exactly one
symbol of the deployed revision carries that name (certainty `name_match`, never `exact`).

`collect` reads Postgres (async), `project` writes Neo4j (sync, run in a worker thread). Both are idempotent: ids are
deterministic and evidence nodes are immutable, so re-running a sync is a no-op for rows already in the graph.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from psycopg import AsyncConnection

from app.graph import identity as I
from app.graph.evidence import revision_manifest, write_deployment
from app.graph.repository import GraphRepository, NodeWrite, RelWrite, prepare_node, prepare_rel

SOURCE = "pulselab"
MAX_EVENTS = 5000


@dataclass
class Bundle:
    project_id: str
    organization_id: str
    project_name: str
    environments: list[dict]
    deployments: list[dict]
    tickets: list[dict] = field(default_factory=list)
    services: list[dict] = field(default_factory=list)
    connections: list[dict] = field(default_factory=list)
    repositories: list[dict] = field(default_factory=list)
    service_mappings: list[dict] = field(default_factory=list)
    all_tickets: list[dict] = field(default_factory=list)
    _manifests: dict = field(default_factory=dict)


@dataclass
class SyncReport:
    environments: int = 0
    services: int = 0
    deployments: int = 0
    deployments_with_revision: int = 0
    log_events: int = 0
    located: int = 0
    requests: int = 0
    measurements: int = 0
    tickets: list[str] = field(default_factory=list)
    connections: int = 0
    repositories: int = 0
    ticket_nodes: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, why: str, n: int = 1) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + n


async def collect(conn: AsyncConnection, project_id: UUID) -> Bundle:
    proj = await (await conn.execute("SELECT id, organization_id, name FROM projects WHERE id=%s", (project_id,))).fetchone()
    envs = [dict(r) for r in await (await conn.execute("SELECT id, name, environment_key FROM environments WHERE project_id=%s", (project_id,))).fetchall()]
    deps = [dict(r) for r in await (await conn.execute(
        "SELECT d.provider_deployment_id, d.provider_project_id, d.project_name, d.target, d.state, d.commit_sha, d.revision_mapping, d.created_at_provider, d.ready_at, "
        "e.environment_key FROM deployments d LEFT JOIN environments e ON e.id = d.environment_id WHERE d.project_id=%s ORDER BY d.created_at_provider", (project_id,))).fetchall()]
    tickets = [dict(r) for r in await (await conn.execute(
        "SELECT id, number, title, payload FROM tickets WHERE project_id=%s AND payload IS NOT NULL AND source='pulselab' ORDER BY number", (project_id,))).fetchall()]
    b = Bundle(str(proj["id"]), str(proj["organization_id"]), proj["name"], envs, deps, tickets)
    q = lambda sql: conn.execute(sql, (project_id,))  # noqa: E731
    b.services = [dict(r) for r in await (await q("SELECT name, service_key FROM services WHERE project_id=%s ORDER BY service_key")).fetchall()]
    b.connections = [dict(r) for r in await (await q(
        "SELECT id, provider, kind, auth_method, status, capabilities FROM connections WHERE project_id=%s ORDER BY created_at")).fetchall()]
    b.repositories = [dict(r) for r in await (await q(
        "SELECT b.id, b.connection_id, b.provider_repository_id, b.full_name, c.provider FROM repository_bindings b JOIN connections c ON c.id = b.connection_id "
        "WHERE b.project_id=%s AND b.archived = false ORDER BY b.full_name")).fetchall()]
    b.service_mappings = [dict(r) for r in await (await q(
        "SELECT m.repository_binding_id, s.service_key, m.path_prefix, m.valid_from, m.valid_to FROM repository_service_mappings m "
        "JOIN services s ON s.id = m.service_id WHERE m.project_id=%s")).fetchall()]
    b.all_tickets = [dict(r) for r in await (await q(
        "SELECT id, number, title, source, status, severity, environment_key, service_hint, revision_number, external_id, "
        "coalesce(source_created_at, created_at) AS reported_at FROM tickets WHERE project_id=%s ORDER BY number")).fetchall()]
    deployment_conn = {r["provider_deployment_id"]: r["connection_id"] for r in await (await q(
        "SELECT provider_deployment_id, connection_id FROM deployments WHERE project_id=%s")).fetchall()}
    for d in b.deployments:
        d["connection_id"] = deployment_conn.get(d["provider_deployment_id"])
    return b




def _manifest(repo: GraphRepository, b: Bundle, revision_id: str) -> str:
    """The revision's manifest id, looked up once per revision per sync (one Neo4j round trip each otherwise)."""
    if revision_id not in b._manifests:
        b._manifests[revision_id] = revision_manifest(repo, b.project_id, revision_id) or ""
    return b._manifests[revision_id]


def _t(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(UTC) if v else None


def revisions_by_sha(repo: GraphRepository, project_id: str) -> dict[str, dict]:
    with repo._driver.session(database=repo._db) as s:
        return {r["sha"]: {"id": r["id"], "repository_id": r["rep"]} for r in s.run(
            "MATCH (v:Entity:Revision {project_id: $p}) RETURN v.commit_sha AS sha, v.id AS id, v.repository_id AS rep", p=project_id)}


def symbols_named(repo: GraphRepository, project_id: str, revision_id: str, name: str) -> list[dict]:
    with repo._driver.session(database=repo._db) as s:
        return [r.data() for r in s.run(
            "MATCH (f:Entity:SourceFile {project_id: $p, revision_id: $r})-[:DEFINES]->(c:Entity:CodeSymbol {project_id: $p}) "
            "WHERE c.qualified_name = $n OR c.qualified_name ENDS WITH ('.' + $n) RETURN c.id AS id, c.qualified_name AS name, f.path AS path", p=project_id, r=revision_id, n=name)]


def write_deployment_row(repo: GraphRepository, pid: str, d: dict, *, env_id: str, service_id: str, revs: dict[str, dict]) -> dict:
    """One Postgres deployment row as a Deployment node. The revision is linked only when the commit is an indexed revision;
    otherwise the mapping stays unknown. Shared by the on-demand sync and the evidence applier so both write identical nodes."""
    rev = revs.get((d["commit_sha"] or "").lower())
    state = (d["state"] or "").lower()
    did = write_deployment(repo, pid, provider_id=d["provider_deployment_id"], service_id=service_id, environment_id=env_id, revision_id=rev["id"] if rev else None,
                           event_time=d["created_at_provider"], status="ready" if state == "ready" else state or "unknown", exposure_status="unknown")
    return {"id": did, "service_id": service_id, "environment_id": env_id, "revision_id": rev["id"] if rev else None, "sha": (d["commit_sha"] or "").lower()}


def project(repo: GraphRepository, b: Bundle) -> SyncReport:
    rep = SyncReport()
    pid = b.project_id
    prj = I.project_node_id(pid)
    repo.write_nodes([prepare_node("Project", prj, pid, {"organization_id": b.organization_id, "name": b.project_name})])
    revs = revisions_by_sha(repo, pid)
    env_ids: dict[str, str] = {}
    for e in b.environments:
        eid = I.environment_id(pid, e["environment_key"])
        repo.write_nodes([prepare_node("Environment", eid, pid, {"name": e["name"], "environment_key": e["environment_key"]})])
        repo.write_relationships([prepare_rel("HAS_ENVIRONMENT", "Project", prj, "Environment", eid, pid, I.edge_key("HAS_ENVIRONMENT", prj, eid))])
        env_ids[e["environment_key"]] = eid
        rep.environments += 1

    # A deployed service is the hosting resource that runs the code (one per provider project).
    svc_ids: dict[str, str] = {}

    def service_for(provider_project: str, name: str | None) -> str:
        if provider_project not in svc_ids:
            sid = I.service_id(pid, name or provider_project)
            repo.write_nodes([prepare_node("Service", sid, pid, {"name": name or provider_project, "service_key": name or provider_project})])
            repo.write_relationships([prepare_rel("HAS_SERVICE", "Project", prj, "Service", sid, pid, I.edge_key("HAS_SERVICE", prj, sid))])
            svc_ids[provider_project] = sid
            rep.services += 1
        return svc_ids[provider_project]

    # Services configured in TRACE keep their own key; hosting projects become services on first deployment below.
    for s in b.services:
        sid = I.service_id(pid, s["service_key"])
        repo.write_nodes([prepare_node("Service", sid, pid, {"name": s["name"], "service_key": s["service_key"]})])
        repo.write_relationships([prepare_rel("HAS_SERVICE", "Project", prj, "Service", sid, pid, I.edge_key("HAS_SERVICE", prj, sid))])
        rep.services += 1
    con_ids = _write_connections(repo, b, prj)
    rep.connections = len(con_ids)
    rep.repositories = _write_repositories(repo, b, prj, con_ids)

    dep_info: dict[str, dict] = {}
    for d in b.deployments:
        ek = d["environment_key"] or (d["target"] if d["target"] in env_ids else None)
        if ek not in env_ids or d["created_at_provider"] is None:
            rep.skip("deployment_without_environment_or_time")
            continue
        info = write_deployment_row(repo, pid, d, env_id=env_ids[ek], service_id=service_for(d["provider_project_id"], d["project_name"]), revs=revs)
        dep_info[d["provider_deployment_id"]] = info
        rep.deployments += 1
        rep.deployments_with_revision += 1 if info["revision_id"] else 0
        if not info["revision_id"]:
            rep.skip("deployment_revision_not_indexed")
        if con_ids.get(str(d.get("connection_id"))):
            to = con_ids[str(d["connection_id"])]
            repo.write_relationships([prepare_rel("FROM_SOURCE", "Deployment", info["id"], "SourceConnection", to, pid, I.edge_key("FROM_SOURCE", info["id"], to), {"mapping_method": "provider_api"})])

    ticket_ids = _write_tickets(repo, b, prj, env_ids)
    rep.ticket_nodes = len(ticket_ids)
    for t in b.tickets:
        _ticket_evidence(repo, b, t, env_ids, dep_info, revs, rep, ticket_ids.get(str(t["id"])))
    return rep


def _write_connections(repo: GraphRepository, b: Bundle, prj: str) -> dict[str, str]:
    """Non-secret mirror of each connection (provider, kind, status, capability names); the control DB stays authoritative."""
    pid, out = b.project_id, {}
    for c in b.connections:
        cid = I.entity_id("SourceConnection", pid, str(c["id"]))
        caps = c["capabilities"] if isinstance(c["capabilities"], dict) else {}
        repo.write_nodes([prepare_node("SourceConnection", cid, pid, {
            "connection_id": str(c["id"]), "provider": c["provider"], "kind": c["kind"], "auth_method": c["auth_method"], "status": c["status"],
            "name": f"{c['provider']} ({c['kind']})", "capabilities": sorted(k for k, v in caps.items() if v) or ["none"],
        })])
        repo.write_relationships([prepare_rel("HAS_CONNECTION", "Project", prj, "SourceConnection", cid, pid, I.edge_key("HAS_CONNECTION", prj, cid))])
        out[str(c["id"])] = cid
    return out


def _write_repositories(repo: GraphRepository, b: Bundle, prj: str, con_ids: dict[str, str]) -> int:
    """Bound repositories (same identity the indexer uses) with their connection and the services they implement."""
    pid = b.project_id
    by_binding: dict[str, str] = {}
    for r in b.repositories:
        rid = I.repository_id(pid, r["provider"], r["provider_repository_id"])
        url = f"https://github.com/{r['full_name']}" if r["provider"] == "github" else f"{r['provider']}://{r['full_name']}"
        repo.write_nodes([prepare_node("Repository", rid, pid, {"provider": r["provider"], "provider_repository_id": r["provider_repository_id"], "canonical_url": url})])
        rels = [prepare_rel("HAS_REPOSITORY", "Project", prj, "Repository", rid, pid, I.edge_key("HAS_REPOSITORY", prj, rid, r["provider"]), {"scope_source": r["provider"]})]
        if con_ids.get(str(r["connection_id"])):
            to = con_ids[str(r["connection_id"])]
            rels.append(prepare_rel("FROM_SOURCE", "Repository", rid, "SourceConnection", to, pid, I.edge_key("FROM_SOURCE", rid, to), {"mapping_method": "repository_binding"}))
        repo.write_relationships(rels)
        by_binding[str(r["id"])] = rid
    rels = []
    known = {s["service_key"] for s in b.services}
    for m in b.service_mappings:
        rid = by_binding.get(str(m["repository_binding_id"]))
        if not rid or m["service_key"] not in known:
            continue
        sid = I.service_id(pid, m["service_key"])
        rels.append(prepare_rel("IMPLEMENTS_SERVICE", "Repository", rid, "Service", sid, pid, I.edge_key("IMPLEMENTS_SERVICE", rid, sid, m["path_prefix"]),
                                {"mapping_source": "user_mapping" + (f":{m['path_prefix']}" if m["path_prefix"] else ""), "valid_from": m["valid_from"], "valid_to": m["valid_to"]}))
    repo.write_relationships(rels)
    return len(by_binding)


def _write_tickets(repo: GraphRepository, b: Bundle, prj: str, env_ids: dict[str, str]) -> dict[str, str]:
    """Every ticket of the project, as a report: the environment is linked only as reported (REPORTED_IN), never proven."""
    pid, out, rels = b.project_id, {}, []
    nodes = []
    for t in b.all_tickets:
        tid = I.entity_id("Ticket", pid, str(t["id"]))
        nodes.append(prepare_node("Ticket", tid, pid, {
            "control_ticket_id": str(t["id"]), "title": t["title"], "reported_at": t["reported_at"], "revision_number": t["revision_number"],
            "display_id": f"TKT-{t['number']}", "source": t["source"], "status": t["status"], "severity": t["severity"],
            "environment_key": t["environment_key"], "service_hint": t["service_hint"], "source_id": t["external_id"],
        }))
        rels.append(prepare_rel("HAS_TICKET", "Project", prj, "Ticket", tid, pid, I.edge_key("HAS_TICKET", prj, tid)))
        env = env_ids.get((t["environment_key"] or "").lower())
        if env:
            rels.append(prepare_rel("REPORTED_IN", "Ticket", tid, "Environment", env, pid, I.edge_key("REPORTED_IN", tid, env), {"mapping_method": "as_reported"}))
        out[str(t["id"])] = tid
    repo.write_nodes(nodes)
    repo.write_relationships(rels)
    return out


def _ticket_evidence(repo: GraphRepository, b: Bundle, t: dict, env_ids: dict, dep_info: dict, revs: dict, rep: SyncReport, ticket_node: str | None = None) -> None:
    pid, p = b.project_id, t["payload"]
    target = p.get("target") or {}
    # The environment of evidence is the environment its deployment ran in; PulseLab's own label is only a fallback.
    env = (dep_info.get(target.get("deployment_id")) or {}).get("environment_id") or env_ids.get((target.get("environment") or "").lower())
    if not env:
        rep.skip("ticket_environment_not_in_project")
        return
    conn_id = f"{SOURCE}:{p.get('ticket', {}).get('run_id') or t['id']}"
    now = datetime.now(UTC)
    nodes: list[NodeWrite] = []
    rels: list[RelWrite] = []
    located_cache: dict[tuple[str, str], list[dict]] = {}

    events = p.get("backend_events", [])[:MAX_EVENTS]
    for item in events:
        e = item["event"]
        dep = dep_info.get(e.get("deployment_id"))
        if not dep:
            rep.skip("event_deployment_unknown")
            continue
        key = e["event_id"]
        eid = I.evidence_id(pid, conn_id, key)
        text = f"{e['message']} [{e.get('component_id')}/{e.get('operation')}]" + (f" duration={e['duration_ms']}ms" if e.get("duration_ms") is not None else "")
        nodes.append(prepare_node("LogEvent", eid, pid, {
            "source_connection_id": conn_id, "source_record_key": key, "ingested_at": now, "origin": "telemetry", "content_hash": I.content_hash(json.dumps(e, sort_keys=True)),
            "redacted_content": text, "severity": (e.get("level") or "INFO").lower(), "message": e["message"], "event_time": _t(e.get("event_time")),
            "request_id": e.get("request_id"), "environment_id": env, "service_id": dep["service_id"], "origin_detail": item.get("origin"),
            "source_locator": f"{e.get('component_id')}/{e.get('operation')}/{e.get('event_name')}",
        }, now=now))
        rep.log_events += 1
        for typ, target_label, to, props in (
            ("OBSERVED_ON", "Service", dep["service_id"], {"mapping_method": "deployment_metadata"}),
            ("IN_ENVIRONMENT", "Environment", env, {"mapping_method": "source_scope"}),
            ("ASSOCIATED_WITH", "Deployment", dep["id"], {"mapping_method": "event_deployment_id"}),
        ):
            rels.append(prepare_rel(typ, "LogEvent", eid, target_label, to, pid, I.edge_key(typ, eid, to), props))
        # An operation name locates code only when it is unambiguous at the deployed revision.
        if dep["revision_id"] and e.get("event_name") == "operation.started" and e.get("operation"):
            ck = (dep["revision_id"], e["operation"])
            if ck not in located_cache:
                located_cache[ck] = symbols_named(repo, pid, dep["revision_id"], e["operation"])
            hits = located_cache[ck]
            if len(hits) == 1:
                man = _manifest(repo, b, dep["revision_id"])
                rels.append(prepare_rel("LOCATES", "LogEvent", eid, "CodeSymbol", hits[0]["id"], pid, I.edge_key("LOCATES", eid, hits[0]["id"]),
                                        {"mapping_method": "operation_name", "certainty": "name_match", "manifest_id": man}))
                rep.located += 1
            else:
                rep.skip("operation_not_uniquely_located")

    # What the client side observed: one RequestSpan per request, and the run's counts as Measurements.
    for r in p.get("requests", []):
        dep = next(iter(dep_info.values()), None)
        backend = next((i["event"] for i in events if i["event"].get("request_id") == r.get("backend_request_id")), None)
        dep = dep_info.get((backend or {}).get("deployment_id")) or dep
        if not dep:
            continue
        key = r["observation_id"]
        sid = I.evidence_id(pid, conn_id, f"span:{key}")
        body = json.dumps(r.get("request_body"), sort_keys=True)
        text = f"{r['method']} {r['path']} -> HTTP {r.get('http_status')} ({r.get('outcome')}) in {r.get('duration_ms')}ms; request {body}"
        nodes.append(prepare_node("RequestSpan", sid, pid, {
            "source_connection_id": conn_id, "source_record_key": f"span:{key}", "ingested_at": now, "origin": "telemetry", "content_hash": I.content_hash(text), "redacted_content": text,
            "trace_id": r.get("backend_request_id") or key, "span_id": key, "operation": f"{r['method']} {r['path']}", "status": str(r.get("http_status") or r.get("outcome")),
            "event_time": _t(r.get("started_at")), "duration": f"{r.get('duration_ms')}ms", "environment_id": env, "service_id": dep["service_id"], "origin_detail": r.get("origin"),
        }, now=now))
        for typ, label, to, props in (("OBSERVED_ON", "Service", dep["service_id"], {"mapping_method": "deployment_metadata"}), ("IN_ENVIRONMENT", "Environment", env, {"mapping_method": "source_scope"}),
                                      ("ASSOCIATED_WITH", "Deployment", dep["id"], {"mapping_method": "backend_request_id"})):
            rels.append(prepare_rel(typ, "RequestSpan", sid, label, to, pid, I.edge_key(typ, sid, to), props))
        rep.requests += 1

    summary = (p.get("ticket") or {}).get("observed_counts") or {}
    run_dep = dep_info.get(target.get("deployment_id")) or next(iter(dep_info.values()), None)
    ws, we = _t((p.get("ticket") or {}).get("started_at")), _t((p.get("ticket") or {}).get("ended_at"))
    if run_dep and ws and we:
        for metric, value in summary.items():
            if not value:
                continue
            mid = I.evidence_id(pid, conn_id, f"count:{metric}")
            text = f"{metric.replace('_', ' ')} = {value} over the PulseLab run {ws.isoformat()} to {we.isoformat()}"
            nodes.append(prepare_node("Measurement", mid, pid, {
                "source_connection_id": conn_id, "source_record_key": f"count:{metric}", "ingested_at": now, "origin": "telemetry", "content_hash": I.content_hash(text), "redacted_content": text,
                "metric": metric, "unit": "requests", "value": float(value), "aggregation": "count over the run window", "window_start": ws, "window_end": we, "event_time": we,
                "environment_id": env, "service_id": run_dep["service_id"], "origin_detail": "pulselab",
            }, now=now))
            for typ, label, to, props in (("OBSERVED_ON", "Service", run_dep["service_id"], {"mapping_method": "deployment_metadata"}), ("IN_ENVIRONMENT", "Environment", env, {"mapping_method": "source_scope"}),
                                          ("ASSOCIATED_WITH", "Deployment", run_dep["id"], {"mapping_method": "target_deployment_id"})):
                rels.append(prepare_rel(typ, "Measurement", mid, label, to, pid, I.edge_key(typ, mid, to), props))
            rep.measurements += 1

    if ticket_node:
        rels.extend(prepare_rel("HAS_EVIDENCE", "Ticket", ticket_node, n.label, n.id, pid, I.edge_key("HAS_EVIDENCE", ticket_node, n.id), {"mapping_method": "ticket_export"}) for n in nodes)
    repo.write_nodes(nodes)
    repo.write_relationships(rels)
    rep.tickets.append(str(t["id"]))
