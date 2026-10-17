"""Project accepted evidence rows (Postgres `evidence_inbox`) and ingestion gaps into the evidence graph (T-009, architecture A2/A10).

This module is the Neo4j half and is synchronous (run it in a worker thread). The Postgres half (claiming, leases, acknowledgement)
is `app/domain/ingest/graph_apply.py`; a row is acknowledged only after `project_evidence` returned, i.e. after Neo4j committed.

Rules this file keeps:
- Ids are deterministic and project-scoped (`I.evidence_id(project, source connection, provider record key)`), and LogEvent is
  immutable, so writing the same row twice (crash after the graph commit, lease takeover) is a no-op and never a conflict.
- Everything a node says that could differ between attempts (service, deployment) comes from `graph_scope`, decided once in Postgres.
- Scope is never guessed: environment from the row (NULL stays unknown, no IN_ENVIRONMENT edge), service only when mapped by a
  deployment, the Log scope or the source's declared service, a Deployment only when TRACE has a deployment row for it, and a revision
  only when that deployment's commit is an indexed revision (same writer as the on-demand sync).
- `event_time` (when it happened) and `ingested_at` (when TRACE received it) stay separate; `created_at` is when this node was written.
  Late evidence is simply another immutable node: queries filter by `ingested_at <= cutoff`, so earlier assessments are never rewritten.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.graph import identity as I
from app.graph.evidence import revision_manifest
from app.graph.repository import GraphRepository, NodeWrite, RelWrite, prepare_node, prepare_rel
from app.graph.sync import revisions_by_sha, symbols_named, write_deployment_row


@dataclass
class EvidenceBundle:
    project_id: str
    organization_id: str
    project_name: str
    environments: dict[str, dict]            # environment uuid (str) -> {environment_key, name}
    rows: list[dict]                         # evidence_inbox rows, graph_scope already resolved
    deployments: dict[str, dict] = field(default_factory=dict)   # provider_deployment_id -> deployments row
    gaps: list[dict] = field(default_factory=list)               # ingestion_batches rows that carry a coverage gap

    def subset(self, rows: list[dict]) -> EvidenceBundle:
        return EvidenceBundle(self.project_id, self.organization_id, self.project_name, self.environments, rows, self.deployments, [])


@dataclass
class ApplyReport:
    events: int = 0
    in_environment: int = 0
    observed_on_service: int = 0
    associated_with_deployment: int = 0
    deployments_with_revision: int = 0
    located: int = 0
    unknown_environment: int = 0
    unknown_service: int = 0
    unknown_deployment: int = 0
    gaps: int = 0

    def add(self, other: ApplyReport) -> None:
        for k in self.__dataclass_fields__:
            setattr(self, k, getattr(self, k) + getattr(other, k))


def source_connection(row: dict) -> str:
    """The provenance id an evidence node is keyed on: the connection that delivered it, or the ticket whose export carried it."""
    return str(row["connection_id"]) if row.get("connection_id") else f"ticket:{row['ticket_id']}"


def _origin_detail(row: dict) -> str:
    base = row["provider"] + (f"/{row['source']}" if row.get("source") else "")
    if row.get("source") == "browser":
        base += " (untrusted browser observation)"
    return base + (f" deployment={row['deployment_id']}" if row.get("deployment_id") else "")


def _content(row: dict) -> str:
    text = row.get("message") or "(empty message)"
    if row.get("status_code") is not None:
        text += f"\nhttp_status={row['status_code']}"
    if (row.get("extra") or {}).get("message_truncated"):
        text += "\n[message truncated at the size cap]"
    return text


def project_evidence(repo: GraphRepository, b: EvidenceBundle) -> ApplyReport:
    rep = ApplyReport()
    if b.rows:
        _project_rows(repo, b, rep)
    if b.gaps:
        _project_gaps(repo, b, rep)
    return rep


def _deployment_env_key(b: EvidenceBundle, d: dict) -> str | None:
    env = b.environments.get(str(d["environment_id"])) if d.get("environment_id") else None
    if env:
        return env["environment_key"]
    keys = {e["environment_key"] for e in b.environments.values()}
    return d["target"] if d.get("target") in keys else None


def _project_rows(repo: GraphRepository, b: EvidenceBundle, rep: ApplyReport) -> None:
    pid = b.project_id
    prj = I.project_node_id(pid)
    nodes: list[NodeWrite] = [prepare_node("Project", prj, pid, {"organization_id": b.organization_id, "name": b.project_name})]
    rels: list[RelWrite] = []

    def rel(t: str, fl: str, f: str, tl: str, to: str, **props) -> None:
        rels.append(prepare_rel(t, fl, f, tl, to, pid, I.edge_key(t, f, to), props))

    env_nodes: dict[str, str] = {}       # environment uuid -> graph id (only environments actually referenced)

    def env_node(env_uuid: Any) -> str | None:
        e = b.environments.get(str(env_uuid)) if env_uuid else None
        if e is None:
            return None
        eid = env_nodes.get(str(env_uuid))
        if eid is None:
            eid = env_nodes[str(env_uuid)] = I.environment_id(pid, e["environment_key"])
            nodes.append(prepare_node("Environment", eid, pid, {"name": e["name"], "environment_key": e["environment_key"]}))
            rel("HAS_ENVIRONMENT", "Project", prj, "Environment", eid)
        return eid

    service_nodes: dict[str, str] = {}

    def service_node(key: str, name: str | None) -> str:
        sid = service_nodes.get(key)
        if sid is None:
            sid = service_nodes[key] = I.service_id(pid, key)
            nodes.append(prepare_node("Service", sid, pid, {"name": name or key, "service_key": key}))
            rel("HAS_SERVICE", "Project", prj, "Service", sid)
        return sid

    # Deployments the rows refer to, and only those TRACE has a row for. Written first so the evidence edges have an endpoint.
    revs = revisions_by_sha(repo, pid) if any((r["graph_scope"] or {}).get("deployment") for r in b.rows) else {}
    dep_info: dict[str, dict] = {}       # provider deployment id -> written deployment ({} = known to TRACE but not writable: no environment or time)
    to_write: list[tuple[dict, str, str]] = []
    for r in b.rows:
        dpid = (r["graph_scope"] or {}).get("deployment")
        if not dpid or dpid in dep_info or dpid not in b.deployments:
            continue
        d = b.deployments[dpid]
        ek = _deployment_env_key(b, d)
        env_uuid = next((u for u, e in b.environments.items() if e["environment_key"] == ek), None)
        dep_info[dpid] = {}
        if env_uuid is None or d.get("created_at_provider") is None:
            continue
        env_node(env_uuid)
        to_write.append((d, I.environment_id(pid, ek), service_node(d["project_name"] or d["provider_project_id"], d["project_name"])))
    repo.write_nodes(nodes)
    repo.write_relationships(rels)
    nodes, rels = [], []
    for d, env_id, sid in to_write:
        info = write_deployment_row(repo, pid, d, env_id=env_id, service_id=sid, revs=revs)
        dep_info[d["provider_deployment_id"]] = info
        rep.deployments_with_revision += 1 if info["revision_id"] else 0

    located_cache: dict[tuple[str, str], list[dict]] = {}
    manifest_cache: dict[str, str | None] = {}
    for r in b.rows:
        sc = r["graph_scope"] or {}
        browser_observation = r.get("source") == "browser"
        # Browser supplied observations are retained in the graph with provenance, but are not attached
        # to backend service, environment, deployment, or source-code evidence relationships.
        env_id = None if browser_observation else env_node(r["environment_id"])
        svc_id = None if browser_observation else service_node(sc["service_key"], sc.get("service_name")) if sc.get("service_key") else None
        eid = I.evidence_id(pid, source_connection(r), r["source_record_key"])
        nodes.append(prepare_node("LogEvent", eid, pid, {
            "source_connection_id": source_connection(r), "source_record_key": r["source_record_key"], "ingested_at": r["ingested_at"], "origin": "telemetry",
            "content_hash": r["content_hash"], "redacted_content": _content(r), "severity": r["level"] or "unknown", "message": r["message"] or "(empty message)",
            "event_time": r["event_time"], "request_id": r["request_id"], "environment_id": env_id, "service_id": svc_id,
            "origin_detail": _origin_detail(r), "source_locator": r["service_hint"],
        }))
        rep.events += 1
        if env_id:
            rel("IN_ENVIRONMENT", "LogEvent", eid, "Environment", env_id, mapping_method="source_scope")
            rep.in_environment += 1
        else:
            rep.unknown_environment += 1
        if svc_id:
            rel("OBSERVED_ON", "LogEvent", eid, "Service", svc_id, mapping_method={"deployment_metadata": "deployment_metadata", "log_scope": "source_scope", "source_scope": "source_scope"}[sc["service_via"]])
            rep.observed_on_service += 1
        else:
            rep.unknown_service += 1
        dep = None if browser_observation else dep_info.get(sc.get("deployment") or "")
        if dep and dep.get("id"):
            rel("ASSOCIATED_WITH", "LogEvent", eid, "Deployment", dep["id"], mapping_method="event_deployment_id")
            rep.associated_with_deployment += 1
            # An operation name locates code only when it is unambiguous at the deployed (indexed) revision.
            ex = r.get("extra") or {}
            if dep["revision_id"] and ex.get("event_name") == "operation.started" and ex.get("operation"):
                if dep["revision_id"] not in manifest_cache:
                    manifest_cache[dep["revision_id"]] = revision_manifest(repo, pid, dep["revision_id"])
                ck = (dep["revision_id"], ex["operation"])
                if ck not in located_cache:
                    located_cache[ck] = symbols_named(repo, pid, dep["revision_id"], ex["operation"])
                hits = located_cache[ck]
                if len(hits) == 1 and manifest_cache[dep["revision_id"]]:
                    rels.append(prepare_rel("LOCATES", "LogEvent", eid, "CodeSymbol", hits[0]["id"], pid, I.edge_key("LOCATES", eid, hits[0]["id"]),
                                            {"mapping_method": "operation_name", "certainty": "name_match", "manifest_id": manifest_cache[dep["revision_id"]]}))
                    rep.located += 1
        else:
            rep.unknown_deployment += 1
    repo.write_nodes(nodes)       # Environment and Service nodes added while walking the rows are in `nodes` before the events
    repo.write_relationships(rels)


def _gap(kind: str, description: str, batch: dict) -> tuple[str, str, dict]:
    return kind, description, {"batch_id": str(batch["id"]), "connection_id": str(batch["connection_id"]) if batch.get("connection_id") else None, "via": batch.get("via"), "received": batch["received"]}


def gap_specs(batch: dict) -> list[tuple[str, str, dict]]:
    """Coverage gaps one intake request left behind. Descriptions carry counts only, never record content."""
    out = []
    reason = batch.get("reason")
    if reason == "oversize_body":
        out.append(_gap("oversize_input", f"A signed request of {batch.get('body_bytes')} bytes exceeded the intake size limit and was refused; none of its events were stored.", batch))
    elif reason == "batch_too_large":
        out.append(_gap("rejected_input", "A signed request carried more events than one batch may hold and was refused whole; none of its events were stored.", batch))
    elif reason:
        out.append(_gap("rejected_input", "A signed request was not valid JSON or NDJSON and was refused whole; none of its events were stored.", batch))
    if batch["rejected"]:
        out.append(_gap("rejected_input", f"{batch['rejected']} of {batch['received']} record(s) were malformed (no usable id, message or project) and were not stored.", batch))
    if batch["out_of_scope"]:
        out.append(_gap("unmapped_scope", f"{batch['out_of_scope']} record(s) belong to a hosting project outside this project's Log scope and were not stored.", batch))
    if batch["unmapped"]:
        out.append(_gap("unmapped_scope", f"{batch['unmapped']} stored record(s) have an environment TRACE could not map; their environment is unknown.", batch))
    if batch["truncated"]:
        out.append(_gap("truncated_input", f"{batch['truncated']} message(s) were cut at the size cap; the stored text is partial.", batch))
    return out


def _project_gaps(repo: GraphRepository, b: EvidenceBundle, rep: ApplyReport) -> None:
    pid = b.project_id
    nodes: list[NodeWrite] = []
    for batch in b.gaps:
        for kind, description, scope in gap_specs(batch):
            gid = I.entity_id("EvidenceGap", pid, "ingest", str(batch["id"]), kind, description)
            nodes.append(prepare_node("EvidenceGap", gid, pid, {
                "kind": kind, "description": description, "scope_json": I.canonical_json(scope), "detected_at": batch["created_at"], "status": "open",
            }))
    if nodes:
        repo.write_nodes(nodes)
        rep.gaps += len(nodes)
