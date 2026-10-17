"""TRACE evidence-graph ontology (TRACE/ontology.md O1-O5, O11). This module is the single machine-readable copy of
the node and relationship contract; Cypher is only ever built from the labels and types declared here.

Version it: any change to a label, required field or relationship is a new ONTOLOGY_VERSION plus a graph migration.
"""
from __future__ import annotations

from dataclasses import dataclass, field

ONTOLOGY_VERSION = "1.0"
SCHEMA_VERSION = 1

# Base label every node carries (shared identity and indexing).
BASE_LABEL = "Entity"

# Present on every project-owned node (O1). `id` is globally unique; project_id scopes every query.
COMMON_FIELDS = ("id", "project_id", "schema_version", "created_at")


@dataclass(frozen=True)
class NodeSpec:
    label: str
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()
    #: Extra labels applied together with `label` (EvidenceRecord subtypes carry both).
    also: tuple[str, ...] = ()
    #: Immutable nodes are never rewritten once created; a re-write with a different content hash is an error.
    immutable: bool = False
    #: At least one of these must be set (ImpactAssessment needs a run or a change).
    one_of: tuple[str, ...] = ()
    doc: str = ""

    @property
    def labels(self) -> tuple[str, ...]:
        return (BASE_LABEL, *self.also, self.label)

    @property
    def fields(self) -> frozenset[str]:
        return frozenset((*COMMON_FIELDS, *self.required, *self.optional))


def _evidence_fields() -> tuple[str, ...]:
    return ("source_connection_id", "source_record_key", "ingested_at", "origin", "content_hash", "redacted_content")


# `event_time` is required on EvidenceRecord but may be null: O6 allows unknown-time candidates (labelled), and
# ingest time must never be copied into it. Validation therefore requires the key but accepts None.
NULLABLE_REQUIRED: dict[str, tuple[str, ...]] = {
    "EvidenceRecord": ("event_time",),
    "LogEvent": ("event_time",),
    "Measurement": ("event_time",),
    "RequestSpan": ("event_time",),
    "HumanObservation": ("event_time",),
    "Deployment": ("revision_id",),
    "Change": ("before_ref", "after_ref"),
}

_EV = _evidence_fields()
_EV_OPT = ("environment_id", "service_id", "dedupe_method", "origin_detail", "source_locator", "source_clock_skew_ms")

NODES: dict[str, NodeSpec] = {
    s.label: s
    for s in [
        NodeSpec("Project", ("organization_id", "name"), doc="one project boundary"),
        NodeSpec("Environment", ("name", "environment_key"), doc="belongs to exactly one project"),
        NodeSpec("Service", ("name", "service_key"), doc="logical component, may have many deployments"),
        NodeSpec("Resource", ("provider", "resource_type", "provider_resource_id"), doc="infrastructure or external dependency"),
        NodeSpec("Journey", ("name", "definition_source"), doc="optional business journey; never inferred from a community label"),
        NodeSpec("SourceConnection", ("connection_id", "provider", "capabilities"), doc="non-secret provenance mirror; control DB is authoritative"),
        NodeSpec("Repository", ("provider", "provider_repository_id", "canonical_url"), doc="project binding to a repository"),
        NodeSpec("Revision", ("repository_id", "commit_sha"), immutable=True, doc="immutable commit"),
        NodeSpec("GraphBuild", ("revision_id", "extractor_version", "output_schema_version", "ontology_version", "status", "counts"), doc="one extraction attempt of a revision"),
        NodeSpec("RevisionManifest", ("entries_json", "manifest_hash"), immutable=True, doc="selected repository/revision/build combinations for an analysis"),
        NodeSpec("SourceFile", ("revision_id", "path", "content_hash", "language"), immutable=True, doc="exact revision content"),
        NodeSpec("SourceChunk", ("file_id", "start_line", "end_line", "content", "content_hash"), immutable=True, doc="citable bounded text with exact line alignment"),
        NodeSpec("CodeSymbol", ("file_id", "qualified_name", "kind", "start_line", "end_line"), ("signature", "build_id"), doc="function, method or class at a revision"),
        NodeSpec("CodeCommunity", ("build_id", "label"), doc="extraction grouping, not a service"),
        NodeSpec("SchemaEntity", ("file_id", "name", "kind", "definition_locator"), doc="code-derived table/column; runtime drift unknown"),
        NodeSpec("Deployment", ("service_id", "environment_id", "provider_id", "status", "event_time", "exposure_status"), ("revision_id",), doc="revision may be unknown; gradual rollout possible"),
        NodeSpec("Change", ("source_id", "kind", "event_time"), ("before_ref", "after_ref"), doc="commit/config/flag/migration/deployment change"),
        NodeSpec("EvidenceRecord", ("source_connection_id", "source_record_key", "ingested_at", "origin", "content_hash", "redacted_content"), ("event_time", *_EV_OPT), immutable=True, doc="immutable normalized observation"),
        NodeSpec("LogEvent", (*_EV, "severity", "message"), ("event_time", "request_id", "stack_frames_json", *_EV_OPT), also=("EvidenceRecord",), immutable=True),
        NodeSpec("Measurement", (*_EV, "metric", "unit", "value", "aggregation", "window_start", "window_end"), ("event_time", "sample_count", "sampling", *_EV_OPT), also=("EvidenceRecord",), immutable=True, doc="aggregation definition is mandatory"),
        NodeSpec("RequestSpan", (*_EV, "trace_id", "span_id", "operation", "status"), ("event_time", "parent_span_id", "duration", *_EV_OPT), also=("EvidenceRecord",), immutable=True, doc="only when the source supplies span evidence"),
        NodeSpec("HumanObservation", (*_EV, "actor_id", "submitted_at", "check_id", "verification_status"), ("event_time", "claimed_observed_at", *_EV_OPT), also=("EvidenceRecord",), immutable=True, doc="human origin preserved"),
        NodeSpec("EvidenceGap", ("kind", "description", "scope_json", "detected_at", "status"), ("resolved_at", "source_job_id"), doc="absent/stale/partial/ambiguous/unsupported source or mapping"),
        NodeSpec("Ticket", ("control_ticket_id", "title", "reported_at", "revision_number"), ("source_id",), doc="a report, not a proven observation"),
        NodeSpec("InvestigationRun", ("control_run_id", "environment_id", "window_start", "window_end", "evidence_cutoff", "status"), ("manifest_id",), doc="immutable run scope"),
        NodeSpec("Step", ("run_id", "sequence", "kind", "status", "started_at"), ("finished_at",), doc="persisted execution progression"),
        NodeSpec("ToolExecution", ("run_id", "step_id", "tool_name", "input_hash", "query_version", "status", "coverage_json"), doc="manifests, not raw arguments"),
        NodeSpec("RetrievalManifest", ("run_id", "execution_id", "item_ids_json", "hash", "cutoff"), ("query_template", "truncated", "gaps_json"), immutable=True, doc="exact items a citation may point at"),
        NodeSpec("Hypothesis", ("run_id", "statement", "created_step"), doc="candidate explanation"),
        NodeSpec("Assessment", ("hypothesis_id", "sequence", "status", "summary", "assessed_at"), immutable=True, doc="versioned; never rewritten"),
        NodeSpec("Claim", ("run_id", "text", "kind"), doc="factual claims must cite primary evidence"),
        NodeSpec("Finding", ("run_id", "sequence", "status", "summary", "limitations_json"), immutable=True, doc="versioned conclusion"),
        NodeSpec("ImpactAssessment", ("target_id", "categories", "scope_json", "assessed_at"), ("run_id", "change_id"), one_of=("run_id", "change_id"), doc="non-exclusive exposure categories"),
        NodeSpec("HumanCheck", ("run_id", "step_id", "question", "scope_json", "status"), ("instructions", "expected_result"), doc="user request or manual diagnostic"),
        NodeSpec("ResolutionPlan", ("run_id", "finding_id", "version", "actions_json", "preconditions_json", "criteria_json"), doc="proposed plan; control DB governs operations"),
        NodeSpec("PatchVersion", ("plan_id", "base_manifest_id", "content_hash", "artifact_ref", "test_status"), immutable=True, doc="immutable proposed patch; no approval secrets"),
        NodeSpec("ExternalChangeTicket", ("provider", "external_id", "url", "operation_id"), doc="non-secret evidence of an authorized ticket write"),
        NodeSpec("PullRequest", ("repository_id", "provider_pr_id", "url", "base_sha", "head_sha", "status"), doc="provider facts; merge is not deployment"),
        NodeSpec("VerificationRun", ("plan_id", "criteria_version", "outcome"), ("deployment_id", "window_start", "window_end"), doc="recovery check against explicit criteria"),
        NodeSpec("ConversationAnswer", ("conversation_id", "message_id", "version", "text", "status"), ("run_id",), doc="generated interpretation, never fresh primary evidence"),
        NodeSpec("Review", ("finding_id", "reviewer_id", "label", "reason", "reviewed_at"), immutable=True, doc="human evaluation; revisions are audited"),
    ]
}

EVIDENCE_LABELS = frozenset({"EvidenceRecord", "LogEvent", "Measurement", "RequestSpan", "HumanObservation"})

# ---- controlled vocabularies (O5, O11) -------------------------------------------------------------------------
ORIGINS = frozenset({"code", "configuration", "provider_metadata", "telemetry", "human"})
DERIVATIONS = frozenset({"extracted", "resolved", "inferred", "ambiguous", "declared"})
VERIFICATIONS = frozenset({"unverified", "corroborated", "disputed"})
ASSESSMENT_STATUSES = frozenset({"supported", "plausible", "contradicted", "insufficient"})
GAP_KINDS = frozenset(
    {
        "source_disconnected", "permission_missing", "rate_limited", "stale", "partial_parse", "ambiguous_revision",
        "unsupported_language", "unresolved_dependency", "truncated_query", "insufficient_traffic", "missing_baseline", "redacted_content",
    }
)
IMPACT_CATEGORIES = frozenset({"direct_exposure", "potential_propagation", "observed_impact", "unknown"})
VERIFICATION_OUTCOMES = frozenset({"awaiting_deployment", "watching", "recovered", "regressed", "inconclusive", "unable_to_verify"})
CLAIM_KINDS = frozenset({"observed_summary", "inference", "recommendation"})
RUN_STATUSES = frozenset({"queued", "running", "waiting_for_input", "completed", "failed", "cancelled"})

# (label, field) -> allowed values. List-valued fields (categories) are checked element-wise.
ENUMS: dict[tuple[str, str], frozenset[str]] = {
    ("EvidenceRecord", "origin"): ORIGINS,
    ("Assessment", "status"): ASSESSMENT_STATUSES,
    ("EvidenceGap", "kind"): GAP_KINDS,
    ("ImpactAssessment", "categories"): IMPACT_CATEGORIES,
    ("VerificationRun", "outcome"): VERIFICATION_OUTCOMES,
    ("Claim", "kind"): CLAIM_KINDS,
    ("InvestigationRun", "status"): RUN_STATUSES,
    ("HumanObservation", "verification_status"): VERIFICATIONS,
}

# Fields holding instants. They must be timezone-aware datetimes (UTC); naive values are rejected.
TIMESTAMP_SUFFIXES = ("_at",)
TIMESTAMP_FIELDS = frozenset(
    {"event_time", "window_start", "window_end", "cutoff", "evidence_cutoff", "valid_from", "valid_to", "claimed_observed_at"}
)

# ---- relationships (O4) ----------------------------------------------------------------------------------------
_EXTRACTION = ("build_id", "derivation", "source_locator", "extractor_version")
#: O5: keep the extractor's own confidence string verbatim and the independent verification dimension.
_EXTRACTION_OPT = ("graphify_confidence", "verification")


@dataclass(frozen=True)
class RelSpec:
    type: str
    sources: frozenset[str]
    targets: frozenset[str]
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    doc: str = ""
    #: Properties that distinguish otherwise identical edges (several call sites, several builds).
    discriminators: tuple[str, ...] = field(default_factory=tuple)

    @property
    def context(self) -> frozenset[str]:
        return frozenset(("project_id", "key", *self.required, *self.optional))


_CODE_SYMBOLISH = ("CodeSymbol", "SourceFile")
_ENTITY_KINDS = ("Service", "Resource", "Journey")


def _r(t: str, src: tuple[str, ...], dst: tuple[str, ...], required: tuple[str, ...] = (), optional: tuple[str, ...] = (), doc: str = "", disc: tuple[str, ...] = ()) -> RelSpec:
    return RelSpec(t, frozenset(src), frozenset(dst), required, optional, doc, disc)


RELATIONSHIPS: dict[str, RelSpec] = {
    r.type: r
    for r in [
        _r("HAS_ENVIRONMENT", ("Project",), ("Environment",)),
        _r("HAS_SERVICE", ("Project",), ("Service",)),
        _r("HAS_RESOURCE", ("Project",), ("Resource",)),
        _r("HAS_JOURNEY", ("Project",), ("Journey",)),
        _r("HAS_REPOSITORY", ("Project",), ("Repository",), ("scope_source",)),
        _r("HAS_REVISION", ("Repository",), ("Revision",)),
        _r("INDEXES", ("GraphBuild",), ("Revision",)),
        _r("SELECTS_BUILD", ("RevisionManifest",), ("GraphBuild",), ("repository_id",)),
        _r("HAS_FILE", ("Revision",), ("SourceFile",)),
        _r("HAS_CHUNK", ("SourceFile",), ("SourceChunk",)),
        _r("DEFINES", ("SourceFile",), ("CodeSymbol", "SchemaEntity"), ("build_id", "source_locator"), disc=("build_id", "source_locator")),
        _r("CALLS", ("CodeSymbol",), ("CodeSymbol",), _EXTRACTION, _EXTRACTION_OPT, disc=("build_id", "source_locator")),
        _r("IMPORTS", ("SourceFile", "CodeSymbol"), ("SourceFile", "CodeSymbol"), _EXTRACTION, _EXTRACTION_OPT, disc=("build_id", "source_locator")),
        _r("INHERITS_FROM", ("CodeSymbol",), ("CodeSymbol",), _EXTRACTION, _EXTRACTION_OPT, disc=("build_id", "source_locator")),
        _r("USES", ("CodeSymbol", "SourceFile"), ("CodeSymbol", "SchemaEntity", "SourceFile"), _EXTRACTION, _EXTRACTION_OPT, disc=("build_id", "source_locator")),
        _r("CONTAINS", ("CodeSymbol", "SourceFile"), ("CodeSymbol",), _EXTRACTION, _EXTRACTION_OPT, disc=("build_id", "source_locator")),
        _r("MEMBER_OF", ("CodeSymbol",), ("CodeCommunity",), ("build_id",), disc=("build_id",)),
        _r("REFERENCES_SCHEMA", ("CodeSymbol", "SchemaEntity"), ("SchemaEntity",), ("build_id", "derivation", "source_locator"), _EXTRACTION_OPT, disc=("build_id", "source_locator")),
        _r("IMPLEMENTS_SERVICE", ("Repository", "CodeSymbol"), ("Service",), ("mapping_source",), ("valid_from", "valid_to", "evidence_ids", "verification")),
        _r("DEPLOYS_REVISION", ("Deployment",), ("Revision",), ("mapping_method",), ("evidence_ids",)),
        _r("DEPLOYS_SERVICE", ("Deployment",), ("Service",)),
        _r("IN_ENVIRONMENT", ("Deployment", *EVIDENCE_LABELS), ("Environment",), ("mapping_method",)),
        _r("DEPENDS_ON", _ENTITY_KINDS, ("Service", "Resource"), ("origin", "derivation", "environment_id"), ("valid_from", "valid_to", "evidence_ids", "verification"), disc=("origin", "environment_id")),
        _r("OBSERVED_ON", tuple(EVIDENCE_LABELS), ("Service", "Resource"), ("mapping_method",)),
        _r("ASSOCIATED_WITH", tuple(EVIDENCE_LABELS), ("Deployment", "Change"), ("mapping_method",), ("evidence_ids",)),
        _r("LOCATES", tuple(EVIDENCE_LABELS), ("CodeSymbol", "SourceChunk"), ("mapping_method", "certainty", "manifest_id")),
        _r("BEFORE_REVISION", ("Change",), ("Revision",), ("repository_id",)),
        _r("AFTER_REVISION", ("Change",), ("Revision",), ("repository_id",)),
        _r("BASE_REVISION", ("PullRequest",), ("Revision",)),
        _r("HEAD_REVISION", ("PullRequest",), ("Revision",)),
        _r("MERGED_AS", ("PullRequest",), ("Revision",)),
        _r("PROPOSED_AS", ("PatchVersion",), ("PullRequest",), ("operation_id", "patch_hash")),
        _r("MODIFIES", ("Change",), ("SourceFile", "CodeSymbol", "SchemaEntity"), ("before_after_role", "manifest_id")),
        _r("HAS_RUN", ("Ticket",), ("InvestigationRun",), ("request_actor_id",)),
        _r("HAS_STEP", ("InvestigationRun",), ("Step",), ("sequence",)),
        _r("EXECUTED", ("Step",), ("ToolExecution",), ("sequence",)),
        _r("RETRIEVED", ("ToolExecution",), (*EVIDENCE_LABELS, "SourceChunk"), ("manifest_id",)),
        _r("CONSIDERS", ("InvestigationRun",), ("Hypothesis",), ("created_step",)),
        _r("HAS_ASSESSMENT", ("Hypothesis",), ("Assessment",), ("sequence",)),
        _r("SUPPORTED_BY", ("Assessment", "ImpactAssessment"), (*EVIDENCE_LABELS, "SourceChunk"), ("explanation", "step_id")),
        _r("CONTRADICTED_BY", ("Assessment", "ImpactAssessment"), (*EVIDENCE_LABELS, "SourceChunk"), ("explanation", "step_id")),
        _r("HAS_GAP", ("InvestigationRun", "Assessment", "VerificationRun"), ("EvidenceGap",), ("scope",)),
        _r("PRODUCES", ("InvestigationRun",), ("Finding",), ("sequence",)),
        _r("HAS_CLAIM", ("Finding",), ("Claim",), ("sequence",)),
        _r("CITES", ("Claim", "ConversationAnswer"), (*EVIDENCE_LABELS, "SourceChunk"), ("locator", "content_hash", "retrieval_manifest_id")),
        _r("TARGETS", ("ImpactAssessment",), ("Service", "Resource", "CodeSymbol", "Journey")),
        _r("REQUESTS", ("InvestigationRun",), ("HumanCheck",), ("step_id",)),
        _r("ANSWERED_BY", ("HumanCheck",), ("HumanObservation",), ("accepted_submission_id",)),
        _r("ADDRESSES", ("ResolutionPlan",), ("Finding",), ("version",)),
        _r("HAS_PATCH", ("ResolutionPlan",), ("PatchVersion",), ("version",)),
        _r("TRACKS_PR", ("ResolutionPlan",), ("PullRequest",), ("operation_id",)),
        _r("TRACKS_TICKET", ("ResolutionPlan",), ("ExternalChangeTicket",), ("operation_id",)),
        _r("EVALUATES", ("VerificationRun",), ("ResolutionPlan",), ("criteria_version",)),
        _r("OBSERVED_DEPLOYMENT", ("VerificationRun",), ("Deployment",), ("criteria_version",)),
        _r("REVIEWED_BY", ("Finding",), ("Review",), ("review_version",)),
    ]
}

# Optional cross-revision identity mapping (O3): only with the algorithm and its uncertainty recorded.
RELATIONSHIPS["CORRESPONDS_TO"] = _r("CORRESPONDS_TO", ("CodeSymbol", "SourceFile"), ("CodeSymbol", "SourceFile"), ("algorithm", "uncertainty", "evidence_ids"))

#: Evidence nodes in the graph are matched by their EvidenceRecord label; this lets relationship checks treat a
#: LogEvent as an EvidenceRecord.
LABEL_ALIASES: dict[str, frozenset[str]] = {s.label: frozenset((s.label, *s.also)) for s in NODES.values()}


def labels_of(label: str) -> frozenset[str]:
    return LABEL_ALIASES[label]


#: O1: "scope relationships and scalar scope fields must agree". relationship -> property on the source node that, when set,
#: must equal the target node id. The writer refuses an edge that contradicts the node's own scope field.
SCOPE_AGREEMENT: dict[str, str] = {
    "IN_ENVIRONMENT": "environment_id",
    "OBSERVED_ON": "service_id",
    "DEPLOYS_SERVICE": "service_id",
    "DEPLOYS_REVISION": "revision_id",
}

#: Relationship properties that list evidence node ids; every listed id must exist in the same project (O4).
EVIDENCE_ID_FIELDS = ("evidence_ids",)

#: GraphBuild lifecycle (O7): readers and manifests select only published builds.
BUILD_STATUSES = frozenset({"staging", "published", "failed", "superseded"})
ENUMS[("GraphBuild", "status")] = BUILD_STATUSES
