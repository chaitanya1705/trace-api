"""Worker loop: claim -> run handler with a heartbeat -> complete/fail. Shares code with the API process (A1)."""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from psycopg_pool import AsyncConnectionPool

from app.container import Container
from app.jobs import queue
from app.jobs.queue import JobCancelled, PermanentJobError, TransientJobError

log = logging.getLogger("trace.worker")


@dataclass
class JobContext:
    container: Container
    job: dict[str, Any]
    owner: str
    _cancel: asyncio.Event

    async def check_cancel(self) -> None:
        """Handlers call this between provider calls/batches; cancellation never interrupts a write mid-flight."""
        if self._cancel.is_set():
            raise JobCancelled()

    async def progress(self, stage: str, progress: dict | None = None, warnings: list | None = None) -> None:
        async with self.container.pool.connection() as conn:
            await queue.set_progress(conn, self.job["id"], self.owner, stage=stage, progress=progress, warnings=warnings)


Handler = Callable[[JobContext], Awaitable[dict | None]]
HANDLERS: dict[str, Handler] = {}


def handler(kind: str):
    def deco(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn
    return deco


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


async def _heartbeat(pool: AsyncConnectionPool, job_id, owner, lease: int, every: int, cancel: asyncio.Event,
                     lost: asyncio.Event) -> None:
    while True:
        await asyncio.sleep(every)
        async with pool.connection() as conn:
            status = await queue.heartbeat(conn, job_id, owner, lease)
        if status is None:
            lost.set()
            cancel.set()
            return
        if status == "cancel_requested":
            cancel.set()


async def run_one(container: Container, owner: str, kinds: list[str] | None = None) -> bool:
    """Process at most one job. Returns False when the queue had nothing runnable."""
    s = container.settings
    async with container.pool.connection() as conn:
        job = await queue.claim(conn, owner, kinds or list(HANDLERS), s.job_lease_seconds)
    if job is None:
        return False
    cancel, lost = asyncio.Event(), asyncio.Event()
    hb = asyncio.create_task(_heartbeat(container.pool, job["id"], owner, s.job_lease_seconds,
                                        s.job_heartbeat_seconds, cancel, lost))
    ctx = JobContext(container, job, owner, cancel)
    try:
        result = await HANDLERS[job["kind"]](ctx)
        if lost.is_set():
            return True  # lease taken over; the new owner reconciles
        async with container.pool.connection() as conn:
            await queue.complete(conn, job["id"], owner, result)
    except JobCancelled as e:
        async with container.pool.connection() as conn:
            await queue.mark_cancelled(conn, job["id"], owner, result_ref=getattr(e, "result_ref", None))
    except PermanentJobError as e:
        async with container.pool.connection() as conn:
            await queue.fail(conn, job["id"], owner, code=e.code, message=e.message, retryable=False,
                             result_ref=getattr(e, "result_ref", None))
    except TransientJobError as e:
        async with container.pool.connection() as conn:
            await queue.fail(conn, job["id"], owner, code=e.code, message=e.message, retryable=True,
                             retry_after=e.retry_after, result_ref=getattr(e, "result_ref", None))
    except Exception as e:  # noqa: BLE001 - unknown failures are retried a bounded number of times
        log.exception("job crashed", extra={"job_id": str(job["id"]), "kind": job["kind"]})
        async with container.pool.connection() as conn:
            await queue.fail(conn, job["id"], owner, code="internal_error", message=type(e).__name__, retryable=True)
    finally:
        hb.cancel()
    return True


RECONCILE_SECONDS = 30.0


async def run_forever(container: Container, poll_seconds: float = 1.0) -> None:
    from app.jobs.reconcile import reconcile_graph_work

    owner = worker_id()
    last_reconcile = float("-inf")
    while True:
        async with container.pool.connection() as conn:
            await queue.reap_expired(conn)
        if time.monotonic() - last_reconcile >= RECONCILE_SECONDS:
            last_reconcile = time.monotonic()
            try:
                await reconcile_graph_work(container)
            except Exception:  # noqa: BLE001 - reconciling is best effort; the next pass tries again
                log.exception("graph reconcile failed")
        worked = await run_one(container, owner)
        if not worked:
            await asyncio.sleep(poll_seconds)
