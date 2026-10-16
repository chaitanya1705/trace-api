"""Fixed, parameterized read templates (ontology O9/O10). The investigator never supplies Cypher: it picks a template by
name and supplies parameters, and the backend has already authorized the project, environment and revision scope.

Every template requires $project_id and constrains every node and relationship it touches to it. Results are bounded by a
required limit. Template names carry a version so retrieval manifests can record exactly which query produced a result.
"""
from __future__ import annotations

from dataclasses import dataclass

MAX_LIMIT = 200
MAX_HOPS = 4  # default 2, ceiling 4 (O8)


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
            "runtime_observations", 1,
            "MATCH (e:Entity:EvidenceRecord)-[:OBSERVED_ON]->(s:Entity:Service {id: $service_id, project_id: $project_id}) "
            "WHERE e.project_id = $project_id AND e.environment_id = $environment_id "
            "AND e.event_time >= datetime($window_start) AND e.event_time < datetime($window_end) "
            "AND e.ingested_at <= datetime($cutoff) "
            "RETURN e.id AS id, e.event_time AS event_time, e.content_hash AS content_hash, labels(e) AS labels, e.redacted_content AS redacted_content "
            "ORDER BY e.event_time LIMIT $limit",
            frozenset({"project_id", "service_id", "environment_id", "window_start", "window_end", "cutoff", "limit"}),
            "observations known at the investigation cutoff, inside the window (event time, not ingest time)",
        ),
        Template(
            "unknown_time_observations", 1,
            "MATCH (e:Entity:EvidenceRecord)-[:OBSERVED_ON]->(s:Entity:Service {id: $service_id, project_id: $project_id}) "
            "WHERE e.project_id = $project_id AND e.environment_id = $environment_id AND e.event_time IS NULL "
            "AND e.ingested_at <= datetime($cutoff) "
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
    return t


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
            return [_path(r["p"]) for r in s.run(t.cypher, **params)]
        return [r.data() for r in s.run(t.cypher, **params)]
