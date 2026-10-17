"""Evidence sync and the GraphRAG investigation runner, against the real Neo4j (opt-in: TRACE_TEST_NEO4J=1).

Everything below is a labeled TEST FIXTURE: a tiny two-file repository, a deployment, and a few synthetic log events. It is
written through the same ontology-validated writers production uses; nothing here is importable from app/.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.graph import evidence as E
from app.graph import sync as S
from app.graph.indexer import index_repository
from app.graph.queries import run_bounded
from app.investigate import runner
from tests.conftest import _creds

pytestmark = pytest.mark.skipif(not os.environ.get("TRACE_TEST_NEO4J"), reason="needs a real Neo4j (set TRACE_TEST_NEO4J=1)")

SHA = "c" * 40
PRICING = '''def discount(total, pct):
    return total * pct / 100


def calculate_price(items, coupon):
    total = sum(i["price"] * i["qty"] for i in items)
    if coupon:
        total = total - discount(total, coupon["pct"])
    return total
'''
ORDERS = '''from pricing import calculate_price


def build_receipt(items, coupon):
    return {"total": calculate_price(items, coupon)}
'''


@pytest.fixture
def seeded(repo, pid, tmp_path):
    (tmp_path / "pricing.py").write_text(PRICING)
    (tmp_path / "orders.py").write_text(ORDERS)
    rep = index_repository(repo, project_id=pid, root=str(tmp_path), repository_name="fixture-shop", commit_sha=SHA)
    return rep


def _bundle(pid, rep):
    t0 = datetime.now(UTC) - timedelta(hours=1)
    ev = lambda n, op, name, dur=None: {"event": {  # noqa: E731
        "event_id": f"e{n}", "message": f"{op} {name}", "level": "INFO", "operation": op, "component_id": "pricing", "event_name": f"operation.{name}",
        "event_time": (t0 + timedelta(seconds=n)).isoformat(), "request_id": "r1", "duration_ms": dur, "deployment_id": "dpl_1", "source_revision": SHA}, "origin": "fixture_backend"}
    payload = {
        "target": {"deployment_id": "dpl_1", "environment": "production", "source_revision": SHA},
        "ticket": {"run_id": "run-1", "started_at": t0.isoformat(), "ended_at": (t0 + timedelta(seconds=30)).isoformat(), "observed_counts": {"requests_sent": 1, "receipt_mismatches": 1}},
        "requests": [{"observation_id": "o1", "backend_request_id": "r1", "method": "POST", "path": "/checkout", "http_status": 200, "outcome": "success", "duration_ms": 12,
                      "started_at": t0.isoformat(), "origin": "fixture_client", "request_body": {"coupon": "SAVE10", "items": [{"qty": 1}]}}],
        "backend_events": [ev(1, "calculate_price", "started"), ev(2, "build_receipt", "started")] ,
    }
    return S.Bundle(pid, "org-1", "Fixture Shop", [{"id": "e", "name": "production", "environment_key": "production"}], [
        {"provider_deployment_id": "dpl_1", "provider_project_id": "prj_1", "project_name": "fixture-backend", "target": "production", "state": "READY", "commit_sha": SHA,
         "revision_mapping": "provider_metadata", "created_at_provider": datetime.now(UTC) - timedelta(hours=2), "ready_at": None, "environment_key": "production"}],
        [{"id": uuid.uuid4(), "number": 1, "title": "Receipts differ from the calculated price", "payload": payload}])


def test_sync_writes_evidence_and_locates_code_by_unique_operation_name(repo, pid, seeded):
    report = S.project(repo, _bundle(pid, seeded))
    assert (report.environments, report.services, report.deployments, report.deployments_with_revision) == (1, 1, 1, 1)
    assert (report.log_events, report.requests, report.measurements, report.located) == (2, 1, 2, 2)
    counts = repo.counts(pid)
    assert counts["LogEvent"] == 2 and counts["RequestSpan"] == 1 and counts["Measurement"] == 2 and counts["Deployment"] == 1


def test_sync_is_idempotent(repo, pid, seeded):
    b = _bundle(pid, seeded)
    S.project(repo, b)
    first = repo.counts(pid)
    S.project(repo, b)
    assert repo.counts(pid) == first


def test_deployment_without_an_indexed_revision_keeps_the_mapping_unknown(repo, pid, seeded):
    b = _bundle(pid, seeded)
    b.deployments[0]["commit_sha"] = "d" * 40
    report = S.project(repo, b)
    assert report.deployments_with_revision == 0 and report.skipped.get("deployment_revision_not_indexed") == 1
    assert report.located == 0  # no revision, so no operation is located in code (never guessed)


def test_evidence_is_scoped_to_its_project(repo, pid, seeded, driver):
    S.project(repo, _bundle(pid, seeded))
    other = "itest-" + uuid.uuid4().hex[:10]
    try:
        svc = E.I.service_id(pid, "fixture-backend")
        env = E.I.environment_id(pid, "production")
        rows = run_bounded(driver, _creds()[3], "evidence_by_operation", project_id=other, service_id=svc, environment_id=env, window_start="2000-01-01T00:00:00Z",
                           window_end="2100-01-01T00:00:00Z", cutoff="2100-01-01T00:00:00Z", limit=10).rows
        assert rows == []
    finally:
        repo.delete_project(other)


def _ticket(pid, **kw):
    now = datetime.now(UTC)
    base = dict(id=str(uuid.uuid4()), title="Receipts differ from the calculated price", description="", severity=None, environment_key="production", service_hint=None,
                window_start=now - timedelta(hours=1, minutes=1), window_end=now, opened_at=now - timedelta(minutes=5), revision_number=1)
    return runner.TicketIn(**{**base, **kw})


def _detail(driver, pid, control_run_id):
    from app.investigate import read

    return read.run_detail(driver, _creds()[3], pid, control_run_id)


def test_investigation_without_evidence_finishes_as_insufficient_and_says_why(repo, pid, seeded, driver):
    # indexed code and a scope exist, but no runtime evidence was synced for the window
    S.project(repo, S.Bundle(pid, "org-1", "Fixture Shop", [{"id": "e", "name": "production", "environment_key": "production"}], [], []))
    run_id = str(uuid.uuid4())
    runner.investigate(driver, _creds()[3], project_id=pid, ticket=_ticket(pid), control_run_id=run_id, actor_id="t", api_key=None, model="none")
    d = _detail(driver, pid, run_id)
    assert d["run"]["status"] == "completed"
    assert d["finding"]["status"] == "insufficient_evidence"
    assert any(g["kind"] == "source_disconnected" for g in d["gaps"])


def test_investigation_runs_every_step_cites_only_retrieved_evidence_and_records_the_run(repo, pid, seeded, driver):
    S.project(repo, _bundle(pid, seeded))
    run_id = str(uuid.uuid4())
    runner.investigate(driver, _creds()[3], project_id=pid, ticket=_ticket(pid), control_run_id=run_id, actor_id="t", api_key=None, model="none")  # no key: graph-rule fallback
    d = _detail(driver, pid, run_id)
    assert d["run"]["status"] == "completed"
    assert [s["kind"] for s in d["steps"]] == ["scope", "deployments", "logs", "code", "changes", "blast_radius", "hypotheses", "impact", "report"]
    tools = {t["name"] for s in d["steps"] for t in s["tools"]}
    assert {"deployments_in_window", "evidence_by_operation", "request_observations", "code_located_in_window"} <= tools
    # the fallback model path is declared as a gap, never presented as the language model
    assert any("language model was unavailable" in g["description"] for g in d["gaps"])
    retrieved = {i for s in d["steps"] for t in s["tools"] for i in t["item_ids"]}
    cited = {c["id"] for cl in (d["finding"] or {}).get("claims", []) for c in cl["citations"]} | {i for h in d["hypotheses"] for i in (*h["supported_by"], *h["contradicted_by"])}
    assert cited <= retrieved | {e["id"] for e in d["evidence"]}


def test_a_second_run_is_a_new_immutable_run_not_an_overwrite(repo, pid, seeded, driver):
    S.project(repo, _bundle(pid, seeded))
    t = _ticket(pid)
    ids = []
    for _ in range(2):
        rid = str(uuid.uuid4())
        runner.investigate(driver, _creds()[3], project_id=pid, ticket=t, control_run_id=rid, actor_id="t", api_key=None, model="none")
        ids.append(rid)
    from app.investigate import read

    runs = read.list_runs(driver, _creds()[3], [pid], ticket_id=t.id)
    assert {r["control_run_id"] for r in runs} == set(ids)
