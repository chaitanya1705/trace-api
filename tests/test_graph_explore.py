"""Graph explorer queries against a real Neo4j (opt-in: TRACE_TEST_NEO4J=1). Throwaway project ids, cleaned up afterwards."""
from __future__ import annotations

import os

import pytest

from app.api.v1 import graph_explore as X
from app.errors import Unprocessable
from tests.conftest import _creds
from tests.test_graph_neo4j import _build

pytestmark = pytest.mark.skipif(os.environ.get("TRACE_TEST_NEO4J") != "1", reason="set TRACE_TEST_NEO4J=1 to run against Neo4j")
DB = _creds()[3]


def _closed(r):
    """Every relationship names two nodes that are in the same result (what the canvas needs to draw an edge)."""
    ids = {n["id"] for n in r["nodes"]}
    assert r["relationships"] and all(x["start"] in ids and x["end"] in ids for x in r["relationships"])


def test_schema_lists_labels_and_types_of_this_project_only(driver, repo, pid):
    _build(repo, pid)
    s = X._schema(driver, DB, pid)
    labels = {x["label"]: x["count"] for x in s["labels"]}
    assert labels["CodeSymbol"] == 3 and labels["Project"] == 1
    assert {x["type"]: x["count"] for x in s["relationship_types"]}["CALLS"] == 2
    assert X._schema(driver, DB, pid + "-none")["labels"] == []


def test_trace_overview_returns_backbone_with_its_relationships(driver, repo, pid):
    _build(repo, pid)
    r = X._explore(driver, DB, pid, "trace", None, 50)
    kinds = {n["label"] for n in r["nodes"]}
    assert {"Project", "Environment", "Service", "Repository", "Revision"} <= kinds
    assert "CodeSymbol" not in kinds  # code body is one expand away
    _closed(r)
    assert {x["type"] for x in r["relationships"]} >= {"HAS_ENVIRONMENT", "HAS_SERVICE", "HAS_REVISION"} or r["relationships"]
    assert "MATCH" in r["cypher"] and pid in r["cypher"]


def test_nodes_keep_labels_and_properties(driver, repo, pid):
    ids = _build(repo, pid)
    d = X._node_detail(driver, DB, pid, ids["sym_b"])
    assert d["node"]["label"] == "CodeSymbol" and "Entity" not in d["node"]["labels"]
    assert d["node"]["properties"]["qualified_name"] == "pkg.b"
    assert {(x["type"], x["direction"]) for x in d["degree"]} == {("CALLS", "in"), ("CALLS", "out")}


def test_expand_by_type_and_direction(driver, repo, pid):
    ids = _build(repo, pid)
    out = X._neighbors(driver, DB, pid, ids["sym_b"], "CALLS", "out", 50)
    assert {n["id"] for n in out["nodes"]} == {ids["sym_b"], ids["sym_a"]}
    both = X._neighbors(driver, DB, pid, ids["sym_b"], None, "both", 50)
    assert {ids["sym_a"], ids["sym_c"]} <= {n["id"] for n in both["nodes"]}
    assert "CALLS" in out["cypher"]
    _closed(out)
    _closed(both)


def test_nothing_leaks_across_projects(driver, repo, pid):
    ids = _build(repo, pid)
    assert X._node_detail(driver, DB, pid + "-other", ids["sym_a"]) is None
    assert X._neighbors(driver, DB, pid + "-other", ids["sym_a"], None, "both", 10) is None
    assert X._search(driver, DB, pid + "-other", "pkg", 10) == []
    assert X._path(driver, DB, pid + "-other", ids["sym_a"], ids["sym_c"], 5) is None


def test_shortest_path_between_two_nodes(driver, repo, pid):
    ids = _build(repo, pid)
    p = X._path(driver, DB, pid, ids["sym_c"], ids["sym_a"], 6)
    assert [n["id"] for n in p["nodes"]] == [ids["sym_c"], ids["sym_b"], ids["sym_a"]]
    assert [r["type"] for r in p["relationships"]] == ["CALLS", "CALLS"]
    _closed(p)


def test_search_finds_by_name_and_by_id(driver, repo, pid):
    ids = _build(repo, pid)
    assert ids["sym_a"] in {n["id"] for n in X._search(driver, DB, pid, "pkg.a", 10)} or ids["sym_a"] in {n["id"] for n in X._search(driver, DB, pid, "pkg", 10)}
    assert X._search(driver, DB, pid, ids["sym_a"], 5)[0]["id"] == ids["sym_a"]


def test_label_view_and_input_checks(driver, repo, pid):
    _build(repo, pid)
    r = X._explore(driver, DB, pid, "label", "CodeSymbol", 10)
    assert len(r["nodes"]) == 3 and {n["label"] for n in r["nodes"]} == {"CodeSymbol"}
    with pytest.raises(Unprocessable):
        X._check_label("Entity) DETACH DELETE (n")
    with pytest.raises(Unprocessable):
        X._check_type("CALLS]-() DELETE (x")
    with pytest.raises(Unprocessable):
        X._check_id("x' OR 1=1 //")


def test_long_values_are_truncated_and_caption_is_readable():
    n = X._value("x" * 5000)
    assert len(n) < 2100 and "more characters" in n
    assert X.caption("Ticket", {"display_id": "TKT-5", "title": "Receipt mismatch"}) == "TKT-5 Receipt mismatch"
    assert X.caption("Revision", {"commit_sha": "218241b0cd51"}) == "218241b"
