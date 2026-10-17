"""Durable Postgres queue (A7): SKIP LOCKED claim, leases, heartbeats, bounded retries with jitter, dead-letter,
cancellation. No transaction is held open across handler work: every function here is one short transaction.
"""
from __future__ import annotations

import json
import random
from datetime import timedelta
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

# Lower runs first: human resume/cancel < light ingestion < bulk rebuild (A7)
PRIORITY_HUMAN = 10
PRIORITY_INGEST = 50
PRIORITY_DEFAULT = 100
PRIORITY_BULK = 200

NON_RETRYABLE = "non_retryable"


class JobCancelled(Exception):
    """Raised by handlers at a safe point when cancellation was requested."""


class PermanentJobError(Exception):
    """Validation/auth failures: pause/fail, never retried endlessly."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class TransientJobError(Exception):
    def __init__(self, code: str, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.code, self.message, self.retry_after = code, message, retry_after


def backoff_seconds(attempt: int, retry_after: float | None = None, *, rng=random.random) -> float:
    """Exponential with full jitter, honoring Retry-After when the provider supplied one."""
    base = min(300.0, 2.0 ** attempt)
    wait = base * (0.5 + rng() / 2)
    return max(wait, retry_after or 0.0)


async def enqueue(
    conn: AsyncConnection,
    *,
    organization_id: UUID,
    kind: str,
    payload: dict[str, Any] | None = None,
    project_id: UUID | None = None,
    requested_by: UUID | None = None,
    idempotency_key: str | None = None,
    priority: int = PRIORITY_DEFAULT,
    resource_type: str | None = None,
    resource_id: UUID | None = None,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Idempotent: the same (org, kind, key) returns the existing job instead of a duplicate."""
    row = await (
        await conn.execute(
            "INSERT INTO jobs(organization_id,project_id,kind,payload,idempotency_key,priority,requested_by,"
            " resource_type,resource_id,max_attempts) VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (organization_id, kind, idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING"
            " RETURNING *",
            (organization_id, project_id, kind, json.dumps(payload or {}), idempotency_key, priority,
             requested_by, resource_type, resource_id, max_attempts),
        )
    ).fetchone()
    if row is None:
        row = await (
            await conn.execute(
                "SELECT * FROM jobs WHERE organization_id=%s AND kind=%s AND idempotency_key=%s",
                (organization_id, kind, idempotency_key),
            )
        ).fetchone()
    return row


async def claim(conn: AsyncConnection, owner: str, kinds: list[str], lease_seconds: int) -> dict[str, Any] | None:
    row = await (
        await conn.execute(
            "WITH next AS (SELECT id FROM jobs WHERE status='queued' AND next_attempt_at <= now() AND kind = ANY(%s)"
            " ORDER BY priority, next_attempt_at FOR UPDATE SKIP LOCKED LIMIT 1)"
            " UPDATE jobs j SET status='running', attempt=j.attempt+1, lease_owner=%s,"
            " lease_expires_at=now() + make_interval(secs => %s), started_at=COALESCE(j.started_at, now())"
            " FROM next WHERE j.id = next.id RETURNING j.*",
            (kinds, owner, lease_seconds),
        )
    ).fetchone()
    await conn.commit()
    return row


async def heartbeat(conn: AsyncConnection, job_id: UUID, owner: str, lease_seconds: int) -> str | None:
    """Extends the lease. Returns the job status so the worker can notice cancel_requested; None = lease lost."""
    row = await (
        await conn.execute(
            "UPDATE jobs SET lease_expires_at = now() + make_interval(secs => %s)"
            " WHERE id=%s AND lease_owner=%s AND status IN ('running','cancel_requested') RETURNING status",
            (lease_seconds, job_id, owner),
        )
    ).fetchone()
    await conn.commit()
    return row["status"] if row else None


async def set_progress(conn: AsyncConnection, job_id: UUID, owner: str, *, stage: str,
                       progress: dict | None = None, warnings: list | None = None) -> None:
    await conn.execute(
        "UPDATE jobs SET stage=%s, progress=COALESCE(%s::jsonb, progress), warnings=COALESCE(%s::jsonb, warnings)"
        " WHERE id=%s AND lease_owner=%s",
        (stage, None if progress is None else json.dumps(progress), None if warnings is None else json.dumps(warnings),
         job_id, owner),
    )
    await conn.commit()


async def complete(conn: AsyncConnection, job_id: UUID, owner: str, result_ref: dict | None = None) -> bool:
    row = await (
        await conn.execute(
            "UPDATE jobs SET status='succeeded', finished_at=now(), lease_owner=NULL, lease_expires_at=NULL,"
            " result_ref=%s::jsonb WHERE id=%s AND lease_owner=%s AND status='running' RETURNING id",
            (json.dumps(result_ref) if result_ref is not None else None, job_id, owner),
        )
    ).fetchone()
    await conn.commit()
    return row is not None


async def fail(
    conn: AsyncConnection, job_id: UUID, owner: str, *, code: str, message: str,
    retryable: bool, retry_after: float | None = None,
) -> str:
    """Returns the new status: queued (will retry), failed (permanent) or dead (attempts exhausted)."""
    job = await (await conn.execute("SELECT attempt, max_attempts FROM jobs WHERE id=%s AND lease_owner=%s FOR UPDATE",
                                    (job_id, owner))).fetchone()
    if job is None:
        await conn.rollback()
        return "lost"
    err = json.dumps({"code": code, "message": message[:500], "retryable": retryable})
    if retryable and job["attempt"] < job["max_attempts"]:
        status = "queued"
        await conn.execute(
            "UPDATE jobs SET status='queued', lease_owner=NULL, lease_expires_at=NULL, error=%s::jsonb,"
            " next_attempt_at = now() + make_interval(secs => %s) WHERE id=%s",
            (err, backoff_seconds(job["attempt"], retry_after), job_id))
    else:
        status = "dead" if retryable else "failed"
        await conn.execute(
            "UPDATE jobs SET status=%s, finished_at=now(), lease_owner=NULL, lease_expires_at=NULL, error=%s::jsonb WHERE id=%s",
            (status, err, job_id))
    await conn.commit()
    return status


async def mark_cancelled(conn: AsyncConnection, job_id: UUID, owner: str) -> None:
    await conn.execute(
        "UPDATE jobs SET status='cancelled', finished_at=now(), lease_owner=NULL, lease_expires_at=NULL"
        " WHERE id=%s AND lease_owner=%s", (job_id, owner))
    await conn.commit()


async def request_cancel(conn: AsyncConnection, job_id: UUID, organization_id: UUID) -> str | None:
    """Queued jobs cancel immediately; running ones are flagged and acknowledged by the worker (completed evidence is
    kept). Returns the new status, or None if the job is absent/terminal."""
    row = await (
        await conn.execute(
            "UPDATE jobs SET status = CASE WHEN status='queued' THEN 'cancelled' ELSE 'cancel_requested' END,"
            " finished_at = CASE WHEN status='queued' THEN now() ELSE finished_at END"
            " WHERE id=%s AND organization_id=%s AND status IN ('queued','running') RETURNING status",
            (job_id, organization_id),
        )
    ).fetchone()
    await conn.commit()
    return row["status"] if row else None


async def cancel_for_resource(conn: AsyncConnection, resource_type: str, resource_id: UUID) -> int:
    """Disconnect/provider switch must not leave old jobs running silently."""
    cur = await conn.execute(
        "UPDATE jobs SET status = CASE WHEN status='queued' THEN 'cancelled' ELSE 'cancel_requested' END,"
        " finished_at = CASE WHEN status='queued' THEN now() ELSE finished_at END"
        " WHERE resource_type=%s AND resource_id=%s AND status IN ('queued','running')",
        (resource_type, resource_id))
    return cur.rowcount


async def reap_expired(conn: AsyncConnection) -> int:
    """Expired leases: a crashed worker's job is requeued (handlers must reconcile prior side effects before
    retry) or dead-lettered when attempts are exhausted; cancel_requested ones become cancelled."""
    cur = await conn.execute(
        "UPDATE jobs SET lease_owner=NULL, lease_expires_at=NULL,"
        " status = CASE WHEN status='cancel_requested' THEN 'cancelled'"
        "               WHEN attempt >= max_attempts THEN 'dead' ELSE 'queued' END,"
        " finished_at = CASE WHEN status='cancel_requested' OR attempt >= max_attempts THEN now() ELSE finished_at END,"
        " error = CASE WHEN status='running' THEN '{\"code\":\"lease_expired\",\"message\":\"worker stopped responding\","
        "\"retryable\":true}'::jsonb ELSE error END"
        " WHERE status IN ('running','cancel_requested') AND lease_expires_at < now()")
    await conn.commit()
    return cur.rowcount


def lease_delta(seconds: int) -> timedelta:
    return timedelta(seconds=seconds)
