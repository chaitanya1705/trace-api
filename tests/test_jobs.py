import asyncio

import pytest

from app.jobs import queue, worker
from app.jobs.queue import PermanentJobError, TransientJobError, backoff_seconds

pytestmark = pytest.mark.db


async def _enq(container, org, kind="t.kind", **kw):
    async with container.pool.connection() as conn:
        row = await queue.enqueue(conn, organization_id=org["id"], kind=kind, **kw)
        await conn.commit()
    return row


async def _get(container, job_id):
    async with container.pool.connection() as conn:
        return await (await conn.execute("SELECT * FROM jobs WHERE id=%s", (job_id,))).fetchone()


def test_backoff_grows_jitters_and_honors_retry_after():
    assert backoff_seconds(1, rng=lambda: 0.0) == 1.0 and backoff_seconds(3, rng=lambda: 1.0) == 8.0
    assert backoff_seconds(1, retry_after=90.0) == 90.0
    assert backoff_seconds(30, rng=lambda: 1.0) == 300.0


async def test_idempotent_enqueue_returns_same_job(container, org):
    a = await _enq(container, org, idempotency_key="k1")
    b = await _enq(container, org, idempotency_key="k1")
    assert a["id"] == b["id"]


async def test_concurrent_claims_never_double_run(container, org):
    for i in range(6):
        await _enq(container, org, kind="claim.race", payload={"i": i})

    async def grab(name):
        got = []
        while True:
            async with container.pool.connection() as conn:
                j = await queue.claim(conn, name, ["claim.race"], 30)
            if j is None:
                return got
            got.append(j["id"])

    results = await asyncio.gather(*[grab(f"w{i}") for i in range(3)])
    flat = [j for r in results for j in r]
    assert len(flat) == 6 and len(set(flat)) == 6


async def test_priority_order(container, org):
    low = await _enq(container, org, kind="prio.k", priority=queue.PRIORITY_BULK)
    high = await _enq(container, org, kind="prio.k", priority=queue.PRIORITY_HUMAN)
    async with container.pool.connection() as conn:
        first = await queue.claim(conn, "w", ["prio.k"], 30)
    assert first["id"] == high["id"] and first["id"] != low["id"]


async def test_retry_then_dead_letter_and_permanent_failure(container, org):
    calls = {"n": 0}

    @worker.handler("flaky.k")
    async def flaky(ctx):
        calls["n"] += 1
        raise TransientJobError("upstream", "503 from provider", retry_after=0)

    @worker.handler("perm.k")
    async def perm(ctx):
        raise PermanentJobError("bad_credentials", "401")

    j = await _enq(container, org, kind="flaky.k", max_attempts=2)
    for _ in range(2):  # make the retry immediately eligible
        await worker.run_one(container, "w", ["flaky.k"])
        async with container.pool.connection() as conn:
            await conn.execute("UPDATE jobs SET next_attempt_at = now() WHERE id=%s", (j["id"],))
            await conn.commit()
    row = await _get(container, j["id"])
    assert row["status"] == "dead" and calls["n"] == 2 and row["error"]["retryable"] is True

    p = await _enq(container, org, kind="perm.k")
    await worker.run_one(container, "w", ["perm.k"])
    row = await _get(container, p["id"])
    assert row["status"] == "failed" and row["attempt"] == 1  # permanent errors are not retried


async def test_expired_lease_is_requeued_and_stale_owner_cannot_complete(container, org):
    j = await _enq(container, org, kind="lease.k")
    async with container.pool.connection() as conn:
        await queue.claim(conn, "crashed", ["lease.k"], 30)
        await conn.execute("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id=%s", (j["id"],))
        await conn.commit()
        assert await queue.reap_expired(conn) >= 1
        assert (await queue.claim(conn, "healthy", ["lease.k"], 30))["lease_owner"] == "healthy"
        assert await queue.complete(conn, j["id"], "crashed") is False   # stale owner loses
        assert await queue.complete(conn, j["id"], "healthy") is True


async def test_cancel_queued_immediately_and_running_cooperatively(container, org):
    seen = {}

    @worker.handler("cancel.k")
    async def cancellable(ctx):
        seen["started"] = True
        async with container.pool.connection() as conn:
            await queue.request_cancel(conn, ctx.job["id"], ctx.job["organization_id"])
        ctx._cancel.set()  # what the heartbeat does after observing cancel_requested
        await ctx.check_cancel()
        raise AssertionError("must not continue after cancellation")

    q = await _enq(container, org, kind="cancel.queued")
    async with container.pool.connection() as conn:
        assert await queue.request_cancel(conn, q["id"], org["id"]) == "cancelled"
    j = await _enq(container, org, kind="cancel.k")
    await worker.run_one(container, "w", ["cancel.k"])
    assert seen["started"] and (await _get(container, j["id"]))["status"] == "cancelled"


async def test_cancel_for_resource_stops_queued_work(container, org):
    import uuid
    rid = uuid.uuid4()
    j = await _enq(container, org, kind="res.k", resource_type="connection", resource_id=rid)
    async with container.pool.connection() as conn:
        assert await queue.cancel_for_resource(conn, "connection", rid) == 1
        await conn.commit()
    assert (await _get(container, j["id"]))["status"] == "cancelled"
