"""O12 semantic acceptance fixtures. Each asserts the correct output AND the misleading inference that must be excluded.
Opt-in like test_graph_neo4j (TRACE_TEST_NEO4J=1). Source trees are tiny labelled fixtures written to tmp_path."""
from __future__ import annotations

import os
import textwrap
from datetime import UTC, datetime, timedelta

import pytest

from app.graph import identity as I
from app.graph.indexer import index_repository
from app.graph.queries import Bounded, TemplateError, escape_lucene, run_bounded, run_template
from app.graph.repository import prepare_node, prepare_rel
from tests.conftest import _creds

pytestmark = pytest.mark.skipif(os.environ.get("TRACE_TEST_NEO4J") != "1", reason="set TRACE_TEST_NEO4J=1 to run against Neo4j")
T0 = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def _write(tmp_path, files: dict[str, str]):
    for p, body in files.items():
        f = tmp_path / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(tmp_path)


def _symbol(driver, pid, name):
    with driver.session(database=_creds()[3]) as s:
        return [r["id"] for r in s.run("MATCH (n:Entity:CodeSymbol {project_id: $p}) WHERE n.qualified_name = $n RETURN n.id AS id", p=pid, n=name)]


def test_ambiguous_name_keeps_every_candidate_and_marks_it(repo, driver, pid, tmp_path):
    root = _write(tmp_path, {
        "a.py": "def helper():\n    return 1\n",
        "b.py": "def helper():\n    return 2\n",
        "main.py": "def run():\n    return helper()\n",  # which helper? the extractor cannot know
    })
    index_repository(repo, project_id=pid, root=root, repository_name="fx", commit_sha="c1")
    with driver.session(database=_creds()[3]) as s:
        rows = list(s.run("MATCH (a:CodeSymbol {project_id: $p, qualified_name: 'run'})-[r:CALLS]->(b:CodeSymbol) RETURN r.derivation AS d, b.qualified_name AS n", p=pid))
    assert len(rows) == 2 and {r["d"] for r in rows} == {"ambiguous"}  # both candidates kept, none silently chosen


def test_same_file_call_is_resolved_not_inferred(repo, driver, pid, tmp_path):
    root = _write(tmp_path, {"m.py": "def a():\n    return b()\ndef b():\n    return 1\n", "n.py": "def b():\n    return 2\n"})
    index_repository(repo, project_id=pid, root=root, repository_name="fx", commit_sha="c1")
    with driver.session(database=_creds()[3]) as s:
        rows = list(s.run("MATCH (a:CodeSymbol {project_id: $p, qualified_name: 'a'})-[r:CALLS]->(b:CodeSymbol) RETURN r.derivation AS d, b.file_id AS f", p=pid))
    assert [r["d"] for r in rows] == ["resolved"]  # the local b wins; n.py's b is not linked


def test_partial_parse_and_unsupported_language_are_reported_as_gaps(repo, driver, pid, tmp_path):
    root = _write(tmp_path, {"ok.py": "def ok():\n    return 1\n", "broken.py": "def broken(:\n", "svc.go": "package main\nfunc main() {}\n"})
    rep = index_repository(repo, project_id=pid, root=root, repository_name="fx", commit_sha="c1")
    assert rep.gaps == {"partial_parse": 1, "unsupported_language": 1}
    assert _symbol(driver, pid, "ok") and not _symbol(driver, pid, "broken")  # no invented symbols from a file that did not parse


def test_cycle_terminates_and_returns_bounded_paths(repo, driver, pid, tmp_path):
    root = _write(tmp_path, {"c.py": "def a():\n    return b()\ndef b():\n    return a()\n"})
    index_repository(repo, project_id=pid, root=root, repository_name="fx", commit_sha="c1")
    seed = _symbol(driver, pid, "a")[0]
    with driver.session(database=_creds()[3]) as s:
        builds = [r["id"] for r in s.run("MATCH (b:GraphBuild {project_id: $p, status: 'published'}) RETURN b.id AS id", p=pid)]
    got = run_bounded(driver, _creds()[3], "callers_4hop", project_id=pid, seed_id=seed, build_ids=builds, limit=50)
    assert isinstance(got, Bounded) and got.template == "callers_4hop@1" and 0 < len(got.rows) <= 50
    for p in got.rows:
        assert len(p["relationships"]) <= 4  # hop ceiling holds even in a cycle


def test_expansion_row_ceiling(driver, pid):
    with pytest.raises(TemplateError):
        run_bounded(driver, _creds()[3], "callers_2hop", project_id=pid, seed_id="x", build_ids=["b"], limit=101)


def test_reverse_direction_blast_follows_consumers_only(repo, driver, pid, tmp_path):
    root = _write(tmp_path, {"l.py": "def low():\n    return 1\ndef mid():\n    return low()\ndef top():\n    return mid()\ndef unrelated():\n    return 0\n"})
    index_repository(repo, project_id=pid, root=root, repository_name="fx", commit_sha="c1")
    with driver.session(database=_creds()[3]) as s:
        builds = [r["id"] for r in s.run("MATCH (b:GraphBuild {project_id: $p, status: 'published'}) RETURN b.id AS id", p=pid)]
    rows = run_template(driver, _creds()[3], "potential_exposure", project_id=pid, symbol_id=_symbol(driver, pid, "low")[0], build_ids=builds, limit=50)
    consumers = {r["consumer_id"] for r in rows}
    assert consumers == {_symbol(driver, pid, "mid")[0], _symbol(driver, pid, "top")[0]}  # upstream consumers
    assert _symbol(driver, pid, "unrelated")[0] not in consumers  # and nothing it does not reach
    rows_top = run_template(driver, _creds()[3], "potential_exposure", project_id=pid, symbol_id=_symbol(driver, pid, "top")[0], build_ids=builds, limit=50)
    assert rows_top == []  # changing `top` exposes no consumers: dependencies are not walked downward


def test_rebuild_with_a_failed_build_leaves_the_published_build_alone(repo, driver, pid, tmp_path):
    root = _write(tmp_path, {"a.py": "def a():\n    return 1\n"})
    first = index_repository(repo, project_id=pid, root=root, repository_name="fx", commit_sha="c1")
    assert repo.manifest_build_ids(pid, first.manifest_id) == [first.build_id]
    with pytest.raises(ValueError):
        index_repository(repo, project_id=pid, root=str(tmp_path / "missing"), repository_name="fx", commit_sha="c1")
    with driver.session(database=_creds()[3]) as s:
        st = s.run("MATCH (b:GraphBuild {id: $id}) RETURN b.status AS st", id=first.build_id).single()["st"]
    assert st == "published"


def test_same_path_in_two_projects_never_mixes(repo, driver, pid, tmp_path):
    root = _write(tmp_path, {"same.py": "def f():\n    return 1\n"})
    other = pid + "-b"
    try:
        a = index_repository(repo, project_id=pid, root=root, repository_name="fx", commit_sha="c1")
        b = index_repository(repo, project_id=other, root=root, repository_name="fx", commit_sha="c1")
        assert a.revision_id != b.revision_id and a.repository_id != b.repository_id
        assert repo.counts(pid)["SourceFile"] == 1 and repo.counts(other)["SourceFile"] == 1
    finally:
        repo.delete_project(other)


def test_fulltext_is_scope_filtered_and_reports_truncation(repo, driver, pid, tmp_path):
    root = _write(tmp_path, {"p.py": "def pay():\n    # timeout_ms lowered to 800\n    return 1\n"})
    mine = index_repository(repo, project_id=pid, root=root, repository_name="fx", commit_sha="c1")
    other = pid + "-b"
    try:
        index_repository(repo, project_id=other, root=root, repository_name="fx", commit_sha="c1")
        q = escape_lucene("timeout_ms")
        got = run_bounded(driver, _creds()[3], "source_text_search", project_id=pid, query=q, revision_ids=[mine.revision_id], candidates=10, limit=10)
        assert len(got.rows) == 1 and not got.truncated and got.rows[0]["path"] == "p.py"  # the other project's identical text is excluded
        tiny = run_bounded(driver, _creds()[3], "source_text_search", project_id=pid, query=q, revision_ids=[mine.revision_id], candidates=1, limit=10)
        assert tiny.truncated  # a candidate pool of 1 is exhausted, so the caller must record a truncated_query gap
        with pytest.raises(TemplateError):
            run_bounded(driver, _creds()[3], "source_text_search", project_id=pid, query=q, revision_ids=[], candidates=9999, limit=10)
    finally:
        repo.delete_project(other)


def test_overlapping_deployments_keep_every_candidate_revision(repo, driver, pid):
    svc, env = I.service_id(pid, "pay"), I.environment_id(pid, "production")
    repo.write_nodes([
        prepare_node("Service", svc, pid, {"name": "pay", "service_key": "pay"}),
        prepare_node("Environment", env, pid, {"name": "production", "environment_key": "production"}),
    ])
    deps = []
    for i, minutes in enumerate((5, 7)):  # gradual rollout: two live deployments in the same window, revision unknown for the second
        did = I.entity_id("Deployment", pid, f"dpl_{i}")
        deps.append(did)
        repo.write_nodes([prepare_node("Deployment", did, pid, {"service_id": svc, "environment_id": env, "provider_id": f"dpl_{i}", "status": "ready", "event_time": T0 + timedelta(minutes=minutes), "exposure_status": "partial", "revision_id": None})])
        repo.write_relationships([prepare_rel("DEPLOYS_SERVICE", "Deployment", did, "Service", svc, pid, I.edge_key("DEPLOYS_SERVICE", did, svc))])
    got = run_template(driver, _creds()[3], "deployments_in_window", project_id=pid, service_id=svc, environment_id=env, window_start=T0.isoformat(), window_end=(T0 + timedelta(hours=1)).isoformat(), limit=10)
    assert [r["id"] for r in got] == deps and all(r["revision_id"] is None for r in got)  # unknown mapping stays unknown, not guessed


def test_conflicting_dependency_sources_are_both_kept(repo, driver, pid):
    svc, env = I.service_id(pid, "pay"), I.environment_id(pid, "production")
    res = I.resource_id(pid, "redis", "cache", "sessions")
    repo.write_nodes([
        prepare_node("Service", svc, pid, {"name": "pay", "service_key": "pay"}),
        prepare_node("Environment", env, pid, {"name": "production", "environment_key": "production"}),
        prepare_node("Resource", res, pid, {"provider": "redis", "resource_type": "cache", "provider_resource_id": "sessions"}),
    ])
    base = {"environment_id": env, "valid_from": T0 - timedelta(days=1)}
    repo.write_relationships([
        prepare_rel("DEPENDS_ON", "Service", svc, "Resource", res, pid, I.edge_key("DEPENDS_ON", svc, res, "code"), {**base, "origin": "code", "derivation": "extracted"}),
        prepare_rel("DEPENDS_ON", "Service", svc, "Resource", res, pid, I.edge_key("DEPENDS_ON", svc, res, "human"), {**base, "origin": "human", "derivation": "declared", "verification": "disputed"}),
    ])
    got = run_template(driver, _creds()[3], "dependencies_valid_at", project_id=pid, service_id=svc, environment_id=env, at=T0.isoformat(), limit=10)
    assert sorted(r["origin"] for r in got) == ["code", "human"]  # opposite claims are separate edges, never merged


def test_retrieval_manifest_round_trip(repo, pid):
    mid = repo.record_retrieval(pid, "run_1", "exe_1", ["evd_b", "evd_a", "evd_a"], cutoff=T0, query_template="runtime_observations@2", truncated=False)
    assert mid == repo.record_retrieval(pid, "run_1", "exe_1", ["evd_a", "evd_b"], cutoff=T0, query_template="runtime_observations@2", truncated=False)
    assert repo.citable_ids(pid, mid) == {"evd_a", "evd_b"}
    assert repo.check_citations(pid, mid, ["evd_a", "evd_zzz"]) == ["evd_zzz"]


def test_community_depth_goes_one_level_deeper_when_one_folder_holds_the_code():
    from app.graph.indexer import _community_depth, _community_of

    assert _community_depth(["src/a/x.py", "src/b/y.py", "src/c/z.py", "src/d/w.py", "src/e/v.py", "docs/r.py"]) == 2
    assert _community_depth(["app/x.py", "lib/y.py", "web/z.py"]) == 1
    assert _community_of("src/features/pay/a.ts", 2) == "src/features" and _community_of("a.ts", 2) == "(root)"


def test_dirty_checkout_gets_its_own_revision_and_latest_build_per_repository_wins(repo, driver, pid, tmp_path):
    import subprocess

    root = _write(tmp_path, {"a.py": "def a():\n    return 1\n"})
    run = lambda *a: subprocess.run(["git", "-C", root, *a], check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    run("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "c1")
    clean = index_repository(repo, project_id=pid, root=root, repository_name="fx")
    (tmp_path / "a.py").write_text("def a():\n    return 2\n", encoding="utf-8")  # uncommitted edit
    dirty = index_repository(repo, project_id=pid, root=root, repository_name="fx")
    assert "+wip-" in dirty.commit_sha and dirty.revision_id != clean.revision_id  # the commit's own content is never overwritten
    with driver.session(database=_creds()[3]) as s:
        st = {r["id"]: r["st"] for r in s.run("MATCH (b:GraphBuild {project_id: $p}) RETURN b.id AS id, b.status AS st", p=pid)}
    assert st[dirty.build_id] == "published" and st[clean.build_id] == "superseded"  # one published build per repository
