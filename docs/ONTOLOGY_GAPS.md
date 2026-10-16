# Ontology gap audit (TRACE/ontology.md vs `app/graph`)

Audited 2026-10-17 against a live Neo4j Aura instance. "Enforced" means a test fails if the rule is broken.

## Covered and enforced
| Spec | Where | Test |
|---|---|---|
| O1 universal fields, `Entity` base label, project on every node | `ontology.COMMON_FIELDS`, `validation.validate_node` | test_graph_ontology |
| O1 unknown is null plus key present, never a default (`event_time`, `Deployment.revision_id`, `Measurement.value`) | `NULLABLE_REQUIRED`, validator | event_time / measurement / deployment tests |
| O1 scope relationship and scalar scope field must agree | `SCOPE_AGREEMENT`, `GraphRepository._check_scope_and_evidence` | test_scope_relationship_must_agree… (Neo4j) |
| O2 all 38 labels with required fields; evidence subtypes carry `EvidenceRecord` | `NODES` | consistency tests |
| O3 length-delimited SHA-256 ids, project-scoped, case-preserving paths, traversal rejected, manifest order-independent, distinct call sites | `identity.py` | identity tests |
| O3 revision content immutable; rewrite with different content refused, harmless re-ingest allowed | `GraphRepository.write_nodes` (`_fp` fingerprint) | immutable / re-ingest tests (Neo4j) |
| O4 all relationships with endpoint labels and required context; closed property set (no invented confidence) | `RELATIONSHIPS`, `validate_relationship` | edge tests |
| O4 `evidence_ids` must reference real entities | `_check_scope_and_evidence` | test_evidence_ids_must_exist (Neo4j) |
| O4 cross-project edges refused; node id cannot move project | `write_*` | isolation tests (Neo4j) |
| O5 origin / derivation / verification as independent vocabularies; Graphify confidence string kept verbatim (`graphify_confidence`) | `ORIGINS`, `DERIVATIONS`, `VERIFICATIONS`, `_EXTRACTION_OPT` | vocabulary tests |
| O6 event time vs ingest cutoff; unknown-time evidence kept off the timeline; dependency validity at an instant | `runtime_observations`, `unknown_time_observations`, `dependencies_valid_at` | Neo4j tests |
| O7 staging → published builds; manifest selects only published builds; queries take explicit `build_ids` | `set_build_status`, `create_manifest`, `manifest_build_ids` | test_manifest_only_selects_published_builds (Neo4j) |
| O8 hop default 2, ceiling 4, bounded limits, incoming-direction callers | `queries.py` | template tests, hop/build Neo4j test |
| O9 constraints and indexes, versioned and checksum-guarded | `migrations/graph`, `app/graph/migrate.py` | idempotent migration test |
| O10 recipes: locate source, deployments, potential exposure, runtime observations, contradiction retrieval | six templates | template tests |
| O11 impact categories as a set, gap kinds, claim kinds, verification outcomes | vocabularies | enum tests |
| O11 / O5 citations must point at items in the retrieval manifest | `check_citations` | test_citations_outside_the_retrieval_manifest (Neo4j) |

## Found missing and fixed in this pass
- Scope agreement and `evidence_ids` existence were documented but not checked.
- No build lifecycle: manifests could have selected unpublished data.
- No `graphify_confidence` / `verification` on structural edges; no `source_locator` / clock-skew on evidence.
- No contradiction, potential-exposure or dependency-validity templates.
- No citation check against the retrieval manifest.

## Closed in the second pass (2026-10-17)
| Gap | Now |
|---|---|
| Graphify output normaliser | `app/graph/extract.py` (`Extractor` protocol; built-in ast/regex implementation, honest about inferred vs resolved vs ambiguous) and `app/graph/indexer.py` (stage, validate counts, publish, manifest). A Graphify adapter can replace the extractor later. |
| Full-text search with scope filter and truncation | `source_text_search` template, `escape_lucene`, `run_bounded` returns `truncated` |
| 100 node / 200 edge expansion ceiling | `MAX_NODES` enforced for expansion templates |
| RetrievalManifest builder | `GraphRepository.record_retrieval` (idempotent, query template recorded) |
| O12 fixtures | ambiguous call, same-file resolution, partial parse, unsupported language, cycle, reverse-direction blast, failed rebuild, cross-project paths, full-text isolation + truncation, overlapping deployments, conflicting sources, retrieval manifest |
| Read path for the app | `GET /projects/{id}/graph/code` and `/graph/symbols/{symbol}` (membership-checked, published build only, bounded), shown in trace-web at `/graph` |

## Still open
| Gap | Spec | Task |
|---|---|---|
| Real Graphify adapter and output schema validation (the built-in extractor is a stand-in; TypeScript parsing is regex-based, so nested/complex syntax can be missed) | O7 | T-001 / T-007 |
| Safe checkout of a GitHub repository at a SHA (today the indexer reads a local directory) | R-017 | T-007 |
| Postgres outbox -> graph applier with receipts and reconciliation; project/service/environment nodes are not yet mirrored from the control plane | A2 | T-006 |
| Ranking, event-hash dedupe and diversity across retrieved items | O8 | T-011 |
| Late-evidence rule and replay without model calls | O6 | T-012 |
| Fixtures not yet written: late/out-of-order ingest through the real ingestion path, source path mapping from stack traces, insufficient recovery traffic | O12 | T-009 / T-019 |
| Retention and backup behaviour | O12 | T-024 |
