"""Read-only graph tools for the TRACE Bot. The evidence graph (Neo4j) is the primary source: every tool is a fixed,
parameterized Cypher template that carries the caller's project ids, returns bounded rows, and exposes node ids so answers
can cite them. The model never writes Cypher. Postgres is not touched here; project access is decided by the API layer."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.graph import ontology as O
from app.graph.queries import escape_lucene

MAX_ROWS = 25
MAX_TEXT = 700
# Browser observations are retained for provenance and diagnostics, but are not investigator evidence.
TRUSTED_OBSERVATION = "NOT (coalesce(e.origin_detail, '') CONTAINS '(untrusted browser observation)')"


def _v(x: Any) -> Any:
    if hasattr(x, "to_native"):
        x = x.to_native()
    if isinstance(x, datetime):
        return x.isoformat()
    if isinstance(x, str) and len(x) > MAX_TEXT:
        return x[:MAX_TEXT] + f"… [{len(x) - MAX_TEXT} more characters]"
    if isinstance(x, list):
        return [_v(i) for i in x[:30]]
    return x


def _clean(d: dict) -> dict:
    return {k: _v(v) for k, v in d.items() if v is not None and not k.startswith("_") and k not in ("schema_version", "created_at", "content_hash")}


def _lim(n: Any, default: int = 15) -> int:
    try:
        return max(1, min(int(n), MAX_ROWS))
    except (TypeError, ValueError):
        return default


def _primary(labels: list[str]) -> str:
    rest = [x for x in labels if x not in ("Entity", "EvidenceRecord")]
    return rest[0] if rest else (labels[0] if labels else "Entity")


class GraphTools:
    """One instance per request: the driver, the database and the project ids the caller may read."""

    def __init__(self, driver, database: str, project_ids: list[str]):
        self.driver, self.db, self.pids = driver, database, project_ids
        self.seen: dict[str, str] = {}  # node id -> short caption of everything returned to the model (the citation universe)

    def _run(self, cypher: str, /, **p) -> list[dict]:
        with self.driver.session(database=self.db) as s:
            return [r.data() for r in s.run(cypher, parameters={"pids": self.pids, **p})]

    def _note(self, rows: list[dict], key: str = "id", cap: str = "caption") -> list[dict]:
        for r in rows:
            if r.get(key):
                self.seen[r[key]] = str(r.get(cap) or r[key])[:140]
        return rows

    # ---- orientation ------------------------------------------------------------------------------------------------
    def graph_overview(self) -> dict:
        counts = self._run("MATCH (n:Entity) WHERE n.project_id IN $pids AND (NOT n:LogEvent OR NOT (coalesce(n.origin_detail, '') CONTAINS '(untrusted browser observation)')) "
                           "RETURN coalesce([l IN labels(n) WHERE NOT l IN ['Entity','EvidenceRecord']][0], 'EvidenceRecord') AS label, count(*) AS n ORDER BY n DESC")
        ev = self._run(f"MATCH (e:Entity:LogEvent) WHERE e.project_id IN $pids AND {TRUSTED_OBSERVATION} RETURN min(e.event_time) AS first, max(e.event_time) AS last, "
                       "sum(CASE WHEN e.severity IN ['error','fatal','critical'] THEN 1 ELSE 0 END) AS errors, count(*) AS total")
        svc = self._run("MATCH (s:Entity:Service) WHERE s.project_id IN $pids RETURN s.id AS id, s.name AS caption, s.project_id AS project")
        env = self._run("MATCH (s:Entity:Environment) WHERE s.project_id IN $pids RETURN s.id AS id, s.name AS caption")
        self._note(svc), self._note(env)
        return {"counts": counts, "log_events": _clean(ev[0]) if ev else {}, "services": svc, "environments": env}

    # ---- find things --------------------------------------------------------------------------------------------------
    def search(self, query: str, label: str | None = None, limit: int = 12) -> dict:
        q = escape_lucene(query or "")
        if not q:
            return {"results": []}
        rows = self._run(
            "CALL db.index.fulltext.queryNodes('entity_caption', $q, {limit: 200}) YIELD node, score WHERE node.project_id IN $pids "
            "AND ($label IS NULL OR $label IN labels(node)) AND (NOT node:LogEvent OR NOT (coalesce(node.origin_detail, '') CONTAINS '(untrusted browser observation)')) "
            "RETURN node.id AS id, labels(node) AS labels, coalesce(node.name, node.title, node.qualified_name, node.path, node.statement, node.summary, "
            "node.message, node.operation, node.display_id) AS caption, node.severity AS severity, node.event_time AS event_time, node.project_id AS project, score "
            "ORDER BY score DESC LIMIT $limit", q=q, label=label if label in O.NODES else None, limit=_lim(limit, 12))
        out = [{"id": r["id"], "type": _primary(r["labels"]), "caption": _v(r["caption"]), "severity": r["severity"], "event_time": _v(r["event_time"])} for r in rows]
        return {"results": self._note(out)}

    def get_node(self, id: str, depth: int = 1) -> dict:
        n = self._run("MATCH (n:Entity {id: $id}) WHERE n.project_id IN $pids AND (NOT n:LogEvent OR NOT (coalesce(n.origin_detail, '') CONTAINS '(untrusted browser observation)')) RETURN n, labels(n) AS labels", id=id)
        if not n:
            return {"error": "No node with that id in the projects you can read."}
        node = {**_clean(dict(n[0]["n"])), "id": id, "type": _primary(n[0]["labels"])}
        hops = 2 if depth == 2 else 1
        rels = self._run(
            f"MATCH (n:Entity {{id: $id}})-[r*1..{hops}]-(m:Entity) WHERE n.project_id IN $pids AND m.project_id = n.project_id "
            "AND (NOT m:LogEvent OR NOT (coalesce(m.origin_detail, '') CONTAINS '(untrusted browser observation)')) "
            "WITH r[-1] AS last, m, size(r) AS hop "
            "RETURN type(last) AS rel, startNode(last).id = m.id AS incoming, hop, m.id AS id, labels(m) AS labels, "
            "coalesce(m.name, m.title, m.qualified_name, m.path, m.statement, m.summary, m.message, m.display_id, m.operation) AS caption, m.severity AS severity "
            "ORDER BY hop, rel LIMIT 60", id=id)
        edges = [{"rel": r["rel"], "dir": "in" if r["incoming"] else "out", "hop": r["hop"], "id": r["id"], "type": _primary(r["labels"]),
                  "caption": _v(r["caption"]), "severity": r["severity"]} for r in rels]
        self._note([{"id": id, "caption": node.get("title") or node.get("name") or node.get("message") or node.get("summary") or id}])
        self._note(edges)
        return {"node": node, "connections": edges}

    # ---- incidents ----------------------------------------------------------------------------------------------------
    def list_tickets(self, status: str | None = None, limit: int = 15) -> dict:
        rows = self._run(
            "MATCH (t:Entity:Ticket) WHERE t.project_id IN $pids AND ($status IS NULL OR t.status = $status) "
            "OPTIONAL MATCH (t)-[:HAS_RUN]->(r:Entity:InvestigationRun) "
            "OPTIONAL MATCH (r)-[:PRODUCES]->(f:Entity:Finding) "
            "RETURN t.id AS id, t.display_id AS display_id, t.title AS caption, t.status AS status, t.severity AS severity, t.environment_key AS env, "
            "t.service_hint AS service_hint, t.reported_at AS reported_at, r.id AS run_id, r.status AS run_status, f.summary AS finding "
            "ORDER BY t.reported_at DESC LIMIT $limit", status=status, limit=_lim(limit))
        return {"tickets": self._note([{**r, "reported_at": _v(r["reported_at"]), "finding": _v(r["finding"])} for r in rows])}

    def get_investigation(self, run_id: str) -> dict:
        r = self._run("MATCH (r:Entity:InvestigationRun {id: $id}) WHERE r.project_id IN $pids "
                      "OPTIONAL MATCH (t:Entity:Ticket)-[:HAS_RUN]->(r) OPTIONAL MATCH (r)-[:PRODUCES]->(f:Entity:Finding) "
                      "RETURN r, t.display_id AS ticket, t.title AS title, f.id AS finding_id, f.summary AS summary, f.status AS finding_status", id=run_id)
        if not r:
            return {"error": "No investigation run with that id."}
        hyps = self._run("MATCH (r:Entity:InvestigationRun {id: $id})-[:CONSIDERS]->(h:Entity:Hypothesis) WHERE r.project_id IN $pids "
                         "OPTIONAL MATCH (h)-[:HAS_ASSESSMENT]->(a:Entity:Assessment) RETURN h.id AS id, h.statement AS caption, a.status AS status, a.summary AS why ORDER BY h.created_step", id=run_id)
        claims = self._run("MATCH (r:Entity:InvestigationRun {id: $id})-[:PRODUCES]->(:Entity:Finding)-[:HAS_CLAIM]->(c:Entity:Claim) "
                           "WHERE r.project_id IN $pids OPTIONAL MATCH (c)-[:CITES]->(e:Entity) "
                           "RETURN c.id AS id, c.text AS caption, collect(DISTINCT e.id)[..8] AS cites", id=run_id)
        gaps = self._run("MATCH (r:Entity:InvestigationRun {id: $id})-[:HAS_GAP]->(g:Entity:EvidenceGap) WHERE r.project_id IN $pids "
                         "RETURN g.id AS id, g.kind AS kind, g.description AS caption", id=run_id)
        run = _clean(dict(r[0]["r"]))
        self._note([{"id": run_id, "caption": r[0]["title"]}, {"id": r[0]["finding_id"], "caption": r[0]["summary"]}])
        self._note(hyps), self._note(claims), self._note(gaps)
        return {"run": {"id": run_id, "ticket": r[0]["ticket"], "title": r[0]["title"], "status": run.get("status"), "window_start": run.get("window_start"),
                        "window_end": run.get("window_end")},
                "finding": {"id": r[0]["finding_id"], "status": r[0]["finding_status"], "summary": r[0]["summary"]},
                "hypotheses": hyps, "claims": claims, "gaps": gaps}

    # ---- runtime evidence ---------------------------------------------------------------------------------------------
    def error_summary(self, service: str | None = None, severity: str | None = None) -> dict:
        """Log evidence grouped by service, severity and operation, newest first. Counts come from the graph, not from the model."""
        rows = self._run(
            "MATCH (e:Entity:LogEvent)-[:OBSERVED_ON]->(s:Entity:Service) WHERE e.project_id IN $pids AND s.project_id = e.project_id "
            f"AND {TRUSTED_OBSERVATION} AND ($service IS NULL OR toLower(s.name) CONTAINS toLower($service)) AND ($sev IS NULL OR e.severity = $sev) "
            "RETURN s.name AS service, e.severity AS severity, e.source_locator AS operation, count(*) AS events, "
            "min(e.event_time) AS first, max(e.event_time) AS last, collect(e.id)[..3] AS sample_ids "
            "ORDER BY CASE e.severity WHEN 'fatal' THEN 0 WHEN 'critical' THEN 0 WHEN 'error' THEN 1 WHEN 'warn' THEN 2 WHEN 'warning' THEN 2 ELSE 3 END, events DESC LIMIT 30",
            service=service, sev=severity)
        for r in rows:
            for i in r["sample_ids"]:
                self.seen[i] = f"{r['severity']} {r['operation']}"
            r["first"], r["last"] = _v(r["first"]), _v(r["last"])
        totals = self._run(f"MATCH (e:Entity:LogEvent) WHERE e.project_id IN $pids AND {TRUSTED_OBSERVATION} RETURN e.severity AS severity, count(*) AS events ORDER BY events DESC")
        return {"severity_totals": totals, "groups": rows,
                "note": "Only error/fatal/critical events indicate failures. info events are normal operation."}

    def recent_events(self, severity: str | None = None, text: str | None = None, limit: int = 12) -> dict:
        rows = self._run(
            f"MATCH (e:Entity:LogEvent) WHERE e.project_id IN $pids AND {TRUSTED_OBSERVATION} AND ($sev IS NULL OR e.severity = $sev) "
            "AND ($text IS NULL OR toLower(coalesce(e.redacted_content, e.message)) CONTAINS toLower($text)) "
            "OPTIONAL MATCH (e)-[:OBSERVED_ON]->(s:Entity:Service) OPTIONAL MATCH (e)-[:ASSOCIATED_WITH]->(d:Entity:Deployment) "
            "RETURN e.id AS id, e.severity AS severity, e.event_time AS event_time, e.message AS caption, e.source_locator AS operation, e.request_id AS request_id, "
            "left(e.redacted_content, 400) AS content, s.name AS service, d.provider_id AS deployment ORDER BY e.event_time DESC LIMIT $limit",
            sev=severity, text=text, limit=_lim(limit, 12))
        return {"events": self._note([{**r, "event_time": _v(r["event_time"])} for r in rows])}

    def request_trace(self, request_id: str) -> dict:
        rows = self._run(f"MATCH (e:Entity:LogEvent {{request_id: $rid}}) WHERE e.project_id IN $pids AND {TRUSTED_OBSERVATION} "
                         "RETURN e.id AS id, e.severity AS severity, e.event_time AS event_time, e.message AS caption, left(e.redacted_content, 300) AS content "
                         "ORDER BY e.event_time LIMIT 40", rid=request_id)
        return {"events": self._note([{**r, "event_time": _v(r["event_time"])} for r in rows])}

    # ---- change and code ----------------------------------------------------------------------------------------------
    def deployments(self, limit: int = 10) -> dict:
        rows = self._run(
            "MATCH (d:Entity:Deployment) WHERE d.project_id IN $pids OPTIONAL MATCH (d)-[:DEPLOYS_SERVICE]->(s:Entity:Service) "
            "OPTIONAL MATCH (d)-[:DEPLOYS_REVISION]->(r:Entity:Revision) "
            "RETURN d.id AS id, d.provider_id AS caption, d.status AS status, d.event_time AS event_time, d.exposure_status AS exposure, s.name AS service, "
            "r.commit_sha AS commit_sha, r.id AS revision_id ORDER BY d.event_time DESC LIMIT $limit", limit=_lim(limit, 10))
        return {"deployments": self._note([{**r, "event_time": _v(r["event_time"])} for r in rows])}

    def code_for_event(self, event_id: str) -> dict:
        """Code symbols that an event's stack trace resolves to (LOCATES), with file and lines."""
        rows = self._run(
            f"MATCH (e:Entity {{id: $id}})-[l:LOCATES]->(s:Entity:CodeSymbol)<-[:DEFINES]-(f:Entity:SourceFile) WHERE e.project_id IN $pids AND (NOT e:LogEvent OR {TRUSTED_OBSERVATION}) "
            "RETURN s.id AS id, s.qualified_name AS caption, f.path AS path, s.start_line AS start_line, s.end_line AS end_line, l.certainty AS certainty LIMIT 10", id=event_id)
        return {"code": self._note(rows)}

    def search_code(self, query: str, limit: int = 8) -> dict:
        q = escape_lucene(query or "")
        if not q:
            return {"chunks": []}
        rows = self._run(
            "CALL db.index.fulltext.queryNodes('source_text', $q, {limit: 100}) YIELD node, score WHERE node.project_id IN $pids "
            "MATCH (f:Entity:SourceFile {id: node.file_id}) WHERE f.project_id = node.project_id "
            "RETURN node.id AS id, f.path AS path, node.start_line AS start_line, node.end_line AS end_line, left(node.content, 600) AS content, score "
            "ORDER BY score DESC LIMIT $limit", q=q, limit=_lim(limit, 8))
        for r in rows:
            r["caption"] = f"{r['path']}:{r['start_line']}-{r['end_line']}"
        return {"chunks": self._note(rows)}

    def callers(self, symbol_id: str, hops: int = 2) -> dict:
        """Static callers of a symbol: structural reach, not an observed outage."""
        h = 3 if hops >= 3 else 2 if hops == 2 else 1
        rows = self._run(
            f"MATCH p=(c:Entity:CodeSymbol)-[:CALLS*1..{h}]->(s:Entity:CodeSymbol {{id: $id}}) WHERE s.project_id IN $pids AND c.project_id = s.project_id "
            "OPTIONAL MATCH (f:Entity:SourceFile {id: c.file_id}) RETURN DISTINCT c.id AS id, c.qualified_name AS caption, f.path AS path, length(p) AS hops "
            "ORDER BY hops, caption LIMIT 30", id=symbol_id)
        return {"callers": self._note(rows)}


TOOL_SPECS: list[dict] = [
    {"name": "graph_overview", "description": "Counts of everything in the evidence graph for the projects in scope, plus services, environments and the log time range. Call first when unsure what exists.", "parameters": {}},
    {"name": "search", "description": "Full-text search over names, titles, messages, summaries, file paths and code symbols. Returns node ids to open with get_node.",
     "parameters": {"query": {"type": "string"}, "label": {"type": "string", "description": "optional node label, e.g. Ticket, LogEvent, Service, CodeSymbol, Deployment, Finding"}}, "required": ["query"]},
    {"name": "get_node", "description": "One node with its properties and its connections (depth 1, or 2 for neighbours of neighbours). The way to follow relationships.",
     "parameters": {"id": {"type": "string"}, "depth": {"type": "integer", "enum": [1, 2]}}, "required": ["id"]},
    {"name": "list_tickets", "description": "Tickets with their investigation status and finding summary. Leave status empty to list all; use it only when the user names a status.", "parameters": {"status": {"type": "string", "enum": ["open", "resolved", "closed"]}}},
    {"name": "get_investigation", "description": "An investigation run: finding, claims with citations, hypotheses and their assessment, evidence gaps. Takes the run id (run_...).",
     "parameters": {"run_id": {"type": "string"}}, "required": ["run_id"]},
    {"name": "error_summary", "description": "Runtime log evidence grouped by service, severity and operation with counts and time ranges. Best first step for 'what is failing'.",
     "parameters": {"service": {"type": "string"}, "severity": {"type": "string", "enum": ["error", "warn", "info", "fatal"]}}},
    {"name": "recent_events", "description": "Newest log events, optionally by severity or a text fragment.",
     "parameters": {"severity": {"type": "string"}, "text": {"type": "string"}, "limit": {"type": "integer"}}},
    {"name": "request_trace", "description": "Every log event of one request id, in order.", "parameters": {"request_id": {"type": "string"}}, "required": ["request_id"]},
    {"name": "deployments", "description": "Deployments with status, time, service and the commit they ran (commit unknown stays unknown).", "parameters": {"limit": {"type": "integer"}}},
    {"name": "code_for_event", "description": "Code symbols (file, lines, certainty) that a log event's stack trace resolves to.", "parameters": {"event_id": {"type": "string"}}, "required": ["event_id"]},
    {"name": "search_code", "description": "Full-text search over indexed source code; returns excerpts with file and line range.", "parameters": {"query": {"type": "string"}}, "required": ["query"]},
    {"name": "callers", "description": "Static callers of a code symbol up to 3 hops. Structural reach, not proof of an outage.", "parameters": {"symbol_id": {"type": "string"}, "hops": {"type": "integer"}}, "required": ["symbol_id"]},
]

DISPATCH: dict[str, Callable[..., dict]] = {s["name"]: getattr(GraphTools, s["name"]) for s in TOOL_SPECS}
