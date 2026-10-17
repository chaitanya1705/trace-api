"""Fixed, parameterized read templates (ontology O9/O10). The investigator never supplies Cypher: it picks a template by
name and supplies parameters, and the backend has already authorized the project, environment and revision scope.

Every template requires $project_id and constrains every node and relationship it touches to it. Results are bounded by a
required limit. Template names carry a version so retrieval manifests can record exactly which query produced a result.
"""
from __future__ import annotations

from dataclasses import dataclass

MAX_LIMIT = 200
#: O8: an expansion returns at most 100 nodes / 200 edges. Path templates return one row per path, so their row
#: ceiling is the node ceiling; everything else is bounded by MAX_LIMIT.
MAX_NODES = 100
MAX_EDGES = 200
EXPANSION_PREFIXES = ("callers_", "potential_exposure")
FULLTEXT_CANDIDATES = 500
MAX_HOPS = 4  # default 2, ceiling 4 (O8)
BROWSER_OBSERVATION_EXCLUSION = "NOT (coalesce(e.origin_detail, '') CONTAINS '(untrusted browser observation)')"


@dataclass(frozen=True)
class Template:
    name: str
    version: int
    cypher: str
    params: frozenset[str]
    doc: str

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"


def _caller_paths(depth: int) -> Template:
    if not 1 <= depth <= MAX_HOPS:
        raise ValueError("hop depth must be 1..4")
    # Incoming direction: who reaches the seed through CALLS. Structural connectivity, not an observed outage.
    cypher = (
        f"MATCH p=(caller:Entity:CodeSymbol)-[:CALLS*1..{depth}]->(seed:Entity:CodeSymbol {{id: $seed_id, project_id: $project_id}}) "
        "WHERE all(n IN nodes(p) WHERE n.project_id = $project_id) "
        "AND all(r IN relationships(p) WHERE r.build_id IN $build_ids AND r.project_id = $project_id) "
        "RETURN p LIMIT $limit"
    )
    return Template(f"callers_{depth}hop", 1, cypher, frozenset({"project_id", "seed_id", "build_ids", "limit"}), f"callers up to {depth} hops from a symbol")


TEMPLATES: dict[str, Template] = {
    t.name: t
    for t in [
        Template(
            "source_excerpt", 1,
            "MATCH (f:Entity:SourceFile {project_id: $project_id, revision_id: $revision_id, path: $path})-[:HAS_CHUNK]->(c:Entity:SourceChunk) "
            "WHERE c.project_id = $project_id AND c.start_line <= $end_line AND c.end_line >= $start_line "
            "RETURN c.id AS id, c.content AS content, c.start_line AS start_line, c.end_line AS end_line, c.content_hash AS content_hash "
            "ORDER BY c.start_line LIMIT $limit",
            frozenset({"project_id", "revision_id", "path", "start_line", "end_line", "limit"}),
            "exact source excerpt candidates at one repository revision",
        ),
        Template(
            "runtime_observations", 2,
            "MATCH (e:Entity:EvidenceRecord)-[:OBSERVED_ON]->(s:Entity:Service {id: $service_id, project_id: $project_id}) "
            "WHERE e.project_id = $project_id AND e.environment_id = $environment_id "
            "AND e.event_time >= datetime($window_start) AND e.event_time < datetime($window_end) "
            f"AND e.ingested_at <= datetime($cutoff) AND {BROWSER_OBSERVATION_EXCLUSION} "
            "RETURN e.id AS id, e.event_time AS event_time, e.content_hash AS content_hash, labels(e) AS labels, e.redacted_content AS redacted_content "
            "ORDER BY e.event_time LIMIT $limit",
            frozenset({"project_id", "service_id", "environment_id", "window_start", "window_end", "cutoff", "limit"}),
            "observations known at the investigation cutoff, inside the window (event time, not ingest time)",
        ),
        Template(
            "unknown_time_observations", 2,
            "MATCH (e:Entity:EvidenceRecord)-[:OBSERVED_ON]->(s:Entity:Service {id: $service_id, project_id: $project_id}) "
            "WHERE e.project_id = $project_id AND e.environment_id = $environment_id AND e.event_time IS NULL "
            f"AND e.ingested_at <= datetime($cutoff) AND {BROWSER_OBSERVATION_EXCLUSION} "
            "RETURN e.id AS id, e.ingested_at AS ingested_at, e.content_hash AS content_hash "
            "ORDER BY e.ingested_at LIMIT $limit",
            frozenset({"project_id", "service_id", "environment_id", "cutoff", "limit"}),
            "evidence with no event time: returned as labelled unknown-time candidates, never placed on the timeline",
        ),
        Template(
            "deployments_in_window", 1,
            "MATCH (d:Entity:Deployment)-[:DEPLOYS_SERVICE]->(s:Entity:Service {id: $service_id, project_id: $project_id}) "
            "WHERE d.project_id = $project_id AND d.environment_id = $environment_id "
            "AND d.event_time >= datetime($window_start) AND d.event_time < datetime($window_end) "
            "OPTIONAL MATCH (d)-[m:DEPLOYS_REVISION]->(r:Entity:Revision {project_id: $project_id}) "
            "RETURN d.id AS id, d.event_time AS event_time, d.status AS status, d.exposure_status AS exposure_status, "
            "r.id AS revision_id, r.commit_sha AS commit_sha, m.mapping_method AS mapping_method "
            "ORDER BY d.event_time LIMIT $limit",
            frozenset({"project_id", "service_id", "environment_id", "window_start", "window_end", "limit"}),
            "deployments of a service; the revision is optional because the mapping may be unknown",
        ),
        Template(
            "open_gaps_for_run", 1,
            "MATCH (r:Entity:InvestigationRun {id: $run_id, project_id: $project_id})-[:HAS_GAP]->(g:Entity:EvidenceGap) "
            "WHERE g.project_id = $project_id "
            "RETURN g.id AS id, g.kind AS kind, g.description AS description, g.status AS status, g.detected_at AS detected_at "
            "ORDER BY g.detected_at LIMIT $limit",
            frozenset({"project_id", "run_id", "limit"}),
            "evidence gaps recorded against a run",
        ),
        Template(
            "dependencies_valid_at", 1,
            "MATCH (a:Entity {project_id: $project_id})-[d:DEPENDS_ON]->(b:Entity {project_id: $project_id}) "
            "WHERE a.id = $service_id AND d.environment_id = $environment_id "
            "AND d.valid_from IS NOT NULL AND d.valid_from <= datetime($at) AND (d.valid_to IS NULL OR datetime($at) < d.valid_to) "
            "RETURN b.id AS id, labels(b) AS labels, d.origin AS origin, d.derivation AS derivation, d.verification AS verification "
            "ORDER BY b.id LIMIT $limit",
            frozenset({"project_id", "service_id", "environment_id", "at", "limit"}),
            "dependencies confirmed valid at an instant; unknown validity (no valid_from) is excluded from confirmed temporal claims",
        ),
        Template(
            "contradicting_evidence", 1,
            "MATCH (h:Entity:Hypothesis {id: $hypothesis_id, project_id: $project_id})-[:HAS_ASSESSMENT]->(a:Entity:Assessment {project_id: $project_id})"
            "-[c:CONTRADICTED_BY]->(e:Entity {project_id: $project_id}) "
            "RETURN a.sequence AS assessment_sequence, e.id AS id, labels(e) AS labels, c.explanation AS explanation ORDER BY a.sequence, e.id LIMIT $limit",
            frozenset({"project_id", "hypothesis_id", "limit"}),
            "evidence recorded against a hypothesis, so contradiction is retrieved rather than only support",
        ),
        Template(
            "potential_exposure", 1,
            "MATCH (sym:Entity:CodeSymbol {id: $symbol_id, project_id: $project_id})<-[r:CALLS|IMPORTS|USES*1..2]-(consumer:Entity:CodeSymbol {project_id: $project_id}) "
            "WHERE all(x IN r WHERE x.build_id IN $build_ids AND x.project_id = $project_id) "
            "OPTIONAL MATCH (consumer)-[im:IMPLEMENTS_SERVICE]->(svc:Entity:Service {project_id: $project_id}) "
            "RETURN DISTINCT consumer.id AS consumer_id, svc.id AS service_id, im.mapping_source AS mapping_source LIMIT $limit",
            frozenset({"project_id", "symbol_id", "build_ids", "limit"}),
            "incoming static consumers of a changed symbol and the services they map to: potential propagation, not an observed outage",
        ),
        Template(
            "source_text_search", 1,
            "CALL db.index.fulltext.queryNodes('source_text', $query, {limit: $candidates}) YIELD node, score "
            "WHERE node.project_id = $project_id "
            "MATCH (f:Entity:SourceFile {project_id: $project_id, id: node.file_id}) "
            "WHERE f.revision_id IN $revision_ids "
            "RETURN node.id AS id, f.path AS path, node.start_line AS start_line, node.end_line AS end_line, node.content_hash AS content_hash, score "
            "ORDER BY score DESC LIMIT $limit",
            frozenset({"project_id", "query", "revision_ids", "candidates", "limit"}),
            "full-text candidates, scope-filtered before leaving the graph layer; the global candidate pool is bounded, so truncation is reported",
        ),
        Template(
            "log_events_in_window", 2,
            "MATCH (e:Entity:LogEvent)-[:OBSERVED_ON]->(s:Entity:Service {id: $service_id, project_id: $project_id}) "
            "WHERE e.project_id = $project_id AND e.environment_id = $environment_id AND ($include_info OR e.severity <> 'info') "
            f"AND e.event_time >= datetime($window_start) AND e.event_time < datetime($window_end) AND e.ingested_at <= datetime($cutoff) AND {BROWSER_OBSERVATION_EXCLUSION} "
            "OPTIONAL MATCH (e)-[:ASSOCIATED_WITH]->(d:Entity:Deployment {project_id: $project_id}) "
            "RETURN e.id AS id, e.event_time AS event_time, e.severity AS severity, e.message AS message, e.source_locator AS route, "
            "e.content_hash AS content_hash, d.id AS deployment_id ORDER BY e.event_time LIMIT $limit",
            frozenset({"project_id", "service_id", "environment_id", "window_start", "window_end", "cutoff", "include_info", "limit"}),
            "log events of a service in a window with the deployment that was live when each happened",
        ),
        Template(
            "code_located_by_events", 1,
            "MATCH (e:Entity:LogEvent {project_id: $project_id})-[l:LOCATES]->(s:Entity:CodeSymbol {project_id: $project_id})<-[:DEFINES]-(f:Entity:SourceFile {project_id: $project_id}) "
            "WHERE e.id IN $event_ids "
            "RETURN s.id AS id, s.qualified_name AS name, s.kind AS kind, f.path AS path, f.revision_id AS revision_id, s.start_line AS start_line, "
            "s.end_line AS end_line, count(DISTINCT e) AS events, collect(DISTINCT l.certainty) AS certainty, collect(DISTINCT e.id)[..5] AS sample_event_ids "
            "ORDER BY events DESC, name LIMIT $limit",
            frozenset({"project_id", "event_ids", "limit"}),
            "code symbols that log events' stack frames resolve to at the revision live when they happened",
        ),
        Template(
            "changes_into_revision", 1,
            "MATCH (c:Entity:Change {project_id: $project_id})-[:AFTER_REVISION]->(r:Entity:Revision {id: $revision_id, project_id: $project_id}) "
            "OPTIONAL MATCH (c)-[:BEFORE_REVISION]->(b:Entity:Revision {project_id: $project_id}) "
            "OPTIONAL MATCH (c)-[m:MODIFIES]->(s:Entity:CodeSymbol {project_id: $project_id})<-[:DEFINES]-(f:Entity:SourceFile {project_id: $project_id}) "
            "WITH c, b, collect({id: s.id, name: s.qualified_name, path: f.path, revision_id: f.revision_id, role: m.before_after_role, start_line: s.start_line, end_line: s.end_line}) AS modified "
            "OPTIONAL MATCH (c)-[mf:MODIFIES]->(sf:Entity:SourceFile {project_id: $project_id}) "
            "RETURN c.id AS id, c.kind AS kind, c.source_id AS ref, c.event_time AS event_time, b.id AS before_revision_id, b.commit_sha AS before_sha, modified, "
            "collect({id: sf.id, path: sf.path, revision_id: sf.revision_id, role: mf.before_after_role}) AS files "
            "ORDER BY c.event_time DESC LIMIT $limit",
            frozenset({"project_id", "revision_id", "limit"}),
            "changes that produced a revision and the symbols they modified on each side",
        ),
        Template(
            "evidence_by_operation", 2,
            "MATCH (e:Entity:EvidenceRecord)-[:OBSERVED_ON]->(s:Entity:Service {id: $service_id, project_id: $project_id}) "
            "WHERE e.project_id = $project_id AND e.environment_id = $environment_id AND e.event_time >= datetime($window_start) AND e.event_time < datetime($window_end) "
            f"AND e.ingested_at <= datetime($cutoff) AND {BROWSER_OBSERVATION_EXCLUSION} AND any(l IN labels(e) WHERE l = 'LogEvent') "
            "RETURN e.severity AS severity, e.source_locator AS operation, count(*) AS events, collect(e.id)[..3] AS sample_ids, min(e.event_time) AS first, max(e.event_time) AS last "
            "ORDER BY events DESC LIMIT $limit",
            frozenset({"project_id", "service_id", "environment_id", "window_start", "window_end", "cutoff", "limit"}),
            "log evidence grouped by severity and operation, so a large window is summarised without reading every row",
        ),
        Template(
            "request_observations", 1,
            "MATCH (e:Entity:EvidenceRecord)-[:OBSERVED_ON]->(s:Entity:Service {id: $service_id, project_id: $project_id}) "
            "WHERE e.project_id = $project_id AND e.environment_id = $environment_id AND e.event_time >= datetime($window_start) AND e.event_time < datetime($window_end) "
            "AND e.ingested_at <= datetime($cutoff) AND (e:RequestSpan OR e:Measurement) "
            "RETURN e.id AS id, labels(e) AS labels, e.event_time AS event_time, e.redacted_content AS text, e.content_hash AS content_hash ORDER BY e.event_time LIMIT $limit",
            frozenset({"project_id", "service_id", "environment_id", "window_start", "window_end", "cutoff", "limit"}),
            "client-side request observations and run counts for a service in a window",
        ),
        Template(
            "code_located_in_window", 2,
            "MATCH (e:Entity:LogEvent {project_id: $project_id, service_id: $service_id, environment_id: $environment_id})-[l:LOCATES]->(s:Entity:CodeSymbol {project_id: $project_id})"
            "<-[:DEFINES]-(f:Entity:SourceFile {project_id: $project_id}) "
            f"WHERE e.event_time >= datetime($window_start) AND e.event_time < datetime($window_end) AND e.ingested_at <= datetime($cutoff) AND {BROWSER_OBSERVATION_EXCLUSION} "
            "RETURN s.id AS id, s.qualified_name AS name, s.kind AS kind, f.path AS path, f.revision_id AS revision_id, s.start_line AS start_line, s.end_line AS end_line, "
            "count(DISTINCT e) AS events, collect(DISTINCT l.certainty) AS certainty, collect(DISTINCT e.id)[..3] AS sample_event_ids ORDER BY events DESC, name LIMIT $limit",
            frozenset({"project_id", "service_id", "environment_id", "window_start", "window_end", "cutoff", "limit"}),
            "code symbols that evidence in the window locates, with how certain each mapping is",
        ),
        *(_caller_paths(d) for d in range(1, MAX_HOPS + 1)),
    ]
}


class TemplateError(ValueError):
    pass


def check_params(name: str, params: dict) -> Template:
    t = TEMPLATES.get(name)
    if t is None:
        raise TemplateError(f"unknown query template {name!r}")
    missing = sorted(t.params - params.keys())
    extra = sorted(params.keys() - t.params)
    if missing:
        raise TemplateError(f"{name}: missing parameter(s) {', '.join(missing)}")
    if extra:
        raise TemplateError(f"{name}: unexpected parameter(s) {', '.join(extra)}")
    if not params["project_id"]:
        raise TemplateError(f"{name}: project_id is required")
    limit = params["limit"]
    if not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise TemplateError(f"{name}: limit must be 1..{MAX_LIMIT}")
    if t.name.startswith(EXPANSION_PREFIXES) and limit > MAX_NODES:
        raise TemplateError(f"{name}: an expansion returns at most {MAX_NODES} rows")
    return t


def escape_lucene(text: str) -> str:
    """Make free text safe as a Lucene query (no operators, wildcards or field syntax)."""
    out = []
    for ch in text:
        out.append("\\" + ch if ch in '+-&|!(){}[]^"~*?:\\/' else ch)
    return "".join(out).strip()


def _path(p) -> dict:
    return {
        "nodes": [n["id"] for n in p.nodes],
        "relationships": [
            {"type": r.type, "key": r.get("key"), "build_id": r.get("build_id"), "derivation": r.get("derivation"), "source_locator": r.get("source_locator")}
            for r in p.relationships
        ],
    }


def run_template(driver, database: str, name: str, **params) -> list[dict]:
    """Execute a named template and return plain dicts. Paths are returned as bounded node/relationship lists:
    a path is structural connectivity, not causal proof (O8)."""
    t = check_params(name, params)
    with driver.session(database=database) as s:
        if t.name.startswith("callers_"):
            return [_path(r["p"]) for r in s.run(t.cypher, parameters=params)]
        return [r.data() for r in s.run(t.cypher, parameters=params)]


@dataclass(frozen=True)
class Bounded:
    rows: list
    truncated: bool  # the result hit its row limit, or the full-text candidate pool was exhausted
    template: str  # name@version, recorded in the RetrievalManifest


def run_bounded(driver, database: str, name: str, **params) -> Bounded:
    """run_template plus the truncation signal the investigator must turn into an EvidenceGap(truncated_query)."""
    t = check_params(name, params)
    rows = run_template(driver, database, name, **params)
    truncated = len(rows) >= params["limit"] or (name == "source_text_search" and len(rows) >= params["candidates"])
    if name == "source_text_search" and params["candidates"] > FULLTEXT_CANDIDATES:
        raise TemplateError(f"{name}: candidates must be <= {FULLTEXT_CANDIDATES}")
    return Bounded(rows, truncated, t.ref)
