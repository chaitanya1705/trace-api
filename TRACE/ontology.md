# TRACE ontology, GraphRAG and multi-hop contract

Version 1.0 · 12 October 2026 · Normative graph specification. Use architecture.md for deployment/control-plane storage. Examples are illustrative; no provider data or implementation is supplied.

## O1. Graph areas and universal properties

One Neo4j evidence graph connects application structure, revision-specific code, operational evidence and investigation interpretations. Every project-owned node has `project_id`, globally unique `id`, `schema_version`, `created_at`. Evidence has immutable content/hash, event time if known, ingestion time, source locator and origin. Updated interpretations create versions rather than rewriting observed facts.

Use an `Entity` base label for shared identity/indexing and additional typed labels below. Authorization is a backend responsibility and every query constrains project, both endpoints and applicable revision/environment. Supabase membership/credential secrets are not graph evidence; store only non-secret source-connection references.

EvidenceRecord scope fields apply to all subtypes, including measurements, spans and human observations. Scope relationships and scalar scope fields must agree; normalization rejects conflicting mappings. Missing values are null plus a gap/reason where material; do not use zero/healthy/never defaults for unknown measurements, status or expiry.

## O2. Entities and required properties

Common `id,project_id,schema_version,created_at` omitted from table. Required may be null only when explicit unknown semantics stated.

| Label | Required fields | Cardinality/meaning |
|---|---|---|
| Project | organization_id,name | one project boundary |
| Environment | name,environment_key | belongs to exactly one project |
| Service | name,service_key | logical component, may have many deployments |
| Resource | provider,resource_type,provider_resource_id | project-scoped infrastructure/external dependency |
| Journey | name,definition_source | optional business journey; never inferred solely from community label |
| SourceConnection | connection_id,provider,capabilities | non-secret provenance mirror, control DB authoritative |
| Repository | provider,provider_repository_id,canonical_url | project binding to repository |
| Revision | repository_id,commit_sha | immutable commit |
| GraphBuild | revision_id,extractor_version,output_schema_version,ontology_version,status,counts | potentially multiple extraction attempts per revision |
| RevisionManifest | entries_json,manifest_hash | selected repository/revision/build combinations for an analysis |
| SourceFile | revision_id,path,content_hash,language | exact revision content; relative canonical path |
| SourceChunk | file_id,start_line,end_line,content,content_hash | citable bounded text; exact line alignment |
| CodeSymbol | file_id,qualified_name,kind,start_line,end_line | overloads distinguished by location/signature |
| CodeCommunity | build_id,label | extraction grouping, not a service |
| SchemaEntity | file_id,name,kind,definition_locator | code-derived table/column/schema; runtime drift unknown |
| Deployment | service_id,environment_id,provider_id,status,event_time,exposure_status | revisions may be unknown; gradual rollout possible |
| Change | source_id,kind,event_time,before_ref,after_ref | commit/config/flag/migration/deployment change; refs may be unknown |
| EvidenceRecord | source_connection_id,source_record_key,event_time,ingested_at,origin,content_hash,redacted_content,environment_id?,service_id? | immutable normalized observation |
| LogEvent | severity,message,service_id?,environment_id?,request_id?,stack_frames_json? | additional EvidenceRecord label |
| Measurement | metric,unit,value,aggregation,window_start,window_end,sample_count?,sampling? | additional EvidenceRecord; aggregation definition mandatory |
| RequestSpan | trace_id,span_id,parent_span_id?,operation,duration?,status | additional EvidenceRecord label; only when actual source supplies span evidence |
| HumanObservation | actor_id,submitted_at,claimed_observed_at?,check_id,verification_status | additional EvidenceRecord; preserve human origin |
| EvidenceGap | kind,description,scope_json,detected_at,status | absent/stale/partial/ambiguous/unsupported source or mapping |
| Ticket | control_ticket_id,source_id?,title,reported_at,revision_number | report, not proven observation |
| InvestigationRun | control_run_id,environment_id,window_start,window_end,evidence_cutoff,status,manifest_id? | immutable run scope; continuation checkpoints versioned |
| Step | run_id,sequence,kind,status,started_at,finished_at? | persisted execution progression |
| ToolExecution | run_id,step_id,tool_name,input_hash,query_version,status,coverage_json | result/evidence manifests, not secret raw arguments |
| RetrievalManifest | run_id,execution_id,item_ids_json,hash,cutoff | exact retrieved entities/chunks/paths permitted for citations |
| Hypothesis | run_id,statement,created_step | candidate explanation |
| Assessment | hypothesis_id,sequence,status,summary,assessed_at | supported/plausible/contradicted/insufficient |
| Claim | run_id,text,kind | observed-summary/inference/recommendation; citations mandatory for factual claims |
| Finding | run_id,sequence,status,summary,limitations_json | versioned conclusion |
| ImpactAssessment | run_id?,change_id?,target_id,categories,scope_json,assessed_at | non-exclusive exposure/propagation/observed/unknown |
| HumanCheck | run_id,step_id,question,instructions?,expected_result?,scope_json,status | user request/manual diagnostic |
| ResolutionPlan | run_id,finding_id,version,actions_json,preconditions_json,criteria_json | proposed plan; control DB governs operations |
| PatchVersion | plan_id,base_manifest_id,content_hash,artifact_ref,test_status | immutable proposed patch; no approval secrets |
| ExternalChangeTicket | provider,external_id,url,operation_id | non-secret evidence of authorized ticket write |
| PullRequest | repository_id,provider_pr_id,url,base_sha,head_sha,status | provider facts; merge not deployment |
| VerificationRun | plan_id,deployment_id?,criteria_version,window_start?,window_end?,outcome | awaiting_deployment/watching/recovered/regressed/inconclusive/unable_to_verify |
| ConversationAnswer | conversation_id,message_id,version,run_id?,text,status | generated interpretation, never fresh primary evidence |
| Review | finding_id,reviewer_id,label,reason,reviewed_at | human evaluation; audit immutable revisions |

Nested metadata may be serialized JSON strings because Neo4j properties cannot hold arbitrary nested maps. Frequently filtered fields are scalar/indexed; do not hide project/revision/time inside JSON. UTC timestamps use Neo4j datetime; hashes are SHA-256 of canonical normalized bytes.

## O3. Identity, duplication and versioning

Canonical keys use length-delimited normalized parts (or canonical JSON), then SHA-256. Never naive delimiter concatenation with unescaped user strings.

- Revision: project + repository ID + commit SHA.
- SourceFile: revision ID + normalized case-preserving repo path; case handling follows repo semantics.
- CodeSymbol: file ID + qualified name + kind + signature/location disambiguator.
- Evidence: project + connector + provider record ID; if provider lacks ID, hash stable source fields/event payload and record `dedupe_method=content_hash` with collision limitations.
- Extraction edge: build ID + source entity + relation + target entity + source locator; preserve distinct callsites.
- Manifest: sorted repository/revision/build entries. A project does not have one universal commit SHA.

Revision content is immutable. Re-indexing under another extractor version creates a new GraphBuild. Structural relationships carry build_id; shared immutable file/chunk nodes can be reused. Queries select exactly the build IDs in the run manifest to avoid mixing old/new extraction edges. An optional CORRESPONDS_TO mapping across revisions must include algorithm/evidence and uncertainty; matching names alone is not proof of identity.

## O4. Relationship schema and direction

| Relationship | From → To | Required relationship context |
|---|---|---|
| HAS_ENVIRONMENT | Project → Environment | project_id |
| HAS_SERVICE / HAS_RESOURCE / HAS_JOURNEY | Project → typed entity | project_id |
| HAS_REPOSITORY | Project → Repository | scope_source |
| HAS_REVISION | Repository → Revision | project_id |
| INDEXES | GraphBuild → Revision | project_id |
| SELECTS_BUILD | RevisionManifest → GraphBuild | repository_id |
| HAS_FILE | Revision → SourceFile | project_id |
| HAS_CHUNK | SourceFile → SourceChunk | project_id |
| DEFINES | SourceFile → CodeSymbol/SchemaEntity | build_id,source_locator |
| CALLS | caller symbol → callee symbol | build_id,derivation,source_locator,extractor_version |
| IMPORTS | importing file/symbol → imported file/symbol | same extraction fields |
| INHERITS_FROM | child type → parent type | same extraction fields |
| USES / CONTAINS | consumer/parent → reference/member | same extraction fields |
| MEMBER_OF | CodeSymbol → CodeCommunity | build_id |
| REFERENCES_SCHEMA | CodeSymbol/SchemaEntity → SchemaEntity | build_id,derivation,source_locator |
| IMPLEMENTS_SERVICE | Repository/CodeSymbol → Service | mapping_source,valid_from?,valid_to?,evidence_ids |
| DEPLOYS_REVISION | Deployment → Revision | mapping_method,evidence_ids |
| DEPLOYS_SERVICE | Deployment → Service | project_id |
| IN_ENVIRONMENT | Deployment/EvidenceRecord → Environment | mapping_method |
| DEPENDS_ON | consumer Service/Resource/Journey → provider Service/Resource | origin,derivation,environment_id,valid_from?,valid_to?,evidence_ids |
| OBSERVED_ON | EvidenceRecord → Service/Resource | mapping_method |
| ASSOCIATED_WITH | EvidenceRecord → Deployment/Change | mapping_method,evidence_ids |
| LOCATES | EvidenceRecord → CodeSymbol/SourceChunk | mapping_method,certainty,manifest_id |
| BEFORE_REVISION / AFTER_REVISION | Change → Revision | repository_id; absent before revision allowed for creation |
| BASE_REVISION / HEAD_REVISION / MERGED_AS | PullRequest → Revision | provider evidence; merged revision may differ from head |
| PROPOSED_AS | PatchVersion → PullRequest | operation_id,patch_hash; never assumes identical merge content |
| MODIFIES | Change → SourceFile/CodeSymbol/SchemaEntity | before/after_role,manifest_id |
| HAS_RUN | Ticket → InvestigationRun | request_actor_id |
| HAS_STEP / EXECUTED | InvestigationRun → Step; Step → ToolExecution | sequence |
| RETRIEVED | ToolExecution → EvidenceRecord/SourceChunk | manifest_id |
| CONSIDERS | InvestigationRun → Hypothesis | created_step |
| HAS_ASSESSMENT | Hypothesis → Assessment | sequence |
| SUPPORTED_BY / CONTRADICTED_BY | Assessment/ImpactAssessment → EvidenceRecord/SourceChunk | explanation,step_id |
| HAS_GAP | InvestigationRun/Assessment/VerificationRun → EvidenceGap | scope |
| PRODUCES / HAS_CLAIM | InvestigationRun → Finding; Finding → Claim | sequence |
| CITES | Claim/ConversationAnswer → EvidenceRecord/SourceChunk | locator,content_hash,retrieval_manifest_id |
| TARGETS | ImpactAssessment → Service/Resource/CodeSymbol/Journey | project_id |
| REQUESTS / ANSWERED_BY | InvestigationRun → HumanCheck; HumanCheck → HumanObservation | step_id/accepted_submission_id |
| ADDRESSES / HAS_PATCH | ResolutionPlan → Finding; ResolutionPlan → PatchVersion | version |
| TRACKS_PR / TRACKS_TICKET | ResolutionPlan → PullRequest/ExternalChangeTicket | operation_id |
| EVALUATES / OBSERVED_DEPLOYMENT | VerificationRun → ResolutionPlan/Deployment | criteria_version |
| REVIEWED_BY | Finding → Review | review version |

Store evidence_ids as references only when actual cited entities exist; complex dependency assertions needing conflicting sources should use an intermediate assertion entity or separate keyed edges, never merge opposite claims into a single unqualified fact.

## O5. Provenance and certainty

Keep independent dimensions: origin (code/configuration/provider_metadata/telemetry/human), derivation (extracted/resolved/inferred/ambiguous/declared), verification (unverified/corroborated/disputed), and assessment status. Retain original Graphify confidence string and version. EXTRACTED means source evidence of structure, not runtime observation or a causal probability. Unknown upstream fields preserve raw metadata in artifact and raise mapping warning.

Statements from tickets, code comments and previous model answers are not automatically trusted. Prior findings may guide retrieval but fresh claims cite primary evidence and identify historical analogy separately. Human evidence stores actor and claimed timestamp; no relabeling as provider telemetry. Browser-origin LogEvents remain stored with an explicit untrusted provenance marker, but investigator retrieval templates, bot event views and severity aggregates exclude them; a future reporter pathway must be separately labelled and authorized.

## O6. Time, deployments and historical views

Evidence filters use event time for incident relevance and ingestion cutoff for what was known. If event time missing, include only as unknown-time candidate with explicit label; never assign ingest time as fake event time. Source clocks/skew are recorded when known. Late evidence can appear in a new assessment/continuation, not the original historical step.

Dependencies valid at time t require valid_from<=t and (valid_to null or t<valid_to), with declared unknown validity excluded from confirmed temporal claims. Deployment overlap/gradual rollout permits several candidate revisions. Absence of log SHA requires a justified deployment mapping; otherwise annotate ambiguity. Change timestamp preceding failure is correlation, not proof. Query services within environment scope; cross-environment links require explicit authorized comparison mode.

Replay uses persisted steps, manifests and assessment versions without model calls. Rerun creates another run with a new cutoff. Source pinning preserves exact excerpts even after repository changes/disconnection.

## O7. Graphify normalization and publication

Map Graphify file/function/method/class concepts to SourceFile/CodeSymbol; source locators to exact file/line; calls/imports/inherits/uses/contains to allowlisted relationships; community to build-specific CodeCommunity. Extracted SQL/schema concepts can map SchemaEntity once adapter tests establish support. Unknown node types do not become arbitrary Neo4j labels; preserve as unmapped diagnostic and count coverage.

Validate node IDs, endpoints, relation direction, duplicate callsites, language coverage, source existence and path safety. Code-only extraction supplies no runtime infra truth. Load staging batches with build_id; validate counts and mandatory fields; mark published only after all required writes complete. RevisionManifest selects published builds. Readers never mix staging data into completed findings. Keep exact source chunks in Neo4j; `graph.html` is not authoritative evidence.

## O8. Retrieval tools and ranking

Search stages: exact request/error/service/path/commit match → full-text candidate retrieval → typed neighborhood expansion → time/revision filtering → evidence diversity/ranking → bounded excerpt assembly. Optional semantic vector search is a later evaluated enhancement within Neo4j, not required for initial GraphRAG. Do not let community popularity dominate incident relevance.

Rank exact identity/revision/time match first, primary observations ahead of interpretations, fresh applicable evidence ahead of stale; preserve contradictory evidence rather than returning only support. Deduplicate identical event hashes and overlapping chunks but retain independent source counts. RetrievalManifest records selected IDs/hashes/locators, query template version, authorized scope, cutoff, truncation and gaps.

Default two hops, ceiling four; max 100 nodes/200 edges per expansion. Relation-direction rules are query-specific. Cycles visited once per candidate path budget; return bounded paths, not just reachability count. A returned shortest path is structural connectivity, not causal proof. Query timeout and result truncation become gaps in report.

## O9. Parameterized Cypher examples

Illustrative query templates require integration tests against chosen Neo4j version. No raw model Cypher. Inputs already authorized by backend.

```cypher
// Exact source excerpt candidates at one repository revision.
MATCH (f:Entity:SourceFile {project_id:$project_id, revision_id:$revision_id, path:$path})
      -[:HAS_CHUNK]->(c:Entity:SourceChunk)
WHERE c.project_id=$project_id AND c.start_line <= $end_line AND c.end_line >= $start_line
RETURN c.id,c.content,c.start_line,c.end_line,c.content_hash
ORDER BY c.start_line LIMIT $limit
```

```cypher
// Two-hop callers; use separate fixed templates for other validated depths.
MATCH p=(caller:Entity:CodeSymbol)-[:CALLS*1..2]->(seed:Entity:CodeSymbol {id:$seed_id,project_id:$project_id})
WHERE all(n IN nodes(p) WHERE n.project_id=$project_id)
  AND all(r IN relationships(p) WHERE r.build_id IN $build_ids)
RETURN p LIMIT $path_limit
```

```cypher
// Runtime observations known at investigation cutoff.
MATCH (e:Entity:EvidenceRecord)-[:OBSERVED_ON]->(s:Entity:Service {id:$service_id,project_id:$project_id})
WHERE e.project_id=$project_id AND e.environment_id=$environment_id
  AND e.event_time >= datetime($window_start) AND e.event_time < datetime($window_end)
  AND e.ingested_at <= datetime($cutoff)
RETURN e ORDER BY e.event_time LIMIT $limit
```

Constraints/index migration sketch:

```cypher
CREATE CONSTRAINT entity_id_unique IF NOT EXISTS FOR (n:Entity) REQUIRE n.id IS UNIQUE;
CREATE INDEX evidence_scope_time IF NOT EXISTS FOR (n:EvidenceRecord) ON (n.project_id,n.environment_id,n.event_time);
CREATE INDEX file_revision_path IF NOT EXISTS FOR (n:SourceFile) ON (n.project_id,n.revision_id,n.path);
CREATE INDEX run_project IF NOT EXISTS FOR (n:InvestigationRun) ON (n.project_id,n.control_run_id);
CREATE FULLTEXT INDEX source_text IF NOT EXISTS FOR (n:SourceChunk) ON EACH [n.content];
```

Full-text candidates must be scope-filtered before any result leaves graph repository; global candidate ranking may reduce recall, so continue bounded pagination or use scoped search strategy and report truncation. Indexes are performance aids, not access controls. Production migrations version these declarations and validate compatibility.

## O10. Multi-hop recipes and stopping rules

| Question | Allowed traversal | Required caution |
|---|---|---|
| Locate error | LogEvent → Deployment → Revision → SourceChunk/Symbol | evidence mapping mandatory; not latest branch |
| Find relevant change | Service ← Deployment → Revision ← Change mapping | temporal proximity isn't causality |
| Potential exposure | changed Symbol ← CALLS/IMPORTS/USES ← consumers → mapped Services | incoming direction; static path not observed outage |
| Observed impact | candidate Service ← EvidenceRecord within scoped window | sufficient relevant degradation; missing telemetry unknown |
| Contradict hypothesis | Hypothesis → Assessment → contradicting observations | actively seek alternatives/negative tests |
| Verify recovery | PatchVersion → PullRequest → MERGED_AS Revision ← DEPLOYS_REVISION Deployment ← ASSOCIATED_WITH EvidenceRecord | same definition/window coverage and enough traffic |

Stop expansion when no relevant new evidence, unresolved revision boundary, max hops/result/time/token budget, unsupported relationship, or missing scope. Return reason and next useful check. Avoid exhaustive all-path traversal. Relationship uncertainty propagates into path explanation; don't average it into a pseudo confidence percentage.

## O11. Impact, gaps and resolution semantics

Impact categories are a set: direct_exposure, potential_propagation, observed_impact, unknown. A service may be directly changed and observed failing. Unknown specifically describes uncovered dimensions; do not suppress existing observations. Every classification cites path/source and environment/window. Healthy requires a defined check with coverage; no errors in absent telemetry is not healthy.

EvidenceGap kinds: source_disconnected, permission_missing, rate_limited, stale, partial_parse, ambiguous_revision, unsupported_language, unresolved_dependency, truncated_query, insufficient_traffic, missing_baseline, redacted_content, and for telemetry intake (A10) rejected_input, oversize_input, truncated_input, unmapped_scope. Preserve detection/resolution time and source job. Closing gap does not rewrite old assessments.

Findings split factual Claim nodes and recommendations. Every factual claim cites retrieved primary evidence; unsupported statements become explicit uncertainty or are rejected. Resolution plan references finding and exact base manifest. External approvals remain control-plane facts linked by patch hash; patch edits create new version. Verification uses explicit criteria/version/window and traffic coverage. No merger-to-recovery shortcut.

## O12. Retention and semantic acceptance tests

Graph schema changes have migration version, backfill plan, compatibility with retained runs and rollback strategy. Do not reinterpret old labels silently. Retained citations pin content/hash and build manifest. Removing active project access never grants access to retained backups. Dedupe record has provider ID/hash strategy.

Test identities across projects/repos/revisions; unknown deployment vs pinned mapping; overlapping deployments; late/out-of-order events; Graphify ambiguous edge; partial language parse; reverse-direction blast; graph cycle/limits; human evidence; conflicting sources; source path mapping; historical replay cutoff; citation hash stability; insufficient recovery traffic; full-text result isolation. Each fixture must assert both correct output and excluded misleading inference. Evaluation uses expected facts curated independently of model-generated answers.
