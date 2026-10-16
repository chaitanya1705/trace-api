"""Integration tests against a real Neo4j. Opt-in: set TRACE_TEST_NEO4J=1 (credentials come from .env or TEST_NEO4J_*).

Each test uses throwaway project ids (`itest-<uuid>`) and deletes everything it wrote, so it is safe against a shared
instance. Normal `pytest` runs never touch Neo4j.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from dotenv import dotenv_values

from app.graph import identity as I
from app.graph.migrate import apply_migrations
from app.graph.queries import run_template
from app.graph.repository import GraphRepository, GraphWriteError, prepare_node, prepare_rel

pytestmark = pytest.mark.skipif(os.environ.get("TRACE_TEST_NEO4J") != "1", reason="set TRACE_TEST_NEO4J=1 to run against Neo4j")

T0 = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def _creds():
    env = {**dotenv_values(Path(__file__).resolve().parents[1] / ".env"), **os.environ}
    pick = lambda k: env.get("TEST_" + k) or env.get(k)  # noqa: E731
    return pick("NEO4J_URI"), pick("NEO4J_USERNAME"), pick("NEO4J_PASSWORD"), pick("NEO4J_DATABASE") or "neo4j"


@pytest.fixture(scope="module")
def driver():
    from neo4j import GraphDatabase

    uri, user, pw, _ = _creds()
    d = GraphDatabase.driver(uri, auth=(user, pw))
    d.verify_connectivity()
    apply_migrations(d, _creds()[3])
    yield d
    d.close()


@pytest.fixture
def repo(driver):
    return GraphRepository(driver, _creds()[3])


@pytest.fixture
def pid(repo):
    p = "itest-" + uuid.uuid4().hex[:10]
    yield p
    repo.delete_project(p)


def _build(repo: GraphRepository, p: str) -> dict[str, str]:
    """Project -> repo -> revision -> file -> chunk -> three symbols calling each other (c -> b -> a)."""
    ids: dict[str, str] = {}
    ids["project"] = I.project_node_id(p)
    ids["env"] = I.environment_id(p, "production")
    ids["svc"] = I.service_id(p, "payment")
    ids["repo"] = I.repository_id(p, "github", "123")
    ids["rev"] = I.revision_id(p, ids["repo"], "a3f9c1e")
    ids["build"] = I.graph_build_id(p, ids["rev"], "graphify-1", "1", 1)
    ids["file"] = I.source_file_id(p, ids["rev"], "src/payment/client.ts")
    code = "export async function authorize() {\n  return 1;\n}\n"
    ids["chunk"] = I.source_chunk_id(p, ids["file"], 1, 3, I.content_hash(code))
    nodes = [
        prepare_node("Project", ids["project"], p, {"organization_id": "org", "name": "Payments"}),
        prepare_node("Environment", ids["env"], p, {"name": "production", "environment_key": "production"}),
        prepare_node("Service", ids["svc"], p, {"name": "payment-service", "service_key": "payment"}),
        prepare_node("Repository", ids["repo"], p, {"provider": "github", "provider_repository_id": "123", "canonical_url": "https://github.com/acme/payments"}),
        prepare_node("Revision", ids["rev"], p, {"repository_id": ids["repo"], "commit_sha": "a3f9c1e"}),
        prepare_node("GraphBuild", ids["build"], p, {"revision_id": ids["rev"], "extractor_version": "graphify-1", "output_schema_version": "1", "ontology_version": "1.0", "status": "published", "counts": '{"nodes":3}'}),
        prepare_node("SourceFile", ids["file"], p, {"revision_id": ids["rev"], "path": "src/payment/client.ts", "content_hash": I.content_hash(code), "language": "typescript"}),
        prepare_node("SourceChunk", ids["chunk"], p, {"file_id": ids["file"], "start_line": 1, "end_line": 3, "content": code, "content_hash": I.content_hash(code)}),
    ]
    for name, line in (("a", 1), ("b", 10), ("c", 20)):
        sid = I.code_symbol_id(p, ids["file"], f"pkg.{name}", "function", line)
        ids["sym_" + name] = sid
        nodes.append(prepare_node("CodeSymbol", sid, p, {"file_id": ids["file"], "qualified_name": f"pkg.{name}", "kind": "function", "start_line": line, "end_line": line + 5, "build_id": ids["build"]}))
    assert repo.write_nodes(nodes) == len(nodes)

    def rel(t, fl, f, tl, to, **props):
        return prepare_rel(t, fl, ids[f], tl, ids[to], p, I.edge_key(t, ids[f], ids[to], *props.values()), props)

    ctx = {"build_id": ids["build"], "derivation": "extracted", "extractor_version": "graphify-1"}
    rels = [
        rel("HAS_ENVIRONMENT", "Project", "project", "Environment", "env"),
        rel("HAS_SERVICE", "Project", "project", "Service", "svc"),
        rel("HAS_FILE", "Revision", "rev", "SourceFile", "file"),
        rel("HAS_CHUNK", "SourceFile", "file", "SourceChunk", "chunk"),
        rel("CALLS", "CodeSymbol", "sym_c", "CodeSymbol", "sym_b", source_locator="src/payment/client.ts:21", **ctx),
        rel("CALLS", "CodeSymbol", "sym_b", "CodeSymbol", "sym_a", source_locator="src/payment/client.ts:11", **ctx),
    ]
    assert repo.write_relationships(rels) == len(rels)
    return ids


def test_migrations_are_idempotent(driver):
    assert apply_migrations(driver, _creds()[3]) == []


def test_write_is_idempotent_and_counts_match(repo, pid):
    ids = _build(repo, pid)
    before = repo.counts(pid)
    _build(repo, pid)  # same ids, same content: no duplicates
    assert repo.counts(pid) == before
    assert before["CodeSymbol"] == 3 and before["SourceChunk"] == 1
    assert ids["sym_a"].startswith("sym_")


def test_immutable_node_cannot_be_rewritten_with_other_content(repo, pid):
    ids = _build(repo, pid)
    tampered = prepare_node("SourceChunk", ids["chunk"], pid, {"file_id": ids["file"], "start_line": 1, "end_line": 3, "content": "// edited\n", "content_hash": I.content_hash("// edited\n")})
    with pytest.raises(GraphWriteError):
        repo.write_nodes([tampered])


def test_a_node_id_cannot_move_to_another_project(repo, pid):
    ids = _build(repo, pid)
    other = pid + "-b"
    try:
        stolen = prepare_node("Service", ids["svc"], other, {"name": "x", "service_key": "payment"})
        with pytest.raises(GraphWriteError):
            repo.write_nodes([stolen])
    finally:
        repo.delete_project(other)


def test_relationships_cannot_cross_projects(repo, pid):
    ids = _build(repo, pid)
    other = pid + "-b"
    try:
        o_svc = I.service_id(other, "payment")
        repo.write_nodes([prepare_node("Service", o_svc, other, {"name": "other", "service_key": "payment"})])
        crossing = prepare_rel("HAS_SERVICE", "Project", ids["project"], "Service", o_svc, pid, I.edge_key("HAS_SERVICE", ids["project"], o_svc))
        with pytest.raises(GraphWriteError):
            repo.write_relationships([crossing])
    finally:
        repo.delete_project(other)


def test_source_excerpt_template_is_exact_and_scoped(repo, driver, pid):
    ids = _build(repo, pid)
    db = _creds()[3]
    rows = run_template(driver, db, "source_excerpt", project_id=pid, revision_id=ids["rev"], path="src/payment/client.ts", start_line=2, end_line=2, limit=10)
    assert len(rows) == 1 and rows[0]["content_hash"] == I.content_hash(rows[0]["content"])
    assert run_template(driver, db, "source_excerpt", project_id=pid + "-nope", revision_id=ids["rev"], path="src/payment/client.ts", start_line=1, end_line=3, limit=10) == []


def test_callers_follow_incoming_calls_and_respect_hops_and_build(repo, driver, pid):
    ids = _build(repo, pid)
    db = _creds()[3]
    one = run_template(driver, db, "callers_1hop", project_id=pid, seed_id=ids["sym_a"], build_ids=[ids["build"]], limit=50)
    two = run_template(driver, db, "callers_2hop", project_id=pid, seed_id=ids["sym_a"], build_ids=[ids["build"]], limit=50)
    assert {p["nodes"][0] for p in one} == {ids["sym_b"]}
    assert {p["nodes"][0] for p in two} == {ids["sym_b"], ids["sym_c"]}
    assert run_template(driver, db, "callers_2hop", project_id=pid, seed_id=ids["sym_a"], build_ids=["bld_other"], limit=50) == []


def _evidence(repo, ids, pid, key, event_time, ingested_at, msg="boom"):
    eid = I.evidence_id(pid, "con_1", key)
    repo.write_nodes([prepare_node("LogEvent", eid, pid, {
        "source_connection_id": "con_1", "source_record_key": key, "ingested_at": ingested_at, "origin": "telemetry", "content_hash": I.content_hash(msg),
        "redacted_content": msg, "severity": "error", "message": msg, "event_time": event_time, "environment_id": ids["env"], "service_id": ids["svc"],
    })])
    repo.write_relationships([prepare_rel("OBSERVED_ON", "LogEvent", eid, "Service", ids["svc"], pid, I.edge_key("OBSERVED_ON", eid, ids["svc"]), {"mapping_method": "service_tag"})])
    return eid


def test_runtime_observations_use_event_time_and_cutoff(repo, driver, pid):
    ids = _build(repo, pid)
    db = _creds()[3]
    inside = _evidence(repo, ids, pid, "in", T0 + timedelta(minutes=5), T0 + timedelta(minutes=6))
    _evidence(repo, ids, pid, "before-window", T0 - timedelta(hours=1), T0 + timedelta(minutes=6))
    _evidence(repo, ids, pid, "late", T0 + timedelta(minutes=7), T0 + timedelta(hours=2))  # ingested after the cutoff
    unknown = _evidence(repo, ids, pid, "no-time", None, T0 + timedelta(minutes=6))
    args = dict(project_id=pid, service_id=ids["svc"], environment_id=ids["env"], cutoff=(T0 + timedelta(minutes=30)).isoformat(), limit=50)
    got = run_template(driver, db, "runtime_observations", window_start=T0.isoformat(), window_end=(T0 + timedelta(minutes=20)).isoformat(), **args)
    assert [r["id"] for r in got] == [inside]  # unknown time never lands on the timeline; late evidence is excluded
    unk = run_template(driver, db, "unknown_time_observations", **args)
    assert [r["id"] for r in unk] == [unknown]


def test_same_evidence_reingested_is_a_noop_but_changed_content_is_refused(repo, pid):
    ids = _build(repo, pid)
    _evidence(repo, ids, pid, "k", T0, T0 + timedelta(seconds=1))
    _evidence(repo, ids, pid, "k", T0, T0 + timedelta(hours=1))  # same record later: only ingest time differs
    assert repo.counts(pid)["LogEvent"] == 1
    with pytest.raises(GraphWriteError):
        _evidence(repo, ids, pid, "k", T0, T0 + timedelta(seconds=1), msg="a different message")


def test_delete_project_removes_only_that_project(repo, pid):
    _build(repo, pid)
    other = pid + "-b"
    try:
        _build(repo, other)
        assert repo.delete_project(pid) > 0
        assert repo.counts(pid) == {} and repo.counts(other) != {}
    finally:
        repo.delete_project(other)


# ---- ontology gap checks: rules that were documented but not yet enforced -----------------------------------------
def test_scope_relationship_must_agree_with_scalar_scope_field(repo, pid):
    ids = _build(repo, pid)
    other_env = I.environment_id(pid, "staging")
    repo.write_nodes([prepare_node("Environment", other_env, pid, {"name": "staging", "environment_key": "staging"})])
    eid = _evidence(repo, ids, pid, "scoped", T0, T0 + timedelta(seconds=1))  # node says environment_id = production
    bad = prepare_rel("IN_ENVIRONMENT", "LogEvent", eid, "Environment", other_env, pid, I.edge_key("IN_ENVIRONMENT", eid, other_env), {"mapping_method": "tag"})
    with pytest.raises(GraphWriteError):
        repo.write_relationships([bad])
    good = prepare_rel("IN_ENVIRONMENT", "LogEvent", eid, "Environment", ids["env"], pid, I.edge_key("IN_ENVIRONMENT", eid, ids["env"]), {"mapping_method": "tag"})
    assert repo.write_relationships([good]) == 1


def test_evidence_ids_must_exist(repo, pid):
    ids = _build(repo, pid)
    dep = I.entity_id("Deployment", pid, "dpl_1")
    repo.write_nodes([prepare_node("Deployment", dep, pid, {"service_id": ids["svc"], "environment_id": ids["env"], "provider_id": "dpl_1", "status": "ready", "event_time": T0, "exposure_status": "full", "revision_id": ids["rev"]})])
    mk = lambda ev: prepare_rel("DEPLOYS_REVISION", "Deployment", dep, "Revision", ids["rev"], pid, I.edge_key("DEPLOYS_REVISION", dep, ids["rev"], *ev), {"mapping_method": "log_sha", "evidence_ids": list(ev)})  # noqa: E731
    with pytest.raises(GraphWriteError):
        repo.write_relationships([mk(["evd_does_not_exist"])])
    real = _evidence(repo, ids, pid, "sha", T0, T0 + timedelta(seconds=1))
    assert repo.write_relationships([mk([real])]) == 1


def test_manifest_only_selects_published_builds(repo, pid):
    ids = _build(repo, pid)
    entry = {"repository_id": ids["repo"], "revision_id": ids["rev"], "build_id": ids["build"]}
    with pytest.raises(GraphWriteError):
        # _build wrote status "published" directly; flip it to staging to prove staging data is refused
        with repo._driver.session(database=repo._db) as s:
            s.run("MATCH (b:GraphBuild {id: $id}) SET b.status = 'staging'", id=ids["build"]).consume()
        repo.create_manifest(pid, [entry])
    repo.set_build_status(pid, ids["build"], "published")
    m1 = repo.create_manifest(pid, [entry])
    assert m1 == repo.create_manifest(pid, [entry])  # same selection, same manifest
    assert repo.manifest_build_ids(pid, m1) == [ids["build"]]
    with pytest.raises(GraphWriteError):
        repo.set_build_status(pid, ids["build"], "published")  # published -> published is not a transition


def test_citations_outside_the_retrieval_manifest_are_rejected(repo, pid):
    import json

    ids = _build(repo, pid)
    ev = _evidence(repo, ids, pid, "cite", T0, T0 + timedelta(seconds=1))
    rm = I.entity_id("RetrievalManifest", pid, "run_1", "exe_1")
    repo.write_nodes([prepare_node("RetrievalManifest", rm, pid, {"run_id": "run_1", "execution_id": "exe_1", "item_ids_json": json.dumps([ev, ids["chunk"]]), "hash": I.content_hash(ev), "cutoff": T0})])
    assert repo.check_citations(pid, rm, [ev, ids["chunk"]]) == []
    assert repo.check_citations(pid, rm, [ev, "evd_invented"]) == ["evd_invented"]
    assert repo.check_citations(pid + "-other", rm, [ev]) == [ev]  # another project cannot use this manifest


def test_dependencies_valid_at_excludes_unknown_validity(repo, driver, pid):
    ids = _build(repo, pid)
    db = _creds()[3]
    redis = I.resource_id(pid, "redis", "cache", "redis-sessions")
    repo.write_nodes([prepare_node("Resource", redis, pid, {"provider": "redis", "resource_type": "cache", "provider_resource_id": "redis-sessions"})])
    common = {"origin": "code", "derivation": "declared", "environment_id": ids["env"]}
    repo.write_relationships([
        prepare_rel("DEPENDS_ON", "Service", ids["svc"], "Resource", redis, pid, I.edge_key("DEPENDS_ON", ids["svc"], redis, "known"), {**common, "valid_from": T0 - timedelta(days=1), "verification": "unverified"}),
    ])
    args = dict(project_id=pid, service_id=ids["svc"], environment_id=ids["env"], limit=10)
    assert [r["id"] for r in run_template(driver, db, "dependencies_valid_at", at=T0.isoformat(), **args)] == [redis]
    assert run_template(driver, db, "dependencies_valid_at", at=(T0 - timedelta(days=2)).isoformat(), **args) == []  # before valid_from
