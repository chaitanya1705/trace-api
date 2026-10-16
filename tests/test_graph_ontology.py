"""Ontology, identity, validation and query-template contract tests. Pure Python: no Neo4j needed."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.graph import identity as I
from app.graph import ontology as O
from app.graph import queries as Q
from app.graph.migrate import MigrationError, load_migrations, split_statements
from app.graph.repository import fingerprint, prepare_node, prepare_rel
from app.graph.validation import GraphValidationError, validate_node, validate_relationship

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
P = "proj-1"


# ---- ontology is internally consistent ------------------------------------------------------------------------
def test_every_relationship_endpoint_is_a_known_label():
    known = set(O.NODES)
    for r in O.RELATIONSHIPS.values():
        assert r.sources <= known, r.type
        assert r.targets <= known, r.type


def test_every_node_label_has_an_identity_prefix():
    for label in O.NODES:
        I.entity_id(label, P, "x")  # raises if no rule


def test_required_fields_are_declared_fields():
    for s in O.NODES.values():
        assert set(s.required) <= s.fields, s.label
        assert set(O.NULLABLE_REQUIRED.get(s.label, ())) <= s.fields, s.label


def test_extraction_relationships_carry_build_and_locator():
    for t in ("CALLS", "IMPORTS", "INHERITS_FROM", "USES", "CONTAINS"):
        assert {"build_id", "derivation", "source_locator", "extractor_version"} <= set(O.RELATIONSHIPS[t].required)


def test_evidence_subtypes_carry_both_labels():
    for sub in ("LogEvent", "Measurement", "RequestSpan", "HumanObservation"):
        assert O.NODES[sub].labels == ("Entity", "EvidenceRecord", sub)
        assert O.NODES[sub].immutable


# ---- identities ------------------------------------------------------------------------------------------------
def test_ids_are_deterministic_and_project_scoped():
    a = I.revision_id("p1", "rep_1", "ABC123")
    assert a == I.revision_id("p1", "rep_1", "abc123")  # sha is case-insensitive
    assert a != I.revision_id("p2", "rep_1", "abc123")  # same commit, other project
    assert a.startswith("rev_")


def test_length_delimiting_prevents_collisions():
    assert I.canonical_digest("ab", "c") != I.canonical_digest("a", "bc")
    assert I.canonical_digest("a:b", "c") != I.canonical_digest("a", "b:c")
    assert I.canonical_digest(1) != I.canonical_digest("1")


def test_none_part_is_rejected():
    with pytest.raises(ValueError):
        I.canonical_digest("a", None)  # type: ignore[arg-type]


def test_same_symbol_name_in_two_files_or_revisions_is_distinct():
    f1 = I.source_file_id(P, "rev_a", "src/a.py")
    f2 = I.source_file_id(P, "rev_b", "src/a.py")
    assert f1 != f2
    s1 = I.code_symbol_id(P, f1, "pkg.f", "function", 10)
    s2 = I.code_symbol_id(P, f2, "pkg.f", "function", 10)
    s3 = I.code_symbol_id(P, f1, "pkg.f", "function", 40)  # overload at another location
    assert len({s1, s2, s3}) == 3


def test_paths_normalise_but_keep_case_and_reject_traversal():
    assert I.normalize_path("./src\\Pay/client.ts") == "src/Pay/client.ts"
    assert I.source_file_id(P, "r", "Src/A.ts") != I.source_file_id(P, "r", "src/a.ts")
    with pytest.raises(ValueError):
        I.normalize_path("../etc/passwd")
    with pytest.raises(ValueError):
        I.normalize_path("")


def test_manifest_hash_ignores_entry_order():
    e1 = {"repository_id": "rep_a", "revision_id": "rev_1", "build_id": "bld_1"}
    e2 = {"repository_id": "rep_b", "revision_id": "rev_2", "build_id": "bld_2"}
    assert I.manifest_hash([e1, e2]) == I.manifest_hash([e2, e1])
    assert I.manifest_hash([e1]) != I.manifest_hash([e1, e2])


def test_extraction_edges_keep_distinct_call_sites():
    k1 = I.extraction_edge_key("bld", "sym_a", "CALLS", "sym_b", "src/a.py:10")
    k2 = I.extraction_edge_key("bld", "sym_a", "CALLS", "sym_b", "src/a.py:22")
    k3 = I.extraction_edge_key("bld2", "sym_a", "CALLS", "sym_b", "src/a.py:10")
    assert len({k1, k2, k3}) == 3


def test_content_dedupe_key_reports_its_method():
    key, method = I.evidence_key_from_content(["svc", "2026-10-12T12:00:00Z", "boom"])
    assert key.startswith("ch_") and method == "content_hash"


def test_content_hash_is_exact_bytes():
    assert I.content_hash("a\r\nb") != I.content_hash("a\nb")
    assert I.content_hash("é") == I.content_hash("é".encode())


# ---- node validation -------------------------------------------------------------------------------------------
def _service(**kw):
    base = {"id": I.service_id(P, "payment"), "project_id": P, "schema_version": 1, "created_at": NOW, "name": "payment-service", "service_key": "payment"}
    base.update(kw)
    return base


def test_valid_service_passes():
    out = validate_node("Service", _service())
    assert out["service_key"] == "payment"


def test_unknown_label_and_field_are_rejected():
    with pytest.raises(GraphValidationError):
        validate_node("Banana", {})
    with pytest.raises(GraphValidationError) as e:
        validate_node("Service", _service(colour="red"))
    assert "Service.colour" in str(e.value)


def test_missing_required_is_reported_by_name_without_values():
    bad = _service(name="")
    with pytest.raises(GraphValidationError) as e:
        validate_node("Service", bad)
    assert "Service.name" in str(e.value) and "payment-service" not in str(e.value)


def test_naive_datetime_is_rejected_and_aware_one_is_converted_to_utc():
    with pytest.raises(GraphValidationError):
        validate_node("Service", _service(created_at=datetime(2026, 1, 1)))
    out = validate_node("Service", _service(created_at=datetime(2026, 1, 1, 5, tzinfo=timezone(timedelta(hours=5)))))
    assert out["created_at"] == datetime(2026, 1, 1, 0, tzinfo=UTC)


def test_nested_maps_must_be_json_strings():
    with pytest.raises(GraphValidationError):
        validate_node("GraphBuild", {"id": "bld_1", "project_id": P, "schema_version": 1, "created_at": NOW, "revision_id": "r", "extractor_version": "1", "output_schema_version": "1", "ontology_version": "1.0", "status": "ok", "counts": {"nodes": 3}})


def _log(**kw):
    base = {
        "id": "evd_1", "project_id": P, "schema_version": 1, "created_at": NOW, "source_connection_id": "con_1", "source_record_key": "k1",
        "ingested_at": NOW, "origin": "telemetry", "content_hash": I.content_hash("boom"), "redacted_content": "boom", "severity": "error",
        "message": "boom", "event_time": NOW,
    }
    base.update(kw)
    return base


def test_log_event_requires_event_time_key_but_allows_unknown():
    assert "event_time" not in validate_node("LogEvent", _log(event_time=None))  # unknown time stays unknown
    bad = _log()
    del bad["event_time"]
    with pytest.raises(GraphValidationError):
        validate_node("LogEvent", bad)


def test_origin_must_be_in_the_vocabulary():
    with pytest.raises(GraphValidationError):
        validate_node("LogEvent", _log(origin="vibes"))


def test_measurement_value_is_required_no_zero_default():
    m = _log(metric="latency", unit="ms", value=None, aggregation="p50", window_start=NOW, window_end=NOW + timedelta(minutes=1))
    del m["severity"], m["message"]
    with pytest.raises(GraphValidationError) as e:
        validate_node("Measurement", m)
    assert "Measurement.value" in str(e.value)


def test_gap_kind_and_assessment_status_vocabularies():
    gap = {"id": "gap_1", "project_id": P, "schema_version": 1, "created_at": NOW, "kind": "made_up", "description": "x", "scope_json": "{}", "detected_at": NOW, "status": "open"}
    with pytest.raises(GraphValidationError):
        validate_node("EvidenceGap", gap)
    gap["kind"] = "stale"
    validate_node("EvidenceGap", gap)


def test_impact_assessment_needs_run_or_change_and_valid_categories():
    base = {"id": "imp_1", "project_id": P, "schema_version": 1, "created_at": NOW, "target_id": "svc_1", "categories": ["direct_exposure", "observed_impact"], "scope_json": "{}", "assessed_at": NOW}
    with pytest.raises(GraphValidationError):
        validate_node("ImpactAssessment", base)
    validate_node("ImpactAssessment", {**base, "run_id": "run_1"})
    with pytest.raises(GraphValidationError):
        validate_node("ImpactAssessment", {**base, "run_id": "run_1", "categories": ["catastrophic"]})


def test_deployment_may_have_unknown_revision():
    d = {"id": "dep_1", "project_id": P, "schema_version": 1, "created_at": NOW, "service_id": "svc_1", "environment_id": "env_1", "provider_id": "dpl_9", "status": "ready", "event_time": NOW, "exposure_status": "full", "revision_id": None}
    assert "revision_id" not in validate_node("Deployment", d)


# ---- relationship validation ----------------------------------------------------------------------------------
def _calls(**kw):
    base = {"project_id": P, "key": "k", "build_id": "bld_1", "derivation": "extracted", "source_locator": "a.py:3", "extractor_version": "1"}
    base.update(kw)
    return base


def test_valid_call_edge_and_wrong_endpoints():
    validate_relationship("CALLS", "CodeSymbol", "CodeSymbol", _calls())
    with pytest.raises(GraphValidationError):
        validate_relationship("CALLS", "Service", "CodeSymbol", _calls())


def test_log_event_counts_as_evidence_record_for_relationships():
    validate_relationship("OBSERVED_ON", "LogEvent", "Service", {"project_id": P, "key": "k", "mapping_method": "service_tag"})


def test_edge_context_is_required_and_closed():
    bad = _calls()
    del bad["source_locator"]
    with pytest.raises(GraphValidationError):
        validate_relationship("CALLS", "CodeSymbol", "CodeSymbol", bad)
    with pytest.raises(GraphValidationError):
        validate_relationship("CALLS", "CodeSymbol", "CodeSymbol", _calls(confidence=0.9))  # no invented probability
    with pytest.raises(GraphValidationError):
        validate_relationship("CALLS", "CodeSymbol", "CodeSymbol", _calls(derivation="guessed"))


def test_every_relationship_requires_project_id():
    with pytest.raises(GraphValidationError):
        validate_relationship("HAS_SERVICE", "Project", "Service", {"key": "k"})


def test_prepare_helpers_fill_common_fields():
    n = prepare_node("Service", I.service_id(P, "pay"), P, {"name": "pay", "service_key": "pay"}, now=NOW)
    assert n.props["schema_version"] == O.SCHEMA_VERSION and n.props["created_at"] == NOW
    r = prepare_rel("HAS_SERVICE", "Project", "prj_1", "Service", n.id, P, I.edge_key("HAS_SERVICE", "prj_1", n.id))
    assert r.props["project_id"] == P


def test_fingerprint_ignores_volatile_fields_only():
    a = {"content_hash": "h", "created_at": NOW, "ingested_at": NOW, "message": "m"}
    b = {"content_hash": "h", "created_at": NOW + timedelta(days=1), "ingested_at": NOW + timedelta(days=1), "message": "m"}
    c = {**a, "message": "different"}
    assert fingerprint(a) == fingerprint(b) and fingerprint(a) != fingerprint(c)


# ---- migrations and query templates ---------------------------------------------------------------------------
def test_migration_files_load_and_are_contiguous():
    ms = load_migrations()
    assert [m.version for m in ms] == list(range(1, len(ms) + 1))
    assert all(m.statements for m in ms)
    text = " ".join(" ".join(m.statements) for m in ms)
    assert "entity_id_unique" in text and "FULLTEXT" in text


def test_split_statements_ignores_comments_and_blank_parts():
    assert split_statements("// c\nCREATE A;\n\n// d\nCREATE B;\n") == ("CREATE A", "CREATE B")


def test_migration_rejects_bad_names(tmp_path):
    (tmp_path / "bad.cypher").write_text("RETURN 1;")
    with pytest.raises(MigrationError):
        load_migrations(tmp_path)


def test_every_template_is_scoped_and_bounded():
    for t in Q.TEMPLATES.values():
        assert "$project_id" in t.cypher, t.name
        assert "LIMIT $limit" in t.cypher, t.name
        assert {"project_id", "limit"} <= t.params, t.name
        for p in t.params:
            assert f"${p}" in t.cypher, (t.name, p)


def test_caller_templates_stay_within_hop_ceiling_and_filter_builds():
    assert [n for n in Q.TEMPLATES if n.startswith("callers_")] == ["callers_1hop", "callers_2hop", "callers_3hop", "callers_4hop"]
    assert "build_id IN $build_ids" in Q.TEMPLATES["callers_2hop"].cypher
    with pytest.raises(ValueError):
        Q._caller_paths(5)


def test_template_parameter_checks():
    good = {"project_id": P, "run_id": "r", "limit": 10}
    assert Q.check_params("open_gaps_for_run", good).ref == "open_gaps_for_run@1"
    for bad in ({**good, "limit": 0}, {**good, "limit": 999}, {**good, "project_id": ""}, {k: v for k, v in good.items() if k != "run_id"}, {**good, "cypher": "MATCH (n) RETURN n"}):
        with pytest.raises(Q.TemplateError):
            Q.check_params("open_gaps_for_run", bad)
    with pytest.raises(Q.TemplateError):
        Q.check_params("raw_cypher", good)
