"""Writers for operational evidence: scope (environments, services), deployments, commits, log events and tickets.

Each writer goes through the ontology-validated repository, so this is the same path a production ingestion source
uses. Log events are linked to code by their stack frames: the deployment live at the event time gives the revision,
and the frame's file and line select the innermost symbol of that revision (`LOCATES`, mapping_method=stack_frame).
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.graph import identity as I
from app.graph.repository import GraphRepository, NodeWrite, RelWrite, prepare_node, prepare_rel


def _rel(rels: list[RelWrite], project_id: str, t: str, fl: str, f: str, tl: str, to: str, **props) -> None:
    rels.append(prepare_rel(t, fl, f, tl, to, project_id, I.edge_key(t, f, to), props))


def write_scope(repo: GraphRepository, project_id: str, *, environment_key: str, environment_name: str, service_key: str, service_name: str,
                repository_id: str | None = None) -> tuple[str, str]:
    """Environment + Service under the project; the repository implements the service (declared in project config)."""
    prj = I.project_node_id(project_id)
    env = I.environment_id(project_id, environment_key)
    svc = I.service_id(project_id, service_key)
    repo.write_nodes([
        prepare_node("Environment", env, project_id, {"name": environment_name, "environment_key": environment_key}),
        prepare_node("Service", svc, project_id, {"name": service_name, "service_key": service_key}),
    ])
    rels: list[RelWrite] = []
    _rel(rels, project_id, "HAS_ENVIRONMENT", "Project", prj, "Environment", env)
    _rel(rels, project_id, "HAS_SERVICE", "Project", prj, "Service", svc)
    if repository_id:
        _rel(rels, project_id, "IMPLEMENTS_SERVICE", "Repository", repository_id, "Service", svc, mapping_source="project_config")
    repo.write_relationships(rels)
    return env, svc


def write_deployment(repo: GraphRepository, project_id: str, *, provider_id: str, service_id: str, environment_id: str, revision_id: str | None,
                     event_time: datetime, status: str = "ready", exposure_status: str = "full") -> str:
    dep = I.entity_id("Deployment", project_id, provider_id)
    repo.write_nodes([prepare_node("Deployment", dep, project_id, {
        "service_id": service_id, "environment_id": environment_id, "provider_id": provider_id, "status": status,
        "event_time": event_time, "exposure_status": exposure_status, "revision_id": revision_id,
    })])
    rels: list[RelWrite] = []
    _rel(rels, project_id, "DEPLOYS_SERVICE", "Deployment", dep, "Service", service_id)
    _rel(rels, project_id, "IN_ENVIRONMENT", "Deployment", dep, "Environment", environment_id, mapping_method="provider_metadata")
    if revision_id:
        _rel(rels, project_id, "DEPLOYS_REVISION", "Deployment", dep, "Revision", revision_id, mapping_method="provider_commit_sha")
    repo.write_relationships(rels)
    return dep


# ---- commits ------------------------------------------------------------------------------------------------------
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def diff_ranges(root: str, before_sha: str, after_sha: str) -> dict[str, dict[str, list[tuple[int, int]]]]:
    """path -> {"before": [(start, end)], "after": [(start, end)]} of changed lines, from `git diff -U0`."""
    out = subprocess.run(["git", "-C", root, "diff", "-U0", "--no-color", before_sha, after_sha], capture_output=True, text=True, check=True).stdout
    ranges: dict[str, dict[str, list[tuple[int, int]]]] = {}
    path = None
    for line in out.splitlines():
        if line.startswith("+++ "):
            path = None if line.endswith("/dev/null") else line[6:]
            if path:
                ranges.setdefault(path, {"before": [], "after": []})
        elif path and (m := _HUNK.match(line)):
            bs, bc, as_, ac = int(m[1]), int(m[2] or 1), int(m[3]), int(m[4] or 1)
            if bc:
                ranges[path]["before"].append((bs, bs + bc - 1))
            ranges[path]["after"].append((as_, as_ + max(ac, 1) - 1))
    return ranges


def _symbols_overlapping(repo: GraphRepository, project_id: str, revision_id: str, path: str, spans: list[tuple[int, int]]) -> list[str]:
    if not spans:
        return []
    with repo._driver.session(database=repo._db) as s:
        rows = s.run(
            "MATCH (f:Entity:SourceFile {project_id: $p, revision_id: $r, path: $path})-[:DEFINES]->(c:Entity:CodeSymbol {project_id: $p}) "
            "WHERE any(sp IN $spans WHERE c.start_line <= sp[1] AND c.end_line >= sp[0]) RETURN DISTINCT c.id AS id",
            p=project_id, r=revision_id, path=path, spans=[list(x) for x in spans],
        )
        return [r["id"] for r in rows]


def revision_manifest(repo: GraphRepository, project_id: str, revision_id: str) -> str | None:
    with repo._driver.session(database=repo._db) as s:
        row = s.run(
            "MATCH (m:Entity:RevisionManifest {project_id: $p})-[:SELECTS_BUILD]->(b:Entity:GraphBuild {project_id: $p, revision_id: $r}) "
            "RETURN m.id AS id ORDER BY b.created_at DESC LIMIT 1", p=project_id, r=revision_id,
        ).single()
        return row["id"] if row else None


def write_commit_change(repo: GraphRepository, project_id: str, *, root: str, repository_id: str, before_rev: str, after_rev: str,
                        before_sha: str, after_sha: str, event_time: datetime) -> tuple[str, list[str]]:
    """A Change for one commit, MODIFIES-linked to the symbols whose lines the diff touched at both revisions."""
    chg = I.entity_id("Change", project_id, "commit", after_sha)
    repo.write_nodes([prepare_node("Change", chg, project_id, {"source_id": after_sha, "kind": "commit", "event_time": event_time, "before_ref": before_sha, "after_ref": after_sha})])
    rels: list[RelWrite] = []
    _rel(rels, project_id, "BEFORE_REVISION", "Change", chg, "Revision", before_rev, repository_id=repository_id)
    _rel(rels, project_id, "AFTER_REVISION", "Change", chg, "Revision", after_rev, repository_id=repository_id)
    touched: list[str] = []
    for path, sides in diff_ranges(root, before_sha, after_sha).items():
        for role, rev in (("before", before_rev), ("after", after_rev)):
            man = revision_manifest(repo, project_id, rev)
            if sides[role]:  # the file itself, so module-level edits (constants, tables) are not lost
                fid = I.source_file_id(project_id, rev, path)
                rels.append(prepare_rel("MODIFIES", "Change", chg, "SourceFile", fid, project_id, I.edge_key("MODIFIES", chg, fid, role), {"before_after_role": role, "manifest_id": man or ""}))
            for sym in _symbols_overlapping(repo, project_id, rev, path, sides[role]):
                rels.append(prepare_rel("MODIFIES", "Change", chg, "CodeSymbol", sym, project_id, I.edge_key("MODIFIES", chg, sym, role), {"before_after_role": role, "manifest_id": man or ""}))
                if role == "after":
                    touched.append(sym)
    repo.write_relationships(rels)
    return chg, touched


# ---- log events ---------------------------------------------------------------------------------------------------
@dataclass
class LogInput:
    record_key: str
    event_time: datetime | None
    severity: str
    message: str
    request_id: str | None = None
    route: str | None = None
    stack_frames: list[dict] = field(default_factory=list)  # [{"file", "line", "function"}], innermost last


def _live_deployment(s, project_id: str, service_id: str, environment_id: str, at: datetime):
    return s.run(
        "MATCH (d:Entity:Deployment {project_id: $p, service_id: $s, environment_id: $e}) WHERE d.event_time <= $t AND d.status = 'ready' "
        "RETURN d.id AS id, d.revision_id AS revision_id ORDER BY d.event_time DESC LIMIT 1", p=project_id, s=service_id, e=environment_id, t=at,
    ).single()


def _innermost_symbol(s, project_id: str, revision_id: str, path: str, line: int):
    return s.run(
        "MATCH (f:Entity:SourceFile {project_id: $p, revision_id: $r, path: $path})-[:DEFINES]->(c:Entity:CodeSymbol {project_id: $p}) "
        "WHERE c.start_line <= $line AND c.end_line >= $line RETURN c.id AS id, c.qualified_name AS name ORDER BY c.end_line - c.start_line LIMIT 1",
        p=project_id, r=revision_id, path=path, line=line,
    ).single()


def write_log_events(repo: GraphRepository, project_id: str, *, source_connection_id: str, service_id: str, environment_id: str,
                     events: list[LogInput]) -> dict[str, int]:
    """Immutable LogEvents scoped to a service and environment, associated with the live deployment and located in code."""
    now = datetime.now(UTC)
    nodes: list[NodeWrite] = []
    rels: list[RelWrite] = []
    stats = {"events": 0, "located": 0, "unlocated_frames": 0}
    with repo._driver.session(database=repo._db) as s:
        for ev in events:
            eid = I.evidence_id(project_id, source_connection_id, ev.record_key)
            text = ev.message + "".join(f"\n  at {f['function']} ({f['file']}:{f['line']})" for f in ev.stack_frames)
            nodes.append(prepare_node("LogEvent", eid, project_id, {
                "source_connection_id": source_connection_id, "source_record_key": ev.record_key, "ingested_at": now, "origin": "telemetry",
                "content_hash": I.content_hash(text), "redacted_content": text, "severity": ev.severity, "message": ev.message,
                "event_time": ev.event_time, "request_id": ev.request_id, "stack_frames_json": json.dumps(ev.stack_frames) if ev.stack_frames else None,
                "environment_id": environment_id, "service_id": service_id, "source_locator": ev.route,
            }, now=now))
            stats["events"] += 1
            _rel(rels, project_id, "OBSERVED_ON", "LogEvent", eid, "Service", service_id, mapping_method="source_scope")
            _rel(rels, project_id, "IN_ENVIRONMENT", "LogEvent", eid, "Environment", environment_id, mapping_method="source_scope")
            live = _live_deployment(s, project_id, service_id, environment_id, ev.event_time) if ev.event_time else None
            if not live:
                continue
            _rel(rels, project_id, "ASSOCIATED_WITH", "LogEvent", eid, "Deployment", live["id"], mapping_method="live_at_event_time")
            if not live["revision_id"] or not ev.stack_frames:
                continue
            man = revision_manifest(repo, project_id, live["revision_id"]) or ""
            for depth, fr in enumerate(reversed(ev.stack_frames)):
                sym = _innermost_symbol(s, project_id, live["revision_id"], fr["file"], int(fr["line"]))
                if not sym:
                    stats["unlocated_frames"] += 1
                    continue
                certainty = "exact" if sym["name"].split(".")[-1] == fr.get("function") else "line_only"
                rels.append(prepare_rel("LOCATES", "LogEvent", eid, "CodeSymbol", sym["id"], project_id, I.edge_key("LOCATES", eid, sym["id"], depth),
                                        {"mapping_method": "stack_frame", "certainty": certainty, "manifest_id": man}))
                stats["located"] += 1
    repo.write_nodes(nodes)
    repo.write_relationships(rels)
    return stats


def write_ticket(repo: GraphRepository, project_id: str, *, control_ticket_id: str, title: str, reported_at: datetime, revision_number: int) -> str:
    tid = I.entity_id("Ticket", project_id, control_ticket_id)
    repo.write_nodes([prepare_node("Ticket", tid, project_id, {"control_ticket_id": control_ticket_id, "title": title, "reported_at": reported_at, "revision_number": revision_number})])
    return tid
