"""Evidence -> graph against a real Neo4j (T-009, T-006). Opt-in: TRACE_TEST_NEO4J=1 (credentials from .env or TEST_NEO4J_*).

Every test creates a throwaway Postgres project, whose UUID is also the graph project id, and deletes everything it wrote from Neo4j in the
fixture teardown, so it is safe against a shared instance. Provider payloads are FIXTURES (see tests/ingest_helpers.py); the graph writes,
the Cypher and the claim/acknowledge protocol are the real ones.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

import app.jobs.handlers  # noqa: F401  (registers the appliers)
from app.domain.ingest import graph_apply as ga
from app.graph import identity as I
from app.graph import outbox as ob
from app.graph.queries import run_template
from app.graph.repository import GraphRepository, GraphWriteError, prepare_node, prepare_rel
from app.jobs import worker
from app.jobs.reconcile import reconcile_graph_work
from tests.conftest import _creds, token_for
from tests.ingest_helpers import API, T0, add_deployment, ev, make_source, push, rows
from tests.test_graph_neo4j import _build
from tests.test_logs import RuntimeLogs, drain_rec, secret_for, setup
from tests.test_logs import sig as vsig

pytestmark = [pytest.mark.db, pytest.mark.skipif(os.environ.get("TRACE_TEST_NEO4J") != "1", reason="set TRACE_TEST_NEO4J=1 to run against Neo4j")]
APPLY = ["graph.apply_evidence"]


@pytest.fixture
def neo(container, driver):
    """The real driver in the container; every project id a test used is wiped from the graph afterwards."""
    db = _creds()[3]
    container.extras["neo4j"] = driver
    container.settings.neo4j_database = db
    created: list[str] = []
    yield created
    repo = GraphRepository(driver, db)
    for p in created:
        repo.delete_project(p)
        assert repo.counts(p) == {}          # nothing of this test is left on the shared instance
    container.extras.pop("neo4j", None)


def q(driver, cypher: str, **params) -> list[dict]:
    with driver.session(database=_creds()[3]) as s:
        return [r.data() for r in s.run(cypher, **params)]


def node(driver, pid: str, key: str, cid: str):
    got = q(driver, "MATCH (n:Entity:LogEvent {id: $id, project_id: $p}) RETURN properties(n) AS props, labels(n) AS labels, [(n)-[r]->(m) | [type(r), m.id, properties(r)]] AS edges",
            id=I.evidence_id(pid, cid, key), p=pid)
    return got[0] if got else None


async def apply(container, org, pid, owner="t"):
    return await ga.apply_pending(container, org["id"], UUID(pid), owner)


def edge_targets(n, typ):
    return [e[1] for e in n["edges"] if e[0] == typ]


async def test_app_push_reaches_the_graph_with_unknowns_kept_and_replays_cleanly(client, container, org, driver, neo):
    pid, cid, secret, h = await make_source(client, org, name="NeoA")
    neo.append(pid)
    await add_deployment(container, org["id"], pid, cid, dep_id="dpl_A", name="shopflow-api", sha="deadbeef0001")      # a commit TRACE has not indexed
    await push(client, cid, secret, [
        ev(1, deployment_id="dpl_A", level="ERROR", error_type="TimeoutError"), ev(2, deployment_id="dpl_A"), ev(3, deployment_id="dpl_not_stored"),
        ev(4, environment="nowhere"), ev(5, at=None),
    ])
    out = await apply(container, org, pid)
    assert out["applied"] == 5 and out["failed"] == 0 and out["remaining"] == 0 and out["report"]["events"] == 5

    env, svc, dep = I.environment_id(pid, "production"), I.service_id(pid, "shopflow-api"), I.entity_id("Deployment", pid, "dpl_A")
    e1 = node(driver, pid, "evt-1", cid)
    assert set(e1["labels"]) >= {"Entity", "EvidenceRecord", "LogEvent"} and e1["props"]["project_id"] == pid
    p = e1["props"]
    assert p["severity"] == "error" and p["origin"] == "telemetry" and p["source_connection_id"] == cid and p["request_id"] == "req-1"
    assert p["event_time"].to_native() == T0 + timedelta(seconds=1)
    assert p["ingested_at"].to_native() > p["event_time"].to_native() and p["created_at"].to_native() >= p["ingested_at"].to_native()      # when it happened / when TRACE got it / when it was written
    assert p["environment_id"] == env and p["service_id"] == svc and "TimeoutError" in p["message"]
    assert edge_targets(e1, "IN_ENVIRONMENT") == [env] and edge_targets(e1, "OBSERVED_ON") == [svc] and edge_targets(e1, "ASSOCIATED_WITH") == [dep]

    # unknown stays unknown: a deployment TRACE has no row for, an environment it could not map, a missing time
    e3, e4, e5 = (node(driver, pid, f"evt-{i}", cid) for i in (3, 4, 5))
    assert edge_targets(e3, "ASSOCIATED_WITH") == [] and edge_targets(e3, "OBSERVED_ON") == [] and "dpl_not_stored" in e3["props"]["origin_detail"]
    assert edge_targets(e4, "IN_ENVIRONMENT") == [] and "environment_id" not in e4["props"]
    assert "event_time" not in e5["props"] and e5["props"]["ingested_at"] and edge_targets(e5, "IN_ENVIRONMENT") == [env]
    d = q(driver, "MATCH (d:Entity:Deployment {id: $d, project_id: $p}) RETURN properties(d) AS props, [(d)-[r:DEPLOYS_REVISION]->() | r] AS revs", d=dep, p=pid)[0]
    assert d["revs"] == [] and "revision_id" not in d["props"]                       # commit is not an indexed revision: mapping stays unknown, never guessed from the sha text

    # what the investigator's own templates see
    got = run_template(driver, _creds()[3], "log_events_in_window", project_id=pid, service_id=svc, environment_id=env, window_start=T0.isoformat(),
                       window_end=(T0 + timedelta(minutes=5)).isoformat(), cutoff=datetime.now(UTC).isoformat(), include_info=True, limit=50)
    assert sorted(r["id"] for r in got) == sorted(I.evidence_id(pid, cid, k) for k in ("evt-1", "evt-2"))
    assert {r["deployment_id"] for r in got} == {dep}

    # Postgres agrees: acknowledged, and the cursor reached the end
    async with container.pool.connection() as conn:
        s1 = await ga.graph_state(conn, UUID(pid))
    assert s1["applied"] == 5 and s1["unacknowledged"] == 0 and s1["acknowledged_through"] == s1["received_through"]
    cov = (await client.get(f"{API}/projects/{pid}/logs", headers=h)).json()["meta"]["coverage"]
    assert cov["not_yet_in_graph"] == 0 and cov["graph"]["configured"] is True

    # crash after the graph commit: the rows are claimed with a dead lease and the graph already has them
    before = GraphRepository(driver, _creds()[3]).counts(pid)
    async with container.pool.connection() as conn:
        await conn.execute("UPDATE evidence_inbox SET graph_status='claimed', graph_claim_owner='dead', graph_lease_expires_at=now() - interval '1 second', graph_applied_at=NULL WHERE project_id=%s", (UUID(pid),))
        await conn.commit()
    again = await apply(container, org, pid)
    assert again["applied"] == 5 and GraphRepository(driver, _creds()[3]).counts(pid) == before        # replay changed nothing and raised no immutability conflict
    assert {r["graph_attempts"] for r in await rows(container, "SELECT graph_attempts FROM evidence_inbox WHERE project_id=%s", UUID(pid))} == {2}


async def test_indexed_revision_links_the_deployment_and_an_unambiguous_operation_locates_code(client, container, org, driver, neo):
    pid, cid, secret, h = await make_source(client, org, name="NeoB")
    neo.append(pid)
    repo = GraphRepository(driver, _creds()[3])
    ids = _build(repo, pid)                                       # a published build of revision 'a3f9c1e' with symbols pkg.a, pkg.b, pkg.c
    repo.write_relationships([prepare_rel("DEFINES", "SourceFile", ids["file"], "CodeSymbol", ids[f"sym_{x}"], pid, I.edge_key("DEFINES", ids["file"], ids[f"sym_{x}"]),
                                          {"build_id": ids["build"], "source_locator": f"src/payment/client.ts:{n}"}) for x, n in (("a", 1), ("b", 10), ("c", 20))])
    man = repo.create_manifest(pid, [{"repository_id": ids["repo"], "revision_id": ids["rev"], "build_id": ids["build"]}])
    await add_deployment(container, org["id"], pid, cid, dep_id="dpl_idx", name="payments", sha="A3F9C1E")      # sha case differs: matched case-insensitively
    await push(client, cid, secret, [ev(1, deployment_id="dpl_idx", op="a"), ev(2, deployment_id="dpl_idx", op="no_such_symbol"), ev(3, deployment_id="dpl_idx", op="a", event_name="operation.finished")])
    out = await apply(container, org, pid)
    assert out["applied"] == 3 and out["report"]["deployments_with_revision"] == 1 and out["report"]["located"] == 1
    dep = I.entity_id("Deployment", pid, "dpl_idx")
    d = q(driver, "MATCH (d:Entity:Deployment {id: $d, project_id: $p})-[r:DEPLOYS_REVISION]->(v:Revision) RETURN v.id AS rev, r.mapping_method AS how", d=dep, p=pid)
    assert d == [{"rev": ids["rev"], "how": "provider_commit_sha"}]
    e1, e2, e3 = (node(driver, pid, f"evt-{i}", cid) for i in (1, 2, 3))
    loc = [e for e in e1["edges"] if e[0] == "LOCATES"]
    assert len(loc) == 1 and loc[0][1] == ids["sym_a"] and loc[0][2]["certainty"] == "name_match" and loc[0][2]["mapping_method"] == "operation_name" and loc[0][2]["manifest_id"] == man
    assert [e for e in e2["edges"] if e[0] == "LOCATES"] == [] and [e for e in e3["edges"] if e[0] == "LOCATES"] == []      # unmatched or non-start events do not locate code


async def test_late_event_is_added_without_rewriting_what_an_earlier_run_saw(client, container, org, driver, neo):
    pid, cid, secret, h = await make_source(client, org, name="NeoC")
    neo.append(pid)
    db = _creds()[3]
    repo = GraphRepository(driver, db)
    await add_deployment(container, org["id"], pid, cid, dep_id="dpl_A", name="shopflow-api")
    await push(client, cid, secret, [ev(10, deployment_id="dpl_A"), ev(20, deployment_id="dpl_A")])
    await apply(container, org, pid)
    env, svc = I.environment_id(pid, "production"), I.service_id(pid, "shopflow-api")
    cutoff = datetime.now(UTC)
    args = dict(project_id=pid, service_id=svc, environment_id=env, window_start=T0.isoformat(), window_end=(T0 + timedelta(hours=1)).isoformat(), include_info=True, limit=50)
    seen = [r["id"] for r in run_template(driver, db, "log_events_in_window", cutoff=cutoff.isoformat(), **args)]
    assert len(seen) == 2
    mid = repo.record_retrieval(pid, "run_1", "exe_1", seen, cutoff=cutoff, query_template="log_events_in_window@1", truncated=False)
    snapshot = q(driver, "MATCH (m:RetrievalManifest {id: $m, project_id: $p}) RETURN properties(m) AS props", m=mid, p=pid)[0]["props"]

    # the event happened BEFORE both earlier events but reaches TRACE only now
    await push(client, cid, secret, [ev(1, deployment_id="dpl_A", message="late arrival")])
    await apply(container, org, pid)
    late = I.evidence_id(pid, cid, "evt-1")
    n = node(driver, pid, "evt-1", cid)["props"]
    assert n["event_time"].to_native() == T0 + timedelta(seconds=1) and n["ingested_at"].to_native() > cutoff and n["event_time"].to_native() < q(driver, "MATCH (n:LogEvent {id: $i}) RETURN n.event_time AS t", i=I.evidence_id(pid, cid, "evt-10"))[0]["t"].to_native()

    as_known_then = [r["id"] for r in run_template(driver, db, "log_events_in_window", cutoff=cutoff.isoformat(), **args)]
    as_known_now = [r["id"] for r in run_template(driver, db, "log_events_in_window", cutoff=(datetime.now(UTC) + timedelta(minutes=1)).isoformat(), **args)]
    assert set(as_known_then) == set(seen) and late not in as_known_then                                                                # the old cutoff does not see it
    assert late in as_known_now and len(as_known_now) == 3                                                                               # a continuation with a later cutoff does
    after = q(driver, "MATCH (m:RetrievalManifest {id: $m, project_id: $p}) RETURN properties(m) AS props", m=mid, p=pid)[0]["props"]
    assert after == snapshot and repo.citable_ids(pid, mid) == set(seen)
    assert repo.check_citations(pid, mid, [late]) == [late]                                  # the earlier run cannot cite evidence it never retrieved


async def test_every_kind_of_lost_input_becomes_an_evidence_gap_once(client, container, org, driver, neo):
    pid, cid, secret, h = await make_source(client, org, name="NeoD")
    neo.append(pid)
    container.settings.ingest_max_body_bytes = 2000
    await push(client, cid, secret, b'{"event_id":"x"}\nnot json')                                              # malformed
    await push(client, cid, secret, [ev(i, message="z" * 200) for i in range(30)])                              # oversize
    container.settings.ingest_max_body_bytes = 1024 * 1024
    await push(client, cid, secret, [ev(1), {"event_id": "no message"}, ev(2, message="y" * 9000), ev(3, environment="nowhere")])   # rejected + truncated + unmapped
    out = await apply(container, org, pid)
    assert out["gaps"] == 5 and out["applied"] == 3
    kinds = {r["kind"]: r["n"] for r in q(driver, "MATCH (g:Entity:EvidenceGap {project_id: $p}) RETURN g.kind AS kind, count(*) AS n", p=pid)}
    assert kinds == {"rejected_input": 2, "oversize_input": 1, "truncated_input": 1, "unmapped_scope": 1}
    g = q(driver, "MATCH (g:Entity:EvidenceGap {project_id: $p, kind: 'oversize_input'}) RETURN properties(g) AS props", p=pid)[0]["props"]
    assert g["status"] == "open" and g["detected_at"] and json.loads(g["scope_json"])["via"] == "app_push" and "zzzz" not in json.dumps(g, default=str)   # counts and ids, never content
    assert not await rows(container, "SELECT 1 FROM ingestion_batches WHERE project_id=%s AND gaps_applied_at IS NULL AND (reason IS NOT NULL OR rejected>0 OR truncated>0 OR unmapped>0)", UUID(pid))
    again = await apply(container, org, pid)
    assert again["gaps"] == 0 and sum(r["n"] for r in q(driver, "MATCH (g:Entity:EvidenceGap {project_id: $p}) RETURN count(*) AS n", p=pid)) == 5
    st = (await client.get(f"{API}/projects/{pid}/logs/status", headers=h)).json()["data"]
    assert {g["in_graph"] for g in st["recent_gaps"]} == {True}


async def test_projects_never_see_each_others_evidence_in_the_graph(client, container, org, driver, neo):
    a_pid, a_cid, a_secret, ha = await make_source(client, org, name="NeoIsoA")
    b_pid, b_cid, b_secret, hb = await make_source(client, org, name="NeoIsoB")
    neo.extend([a_pid, b_pid])
    db = _creds()[3]
    for pid, cid in ((a_pid, a_cid), (b_pid, b_cid)):
        await add_deployment(container, org["id"], pid, cid, dep_id="dpl_A", name="shopflow-api")
    await push(client, a_cid, a_secret, [ev("same", deployment_id="dpl_A", message="only in A")])
    await push(client, b_cid, b_secret, [ev("same", deployment_id="dpl_A", message="only in B")])
    assert (await push(client, b_cid, a_secret, [ev("stolen")])).status_code == 401                              # A's secret into B's endpoint
    await apply(container, org, a_pid)
    await apply(container, org, b_pid)
    ea, eb = I.evidence_id(a_pid, a_cid, "evt-same"), I.evidence_id(b_pid, b_cid, "evt-same")
    assert ea != eb
    assert q(driver, "MATCH (n:Entity {project_id: $p}) WHERE n.message = 'only in A' RETURN count(*) AS n", p=b_pid)[0]["n"] == 0
    assert q(driver, "MATCH (n:Entity {project_id: $p}) WHERE n.message = 'only in B' RETURN count(*) AS n", p=a_pid)[0]["n"] == 0
    assert q(driver, "MATCH (a:Entity {project_id: $a})-[r]-(b:Entity {project_id: $b}) RETURN count(r) AS n", a=a_pid, b=b_pid)[0]["n"] == 0
    args = dict(window_start=T0.isoformat(), window_end=(T0 + timedelta(hours=1)).isoformat(), cutoff=datetime.now(UTC).isoformat(), include_info=True, limit=50)
    got_b = run_template(driver, db, "log_events_in_window", project_id=b_pid, service_id=I.service_id(b_pid, "shopflow-api"), environment_id=I.environment_id(b_pid, "production"), **args)
    assert [r["id"] for r in got_b] == [eb]
    # asking B's template for A's service finds nothing, and a forged write of A's evidence id under B is refused by the writer
    assert run_template(driver, db, "log_events_in_window", project_id=b_pid, service_id=I.service_id(a_pid, "shopflow-api"), environment_id=I.environment_id(a_pid, "production"), **args) == []
    forged = prepare_node("LogEvent", ea, b_pid, {"source_connection_id": a_cid, "source_record_key": "evt-same", "ingested_at": datetime.now(UTC), "origin": "telemetry", "content_hash": "0" * 64,
                                                  "redacted_content": "x", "severity": "info", "message": "x", "event_time": None})
    with pytest.raises(GraphWriteError):
        GraphRepository(driver, db).write_nodes([forged])


async def test_drain_and_capture_evidence_flows_through_the_queue_and_the_worker(client, container, org, driver, neo):
    pid, cid, h, vproj = await setup(client, container, org, RuntimeLogs())
    neo.append(pid)
    secret = await secret_for(client, cid, h)
    body = json.dumps([drain_rec(1), drain_rec(2, level="error", message="boom")]).encode()
    assert (await client.post(f"{API}/ingest/vercel/{cid}", content=body, headers={"x-vercel-signature": vsig(secret, body)})).json()["accepted"] == 2
    assert (await client.post(f"{API}/projects/{pid}/logs/capture", headers=h, json={"seconds": 5})).status_code == 202
    while await worker.run_one(container, "t", ["connection.verify", "logs.capture"]):
        pass
    async with container.pool.connection() as conn:                                      # only this project's applier job may run against the shared graph
        await conn.execute("UPDATE jobs SET status='cancelled' WHERE kind IN ('graph.apply_evidence','graph.apply_outbox') AND status='queued' AND project_id IS DISTINCT FROM %s", (UUID(pid),))
        await conn.commit()
    queued = await rows(container, "SELECT id FROM jobs WHERE project_id=%s AND kind='graph.apply_evidence' AND status='queued'", UUID(pid))
    assert len(queued) == 1                                                             # one job for the drain and both capture slices together
    async with container.pool.connection() as conn:
        assert (await ga.graph_state(conn, UUID(pid)))["pending"] == 4
    # the first job is lost (as if the queue row never arrived); the reconciler finds the pending rows
    async with container.pool.connection() as conn:
        await conn.execute("UPDATE jobs SET status='cancelled' WHERE project_id=%s AND kind='graph.apply_evidence'", (UUID(pid),))
        await conn.commit()
    await reconcile_graph_work(container)
    async with container.pool.connection() as conn:
        await conn.execute("UPDATE jobs SET status='cancelled' WHERE kind IN ('graph.apply_evidence','graph.apply_outbox') AND status='queued' AND project_id IS DISTINCT FROM %s", (UUID(pid),))
        await conn.execute("UPDATE jobs SET status='cancelled' WHERE kind='graph.apply_outbox' AND status='queued'")
        await conn.commit()
    while await worker.run_one(container, "t", APPLY):
        pass
    job = (await rows(container, "SELECT status, result_ref FROM jobs WHERE project_id=%s AND kind='graph.apply_evidence' AND status='succeeded'", UUID(pid)))[0]
    assert job["result_ref"]["applied"] == 4 and job["result_ref"]["remaining"] == 0
    async with container.pool.connection() as conn:
        s = await ga.graph_state(conn, UUID(pid))
    assert s["applied"] == 4 and s["acknowledged_through"] == s["received_through"]
    n = q(driver, "MATCH (e:Entity:LogEvent {project_id: $p}) RETURN e.severity AS sev, e.environment_id AS env, e.service_id AS svc, e.origin_detail AS od", p=pid)
    assert len(n) == 4 and {r["env"] for r in n} == {I.environment_id(pid, "production")} and len({r["svc"] for r in n}) == 1 and any(r["sev"] == "error" for r in n)
    assert any(r["od"].startswith("vercel") for r in n)


async def test_outbox_scope_reaches_the_graph_and_a_replay_changes_nothing(client, container, org, driver, neo):
    h = token_for(org["owner_id"], org["owner_email"])
    p = (await client.post(f"{API}/projects", headers=h, json={"name": f"NeoOb{org['id'].hex[:6]}", "environments": [{"name": "production"}, {"name": "staging"}], "services": [{"name": "api"}, {"name": "worker"}]})).json()["data"]
    pid = p["id"]
    neo.append(pid)
    repo = GraphRepository(driver, _creds()[3])
    assert repo.counts(pid) == {}
    out = await ob.apply_outbox(container, org["id"], "t")
    assert out["applied"] == 1 and out["projects"] == 1
    c = repo.counts(pid)
    assert c == {"Environment": 2, "Project": 1, "Service": 2}
    assert q(driver, "MATCH (:Entity:Project {id: $j, project_id: $p})-[r:HAS_ENVIRONMENT|HAS_SERVICE]->() RETURN count(r) AS n", j=I.project_node_id(pid), p=pid)[0]["n"] == 4
    assert (await rows(container, "SELECT status FROM outbox WHERE project_id=%s", UUID(pid)))[0]["status"] == "applied"
    # replay: the row goes back to pending (as after a crash before the acknowledgement); the graph is unchanged
    async with container.pool.connection() as conn:
        await conn.execute("UPDATE outbox SET status='pending', applied_at=NULL WHERE project_id=%s", (UUID(pid),))
        await conn.commit()
    assert (await ob.apply_outbox(container, org["id"], "t2"))["applied"] == 1 and repo.counts(pid) == c


async def test_sync_endpoint_applies_due_evidence_and_reports_the_acknowledged_state(client, container, org, driver, neo):
    pid, cid, secret, h = await make_source(client, org, name="NeoSync")
    neo.append(pid)
    await add_deployment(container, org["id"], pid, cid, dep_id="dpl_A", name="shopflow-api")
    await push(client, cid, secret, [ev(1, deployment_id="dpl_A"), ev(2, deployment_id="dpl_A")])
    async with container.pool.connection() as conn:
        await conn.execute("UPDATE jobs SET status='cancelled' WHERE kind='graph.apply_evidence' AND status='queued'")
        await conn.execute("UPDATE evidence_inbox SET graph_status='failed', graph_error='earlier refusal' WHERE project_id=%s AND source_record_key='evt-2'", (UUID(pid),))
        await conn.commit()
    r = await client.post(f"{API}/projects/{pid}/graph/sync", headers=h)
    assert r.status_code == 200, r.text
    ev_out = r.json()["data"]["evidence"]
    assert ev_out["applied"] == 1 and ev_out["state"]["failed"] == 1 and ev_out["state"]["applied"] == 1                 # the failed row is not retried unless asked
    r2 = await client.post(f"{API}/projects/{pid}/graph/sync?retry_failed=true", headers=h)
    assert r2.json()["data"]["evidence"]["state"]["unacknowledged"] == 0 and r2.json()["data"]["evidence"]["applied"] == 1
    assert node(driver, pid, "evt-2", cid) is not None
