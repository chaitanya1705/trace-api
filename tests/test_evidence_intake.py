"""T-009 / T-006 negative paths and applier semantics that need Postgres only (the graph write itself is replaced by a recording double here;
the real Neo4j path is tests/test_evidence_graph_neo4j.py, opt-in). Event payloads are FIXTURES shaped like ShopFlow's own diagnostics.

Covers: malformed, oversize, too many events, duplicate, late / out-of-order, truncated and unmapped input; cross-project isolation; zero rows
versus no source; the drain, app push and live capture paths; and the claim -> graph -> acknowledge protocol (lease expiry, crash, retry, poison
rows, cursor)."""
from __future__ import annotations

import json
from datetime import timedelta
from uuid import UUID

import httpx
import pytest

import app.jobs.handlers  # noqa: F401  (registers the appliers)
import app.jobs.handlers.logs  # noqa: F401
from app.domain.ingest import graph_apply as ga
from app.domain.ingest.vercel_logs import MAX_EVENTS_PER_BATCH, MAX_MESSAGE
from app.graph import outbox as ob
from app.graph.apply_evidence import ApplyReport, gap_specs
from app.graph.validation import GraphValidationError
from app.jobs import worker
from app.jobs.reconcile import reconcile_graph_work
from tests.conftest import add_member, token_for
from tests.ingest_helpers import API, T0, add_deployment, ev, make_project, make_source, push, rows, sig
from tests.test_logs import KINDS, RuntimeLogs, drain_rec, secret_for, setup
from tests.test_logs import sig as vsig

pytestmark = pytest.mark.db
APPLY = ["graph.apply_evidence"]
OUTBOX = ["graph.apply_outbox"]


@pytest.fixture(autouse=True)
async def _no_leftover_applier_jobs(container):
    yield
    async with container.pool.connection() as conn:
        await conn.execute("DELETE FROM jobs WHERE kind IN ('graph.apply_evidence','graph.apply_outbox') AND status='queued'")
        await conn.commit()


class FakeGraph:
    """TEST DOUBLE for the Neo4j projection: records what it was asked to write, can be told to fail."""

    def __init__(self):
        self.bundles = []
        self.error: Exception | None = None
        self.poison: set[str] = set()

    def __call__(self, repo, bundle):
        self.bundles.append(bundle)
        if self.error:
            raise self.error
        if any(r["source_record_key"] in self.poison for r in bundle.rows):
            raise GraphValidationError(["LogEvent.severity: required"])
        return ApplyReport(events=len(bundle.rows), gaps=len(bundle.gaps))

    @property
    def keys(self):
        return [r["source_record_key"] for b in self.bundles for r in b.rows]


@pytest.fixture
def graph(container, monkeypatch):
    fake = FakeGraph()
    container.extras["neo4j"] = object()      # the driver is never touched: project_evidence is replaced
    monkeypatch.setattr(ga, "project_evidence", fake)
    return fake


async def state(container, pid):
    async with container.pool.connection() as conn:
        return await ga.graph_state(conn, UUID(str(pid)))


async def drain_jobs(container, kinds=APPLY):
    while await worker.run_one(container, "t", kinds):
        pass


# ---- the receipt: what intake stores and says about the graph --------------------------------------------------
async def test_without_neo4j_rows_stay_pending_and_coverage_says_so(client, container, org):
    pid, cid, secret, h = await make_source(client, org)
    assert (await push(client, cid, secret, [ev(1), ev(2)])).json()["accepted"] == 2
    assert await rows(container, "SELECT 1 FROM jobs WHERE project_id=%s AND kind='graph.apply_evidence'", UUID(pid)) == []   # nothing to apply with: nothing queued
    cov = (await client.get(f"{API}/projects/{pid}/logs", headers=h)).json()["meta"]["coverage"]
    assert cov["not_yet_in_graph"] == 2 and cov["graph"]["configured"] is False and cov["graph"]["pending"] == 2 and cov["graph"]["applied"] == 0
    assert cov["graph"]["acknowledged_through"] == cov["graph"]["oldest_unacknowledged_at"]
    src = (await client.get(f"{API}/projects/{pid}/log-source", headers=h)).json()["data"]
    assert src["graph"]["configured"] is False and src["graph"]["pending"] == 2 and src["last_batch"]["graph"] == {"pending": 2}


async def test_each_intake_queues_one_applier_job_in_the_same_transaction(client, container, org, graph):
    pid, cid, secret, h = await make_source(client, org)
    await push(client, cid, secret, [ev(1)])
    await push(client, cid, secret, [ev(2)])
    jobs = await rows(container, "SELECT status, payload FROM jobs WHERE project_id=%s AND kind='graph.apply_evidence'", UUID(pid))
    assert len(jobs) == 1 and jobs[0]["status"] == "queued" and jobs[0]["payload"] == {"project_id": pid}
    # a refused request also queues it: its gap has to reach the graph
    await push(client, cid, secret, b"{not json")
    assert len(await rows(container, "SELECT 1 FROM jobs WHERE project_id=%s AND kind='graph.apply_evidence'", UUID(pid))) == 1   # already queued, not duplicated


# ---- negative paths --------------------------------------------------------------------------------------------
async def test_malformed_payload_is_refused_after_auth_and_recorded_as_a_gap(client, container, org):
    pid, cid, secret, h = await make_source(client, org)
    junk = b'{"event_id":"a","message":"ok"}\nthis is not json'
    assert (await client.post(f"{API}/ingest/events/{cid}", content=junk, headers={"x-trace-signature": "sha256=00"})).status_code == 401
    assert await rows(container, "SELECT 1 FROM ingestion_batches WHERE project_id=%s", UUID(pid)) == []                       # unauthenticated: nothing recorded
    r = await push(client, cid, secret, junk)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_payload"
    b = (await rows(container, "SELECT reason, received, accepted FROM ingestion_batches WHERE project_id=%s", UUID(pid)))[0]
    assert b["reason"] == "malformed_payload" and b["accepted"] == 0
    assert await rows(container, "SELECT 1 FROM evidence_inbox WHERE project_id=%s", UUID(pid)) == []
    gaps = (await client.get(f"{API}/projects/{pid}/logs/status", headers=h)).json()["data"]["recent_gaps"]
    assert gaps[0]["reason"] == "malformed_payload" and gaps[0]["in_graph"] is False
    assert [g[0] for g in gap_specs({**b, "id": "b", "received": 0, "rejected": 0, "out_of_scope": 0, "truncated": 0, "unmapped": 0, "connection_id": cid, "via": "app_push"})] == ["rejected_input"]


async def test_oversize_body_is_refused_only_for_an_authenticated_sender(client, container, org):
    pid, cid, secret, h = await make_source(client, org)
    container.settings.ingest_max_body_bytes = 400
    big = [ev(i, message="x" * 50) for i in range(20)]
    body = json.dumps({"events": big}).encode()
    assert (await client.post(f"{API}/ingest/events/{cid}", content=body, headers={"x-trace-signature": "sha256=00"})).status_code == 401
    assert await rows(container, "SELECT 1 FROM ingestion_batches WHERE project_id=%s", UUID(pid)) == []
    r = await push(client, cid, secret, big)
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"
    b = (await rows(container, "SELECT reason, body_bytes, received FROM ingestion_batches WHERE project_id=%s", UUID(pid)))[0]
    assert b["reason"] == "oversize_body" and b["body_bytes"] == len(body)
    assert await rows(container, "SELECT 1 FROM evidence_inbox WHERE project_id=%s", UUID(pid)) == []                              # nothing partially stored
    assert [g[0] for g in gap_specs({**b, "id": "b", "rejected": 0, "out_of_scope": 0, "truncated": 0, "unmapped": 0, "connection_id": cid, "via": "app_push"})] == ["oversize_input"]
    container.settings.ingest_max_body_bytes = 1024 * 1024
    assert (await push(client, cid, secret, [ev(1)])).json()["accepted"] == 1                                                         # the limit is the only thing that changed


async def test_too_many_events_is_refused_whole(client, container, org):
    pid, cid, secret, h = await make_source(client, org)
    r = await push(client, cid, secret, [{"event_id": f"e{i}", "message": "m"} for i in range(MAX_EVENTS_PER_BATCH + 1)])
    assert r.status_code == 422 and r.json()["error"]["code"] == "batch_too_large"
    assert (await rows(container, "SELECT reason FROM ingestion_batches WHERE project_id=%s", UUID(pid)))[0]["reason"] == "batch_too_large"
    assert await rows(container, "SELECT 1 FROM evidence_inbox WHERE project_id=%s", UUID(pid)) == []


async def test_rejected_duplicate_truncated_and_unmapped_records_are_counted(client, container, org):
    pid, cid, secret, h = await make_source(client, org)
    long_msg = "y" * (MAX_MESSAGE + 500)
    batch = [ev(1), ev(1), ev(2, message=long_msg), ev(3, environment="nowhere"), {"event_id": "no-message"}, "not an object", {"message": "no id"}]
    r = (await push(client, cid, secret, batch)).json()
    assert r == {"received": 7, "accepted": 3, "duplicates": 1, "rejected": 3}
    b = (await rows(container, "SELECT * FROM ingestion_batches WHERE project_id=%s", UUID(pid)))[0]
    assert (b["rejected"], b["truncated"], b["unmapped"], b["duplicates"], b["via"]) == (3, 1, 1, 1, "app_push")
    kinds = sorted(g[0] for g in gap_specs({**b, "connection_id": cid}))
    assert kinds == ["rejected_input", "truncated_input", "unmapped_scope"]
    assert all(str(b["rejected"]) in d or str(b["truncated"]) in d or str(b["unmapped"]) in d for _, d, _ in gap_specs({**b, "connection_id": cid}))   # counts, never content
    stored = {x["source_record_key"]: x for x in await rows(container, "SELECT source_record_key, message, extra, environment_id FROM evidence_inbox WHERE project_id=%s", UUID(pid))}
    assert len(stored["evt-2"]["message"]) == MAX_MESSAGE and stored["evt-2"]["extra"]["message_truncated"] is True
    assert stored["evt-3"]["environment_id"] is None and stored["evt-1"]["environment_id"] is not None          # unknown environment stays unknown, not production
    gaps = (await client.get(f"{API}/projects/{pid}/logs/status", headers=h)).json()["data"]["recent_gaps"]
    assert (gaps[0]["rejected"], gaps[0]["truncated"], gaps[0]["unmapped"]) == (3, 1, 1)


async def test_late_and_out_of_order_events_keep_both_times(client, container, org):
    pid, cid, secret, h = await make_source(client, org)
    await push(client, cid, secret, [ev(30, at=T0 + timedelta(minutes=30))])
    await push(client, cid, secret, [ev(5, at=T0 + timedelta(minutes=5))])               # arrives later, happened earlier
    await push(client, cid, secret, [ev(99, at=None)])                                   # no time at all
    data = (await client.get(f"{API}/projects/{pid}/logs", headers=h)).json()["data"]
    by = {x["request_id"]: x for x in data}
    late, early, unknown = by["req-5"], by["req-30"], by["req-99"]
    assert late["event_time"].startswith("2026-10-16T18:35") and early["event_time"].startswith("2026-10-16T19:00")
    assert late["ingested_at"] > early["ingested_at"]                                    # received order is the opposite of event order, and both are kept
    assert unknown["event_time"] is None and unknown["ingested_at"]                      # ingest time is never copied into event time
    assert [x["request_id"] for x in data if x["event_time"]] == ["req-30", "req-5"]    # known times are ordered by event time, newest first
    assert data[0]["request_id"] == "req-99"                                              # the unknown-time row sorts by its ingest time (it was received last); it never gets a fake event time


async def test_cross_project_isolation(client, container, org):
    a_pid, a_cid, a_secret, ha = await make_source(client, org, name="IsoA")
    b_pid, b_cid, b_secret, hb = await make_source(client, org, name="IsoB")
    assert (await push(client, b_cid, a_secret, [ev(1)])).status_code == 401              # A's secret cannot write into B's source
    assert (await push(client, a_cid, b_secret, [ev(1)])).status_code == 401
    assert await rows(container, "SELECT 1 FROM evidence_inbox WHERE project_id=%s", UUID(b_pid)) == []
    assert (await push(client, a_cid, a_secret, [ev("shared", message="only in A")])).json()["accepted"] == 1
    assert (await push(client, b_cid, b_secret, [ev("shared", message="only in B")])).json()["accepted"] == 1   # same provider id, different project: both stored
    a_logs = (await client.get(f"{API}/projects/{a_pid}/logs", headers=ha)).json()
    b_logs = (await client.get(f"{API}/projects/{b_pid}/logs", headers=hb)).json()
    assert [x["message"] for x in a_logs["data"]] == ["only in A"] and [x["message"] for x in b_logs["data"]] == ["only in B"]
    assert a_logs["meta"]["coverage"]["stored_total"] == 1 and b_logs["meta"]["coverage"]["stored_total"] == 1
    envs_a = {e["id"] for e in await rows(container, "SELECT id FROM environments WHERE project_id=%s", UUID(a_pid))}
    assert all(r["environment_id"] in {UUID(str(i)) for i in envs_a} for r in await rows(container, "SELECT environment_id FROM evidence_inbox WHERE project_id=%s", UUID(a_pid)))
    # an event naming another project's environment key maps nowhere in its own project
    assert (await push(client, a_cid, a_secret, [ev("x", environment="staging")])).json()["accepted"] == 1
    # a user who only has a grant on B cannot read A's logs
    uid, email = await add_member(container, org["id"], "viewer")
    async with container.pool.connection() as conn:
        await conn.execute("INSERT INTO project_memberships(organization_id,project_id,user_id,role) VALUES (%s,%s,%s,'viewer')", (org["id"], UUID(b_pid), uid))
        await conn.commit()
    only_b = token_for(uid, email)
    assert (await client.get(f"{API}/projects/{b_pid}/logs", headers=only_b)).status_code == 200
    assert (await client.get(f"{API}/projects/{a_pid}/logs", headers=only_b)).status_code in (403, 404)


async def test_zero_rows_versus_no_source(client, container, org):
    pid, h = await make_project(client, org, name="NoSrc")
    cov = (await client.get(f"{API}/projects/{pid}/logs", headers=h)).json()["meta"]["coverage"]
    assert cov["empty_dataset"] is True and cov["source"]["configured"] is False                      # nothing stored because nothing is connected
    pid2, cid, secret, h2 = await make_source(client, org, name="HasSrc")
    r = await push(client, cid, secret, [])
    assert r.status_code == 200 and r.json() == {"received": 0, "accepted": 0, "duplicates": 0, "rejected": 0}
    cov2 = (await client.get(f"{API}/projects/{pid2}/logs", headers=h2)).json()["meta"]["coverage"]
    assert cov2["empty_dataset"] is True and cov2["source"] == {"configured": True, "app_sources": 1, "hosting_resources": 0}   # connected, but zero rows so far
    assert cov2["graph"]["received_through"] is None and cov2["graph"]["acknowledged_through"] is None and cov2["not_yet_in_graph"] == 0
    src = (await client.get(f"{API}/projects/{pid2}/log-source", headers=h2)).json()["data"]
    assert src["stored_events"] == 0 and src["last_batch"]["received"] == 0


async def test_drain_and_live_capture_account_for_their_input(client, container, org):
    pid, cid, h, vproj = await setup(client, container, org, RuntimeLogs())
    secret = await secret_for(client, cid, h)
    url = f"{API}/ingest/vercel/{cid}"
    # drain: malformed signed body, then a normal one with an out-of-scope project, a rejected record and a duplicate
    bad = b'{"id":"a","projectId":"prj_1"}\nnope'
    r = await client.post(url, content=bad, headers={"x-vercel-signature": vsig(secret, bad)})
    assert r.status_code == 422
    good = json.dumps([drain_rec(1), drain_rec(1), drain_rec(2, projectId="prj_OTHER"), {"id": "no-project"}]).encode()
    ok = await client.post(url, content=good, headers={"x-vercel-signature": vsig(secret, good)})
    assert ok.json() == {"received": 4, "accepted": 1, "duplicates": 1, "out_of_scope": 1, "rejected": 1}
    container.settings.ingest_max_body_bytes = 100
    assert (await client.post(url, content=good, headers={"x-vercel-signature": vsig(secret, good)})).status_code == 413
    container.settings.ingest_max_body_bytes = 1024 * 1024
    reasons = sorted(r["reason"] or "-" for r in await rows(container, "SELECT reason FROM ingestion_batches WHERE project_id=%s AND via='drain'", UUID(pid)))
    assert reasons == ["-", "malformed_payload", "oversize_body"]
    # live capture: a truncated line and a line that cannot be normalised
    class Lines(RuntimeLogs):
        LINES = [*RuntimeLogs.LINES, {**RuntimeLogs.LINES[1], "rowId": "3", "message": "cut off...", "messageTruncated": True}, {"level": "info", "rowId": "4", "message": None}]
    container.extras["http_transport"] = httpx.MockTransport(Lines().handler)
    assert (await client.post(f"{API}/projects/{pid}/logs/capture", headers=h, json={"seconds": 5})).status_code == 202
    while await worker.run_one(container, "t", KINDS):
        pass
    cap = (await rows(container, "SELECT accepted, rejected, truncated, via FROM ingestion_batches WHERE project_id=%s AND via='capture'", UUID(pid)))
    assert sum(c["accepted"] for c in cap) == 3 and sum(c["rejected"] for c in cap) == 1 and sum(c["truncated"] for c in cap) == 1
    st = (await client.get(f"{API}/projects/{pid}/logs/status", headers=h)).json()["data"]
    assert st["resources"][0]["in_graph"] == 0 and st["resources"][0]["oldest_not_in_graph"] is not None and st["graph"]["pending"] == 4
    assert {g["via"] for g in st["recent_gaps"]} == {"drain", "capture"}
    # the capture batches must not masquerade as the drain's last delivery
    assert st["drain"]["last_batch_at"] is not None and (await client.get(f"{API}/connections/{cid}/drain", headers=h)).json()["data"]["last_batch"]["received"] in (0, 4)


# ---- the applier protocol: claim, write, acknowledge ----------------------------------------------------------
async def test_rows_are_acknowledged_only_after_the_graph_write_and_the_cursor_follows(client, container, org, graph, monkeypatch):
    monkeypatch.setattr(ga, "BATCH", 1)
    pid, cid, secret, h = await make_source(client, org)
    for i in (1, 2, 3):
        await push(client, cid, secret, [ev(i)])                                              # three requests: three distinct receive times
    n = len(graph.bundles)
    s0 = await state(container, pid)
    assert s0["pending"] == 3 and s0["applied"] == 0 and s0["acknowledged_through"] == s0["oldest_unacknowledged_at"] != s0["received_through"]

    graph.error = ConnectionError("graph down")                                                # Neo4j unreachable: nothing may be acknowledged
    with pytest.raises(ConnectionError):
        await ga.apply_pending(container, org["id"], UUID(pid), "w1")
    s1 = await state(container, pid)
    assert s1["applied"] == 0 and s1["pending"] == 3 and s1["acknowledged_through"] == s0["acknowledged_through"]
    row = (await rows(container, "SELECT graph_attempts, graph_error, graph_next_attempt_at > now() AS backing_off FROM evidence_inbox WHERE project_id=%s AND graph_attempts>0", UUID(pid)))[0]
    assert row["graph_attempts"] == 1 and row["graph_error"] == "ConnectionError" and row["backing_off"] is True

    async with container.pool.connection() as conn:
        await conn.execute("UPDATE evidence_inbox SET graph_next_attempt_at=now() WHERE project_id=%s", (UUID(pid),))
        await conn.commit()
    graph.error = None
    out = await ga.apply_pending(container, org["id"], UUID(pid), "w1", max_batches=1)        # one batch of one row: the cursor moves exactly one row
    assert out["applied"] == 1 and out["remaining"] == 2
    s2 = await state(container, pid)
    first_two = sorted(x["ingested_at"] for x in await rows(container, "SELECT ingested_at FROM evidence_inbox WHERE project_id=%s", UUID(pid)))
    assert s2["applied"] == 1 and s2["pending"] == 2 and s2["acknowledged_through"] == first_two[1] and s2["acknowledged_through"] < s2["received_through"]

    out = await ga.apply_pending(container, org["id"], UUID(pid), "w1")
    s3 = await state(container, pid)
    assert out["remaining"] == 0 and s3["applied"] == 3 and s3["unacknowledged"] == 0 and s3["acknowledged_through"] == s3["received_through"]
    assert graph.keys[n:] and sorted(set(graph.keys[-3:])) == ["evt-1", "evt-2", "evt-3"]
    first = graph.bundles[-3].rows[0]
    assert first["event_time"] == T0 + timedelta(seconds=1) and first["ingested_at"] > first["event_time"] and first["environment_id"] is not None
    cov = (await client.get(f"{API}/projects/{pid}/logs", headers=h)).json()["meta"]["coverage"]
    assert cov["not_yet_in_graph"] == 0 and cov["graph"]["applied"] == 3 and cov["note"] is None


async def test_crash_after_graph_commit_is_replayed_once_the_lease_expires(client, container, org, graph):
    pid, cid, secret, h = await make_source(client, org)
    await push(client, cid, secret, [ev(1), ev(2)])
    async with container.pool.connection() as conn:
        dead = await ga.claim_rows(conn, UUID(pid), "dead-worker")                              # claimed, graph written, then the process died before acknowledging
    ids = [r["id"] for r in dead]
    assert len(dead) == 2 and (await state(container, pid))["claimed"] == 2
    async with container.pool.connection() as conn:
        assert await ga.claim_rows(conn, UUID(pid), "w2") == []                                 # a live lease is respected
    await reconcile_graph_work(container)
    assert [j["status"] for j in await rows(container, "SELECT status FROM jobs WHERE project_id=%s AND kind='graph.apply_evidence' AND status<>'succeeded'", UUID(pid))].count("queued") == 1   # the intake's own job only

    async with container.pool.connection() as conn:
        await conn.execute("UPDATE evidence_inbox SET graph_lease_expires_at = now() - interval '1 second' WHERE project_id=%s", (UUID(pid),))
        await conn.commit()
        taken = await ga.claim_rows(conn, UUID(pid), "w2")
    assert [r["id"] for r in taken] == ids and all(r["graph_attempts"] == 2 for r in taken)
    async with container.pool.connection() as conn:
        assert await ga.acknowledge(conn, ids, "dead-worker") == 0                              # the dead worker's late acknowledgement changes nothing
        assert await ga.acknowledge(conn, ids, "w2") == 2
    assert (await state(container, pid))["applied"] == 2


async def test_reconciler_queues_the_applier_for_abandoned_claims_and_the_worker_finishes_them(client, container, org, graph):
    pid, cid, secret, h = await make_source(client, org)
    await push(client, cid, secret, [ev(1), ev(2), ev(3)])
    await drain_jobs(container)                                                                 # the job the intake queued
    assert (await state(container, pid))["applied"] == 3
    await push(client, cid, secret, [ev(4)])
    async with container.pool.connection() as conn:
        await ga.claim_rows(conn, UUID(pid), "dead-worker")
        await conn.execute("UPDATE jobs SET status='cancelled' WHERE project_id=%s AND kind='graph.apply_evidence' AND status='queued'", (UUID(pid),))   # the queued job was lost too
        await conn.execute("UPDATE evidence_inbox SET graph_lease_expires_at = now() - interval '1 second' WHERE project_id=%s AND graph_status='claimed'", (UUID(pid),))
        await conn.commit()
    out = await reconcile_graph_work(container)
    assert out["evidence_projects"] >= 1
    assert len(await rows(container, "SELECT 1 FROM jobs WHERE project_id=%s AND kind='graph.apply_evidence' AND status='queued'", UUID(pid))) == 1
    await reconcile_graph_work(container)
    assert len(await rows(container, "SELECT 1 FROM jobs WHERE project_id=%s AND kind='graph.apply_evidence' AND status='queued'", UUID(pid))) == 1   # not duplicated
    await drain_jobs(container)
    s = await state(container, pid)
    assert s["applied"] == 4 and s["unacknowledged"] == 0
    assert graph.keys.count("evt-4") == 1


async def test_a_row_the_ontology_refuses_is_isolated_visible_and_retryable(client, container, org, graph):
    pid, cid, secret, h = await make_source(client, org)
    await push(client, cid, secret, [ev(1), ev(2), ev(3)])
    graph.poison = {"evt-2"}
    out = await ga.apply_pending(container, org["id"], UUID(pid), "w1")
    assert out["applied"] == 2 and out["failed"] == 1
    bad = (await rows(container, "SELECT source_record_key, graph_status, graph_error FROM evidence_inbox WHERE project_id=%s AND graph_status='failed'", UUID(pid)))[0]
    assert bad["source_record_key"] == "evt-2" and bad["graph_error"] == "GraphValidationError: LogEvent.severity: required"     # class and field names only, never a value
    s = await state(container, pid)
    assert s["applied"] == 2 and s["failed"] == 1 and s["acknowledged_through"] == s["oldest_unacknowledged_at"]   # a failed row holds the cursor back: it is not silently skipped
    cov = (await client.get(f"{API}/projects/{pid}/logs", headers=h)).json()["meta"]["coverage"]
    assert cov["not_yet_in_graph"] == 1 and cov["graph"]["failed"] == 1
    graph.poison = set()
    async with container.pool.connection() as conn:
        assert await ga.retry_failed(conn, UUID(pid)) == 1
    assert (await ga.apply_pending(container, org["id"], UUID(pid), "w1"))["applied"] == 1
    assert (await state(container, pid))["unacknowledged"] == 0


async def test_transient_failures_end_in_failed_not_in_an_endless_loop(client, container, org, graph):
    pid, cid, secret, h = await make_source(client, org)
    await push(client, cid, secret, [ev(1)])
    graph.error = TimeoutError("slow")
    for _ in range(ga.MAX_ATTEMPTS):
        async with container.pool.connection() as conn:
            await conn.execute("UPDATE evidence_inbox SET graph_next_attempt_at=now() WHERE project_id=%s", (UUID(pid),))
            await conn.commit()
        with pytest.raises(TimeoutError):
            await ga.apply_pending(container, org["id"], UUID(pid), "w1")
    s = await state(container, pid)
    assert s["failed"] == 1 and s["pending"] == 0
    # a lease that keeps expiring (the worker dies on this row every time) ends the same way
    pid2, cid2, secret2, _ = await make_source(client, org, name="Crashy")
    await push(client, cid2, secret2, [ev(1)])
    async with container.pool.connection() as conn:
        await conn.execute("UPDATE evidence_inbox SET graph_status='claimed', graph_attempts=%s, graph_lease_expires_at=now() - interval '1 second' WHERE project_id=%s", (ga.MAX_ATTEMPTS, UUID(pid2)))
        assert await ga.reap_stuck(conn) == 1
        await conn.commit()
    assert (await state(container, pid2))["failed"] == 1


async def test_scope_is_resolved_once_and_never_guessed(client, container, org, graph):
    pid, cid, secret, h = await make_source(client, org, service_key=None)
    await add_deployment(container, org["id"], pid, cid, dep_id="dpl_A", name="shopflow-api")
    await push(client, cid, secret, [ev(1, deployment_id="dpl_A"), ev(2, deployment_id="dpl_unknown"), ev(3)])
    await ga.apply_pending(container, org["id"], UUID(pid), "w1")
    by = {r["source_record_key"]: r for b in graph.bundles for r in b.rows}
    assert by["evt-1"]["graph_scope"] == {"service_key": "shopflow-api", "service_name": "shopflow-api", "service_via": "deployment_metadata", "deployment": "dpl_A"}
    assert by["evt-2"]["graph_scope"]["service_key"] is None and by["evt-2"]["graph_scope"]["deployment"] is None     # deployment TRACE has no row for: unknown stays unknown
    assert by["evt-3"]["graph_scope"]["service_key"] is None
    assert graph.bundles[0].deployments["dpl_A"]["provider_project_id"] == "prj_api"
    # the decision is stored on the row, so a retry cannot write different content even if more is known by then
    await add_deployment(container, org["id"], pid, cid, dep_id="dpl_unknown", provider_project="prj_x", name="late-service")
    async with container.pool.connection() as conn:
        await conn.execute("UPDATE evidence_inbox SET graph_status='pending', graph_next_attempt_at=now() WHERE project_id=%s AND source_record_key='evt-2'", (UUID(pid),))
        await conn.commit()
    await ga.apply_pending(container, org["id"], UUID(pid), "w1")
    assert graph.bundles[-1].rows[0]["graph_scope"]["service_key"] is None


async def test_declared_service_applies_only_when_no_deployment_places_the_event(client, container, org, graph):
    pid, cid, secret, h = await make_source(client, org, service_key="api", services=["api", "worker"])
    assert (await client.get(f"{API}/projects/{pid}/log-source", headers=h)).json()["data"]["service_key"] == "api"
    assert (await client.post(f"{API}/projects/{pid}/log-source", headers=h, json={"service_key": "nope"})).status_code == 422
    await add_deployment(container, org["id"], pid, cid, dep_id="dpl_A", name="shopflow-api")
    await push(client, cid, secret, [ev(1, deployment_id="dpl_A"), ev(2)])
    await ga.apply_pending(container, org["id"], UUID(pid), "w1")
    by = {r["source_record_key"]: r["graph_scope"] for b in graph.bundles for r in b.rows}
    assert by["evt-1"]["service_via"] == "deployment_metadata" and by["evt-1"]["service_key"] == "shopflow-api"          # a provider fact beats the declaration
    assert by["evt-2"]["service_via"] == "source_scope" and by["evt-2"]["service_key"] == "api"


async def test_log_scope_places_drained_logs_on_the_hosting_project(client, container, org, graph):
    pid, cid, h, vproj = await setup(client, container, org, RuntimeLogs())
    secret = await secret_for(client, cid, h)
    body = json.dumps([drain_rec(1)]).encode()
    await client.post(f"{API}/ingest/vercel/{cid}", content=body, headers={"x-vercel-signature": vsig(secret, body)})
    await ga.apply_pending(container, org["id"], UUID(pid), "w1")
    sc = graph.bundles[0].rows[0]["graph_scope"]
    assert sc["service_via"] == "log_scope" and sc["service_key"] and sc["deployment"] is None                           # deployment dpl_1 is not a stored deployment row: no edge to it


# ---- outbox applier (T-006) ------------------------------------------------------------------------------------
async def test_outbox_rows_are_applied_after_the_graph_write_and_survive_a_crash(client, container, org, monkeypatch):
    container.extras["neo4j"] = object()
    seen = []
    mode = {"fail": None}

    def fake_scope(repo, scope):
        seen.append(scope)
        if mode["fail"]:
            raise mode["fail"]
        return {"environments": len(scope["environments"]), "services": len(scope["services"])}

    monkeypatch.setattr(ob, "project_scope", fake_scope)
    pid, h = await make_project(client, org, name="Ob", services=["api"])
    pending = await rows(container, "SELECT status, kind FROM outbox WHERE project_id=%s", UUID(pid))
    assert [(r["status"], r["kind"]) for r in pending] == [("pending", "project_scope")]               # written in the project's own transaction

    mode["fail"] = ConnectionError("graph down")
    with pytest.raises(ConnectionError):
        await ob.apply_outbox(container, org["id"], "w1")
    r = (await rows(container, "SELECT status, attempts, last_error, next_attempt_at > now() AS backing_off FROM outbox WHERE project_id=%s", UUID(pid)))[0]
    assert (r["status"], r["attempts"], r["last_error"], r["backing_off"]) == ("pending", 1, "ConnectionError", True)

    mode["fail"] = None
    async with container.pool.connection() as conn:
        await conn.execute("UPDATE outbox SET next_attempt_at=now() WHERE project_id=%s", (UUID(pid),))
        await conn.commit()
        dead = await ob.claim(conn, org["id"], "dead-worker")                                           # claimed, then the worker died
    assert len(dead) == 1
    async with container.pool.connection() as conn:
        assert await ob.claim(conn, org["id"], "w2") == []
        await conn.execute("UPDATE outbox SET lease_expires_at = now() - interval '1 second' WHERE project_id=%s", (UUID(pid),))
        await conn.commit()
    await reconcile_graph_work(container)
    assert await rows(container, "SELECT 1 FROM jobs WHERE organization_id=%s AND kind='graph.apply_outbox' AND status='queued'", org["id"])
    await drain_jobs(container, OUTBOX)
    r = (await rows(container, "SELECT status, applied_at FROM outbox WHERE project_id=%s", UUID(pid)))[0]
    assert r["status"] == "applied" and r["applied_at"]
    scope = [x for x in seen if x["project_id"] == pid][-1]
    assert scope["name"].startswith("Ob") and [e["environment_key"] for e in scope["environments"]] == ["production", "staging"] and [s["service_key"] for s in scope["services"]] == ["api"]
    assert await ob.apply_outbox(container, org["id"], "w3") == {"applied": 0, "failed": 0, "projects": 0}   # nothing left: replay is a no-op


async def test_unknown_outbox_kind_is_failed_visibly(client, container, org, monkeypatch):
    container.extras["neo4j"] = object()
    monkeypatch.setattr(ob, "project_scope", lambda repo, scope: {})
    pid, h = await make_project(client, org, name="ObK")
    async with container.pool.connection() as conn:
        await conn.execute("UPDATE outbox SET status='applied' WHERE project_id=%s", (UUID(pid),))
        await conn.execute("INSERT INTO outbox(organization_id,project_id,kind,operation_key,payload) VALUES (%s,%s,'mystery',%s,'{}'::jsonb)", (org["id"], UUID(pid), f"mystery:{pid}"))
        await conn.commit()
    out = await ob.apply_outbox(container, org["id"], "w1")
    assert out["failed"] == 1
    assert (await rows(container, "SELECT status, last_error FROM outbox WHERE kind='mystery' AND project_id=%s", UUID(pid)))[0] == {"status": "failed", "last_error": "unknown outbox kind"}


def test_signature_helpers_are_the_ones_the_endpoint_uses():
    from app.api.v1.log_source import signature_ok

    assert signature_ok("s", b"body", sig("s", b"body")) and not signature_ok("s", b"body", sig("t", b"body"))
