"""Open tickets automatically when the monitored application reports failing requests through its signed log source.

Only app-reported (trusted) ERROR/FATAL events count; browser observations never open a ticket. One failed request is
one occurrence, named by its request-level event (ShopFlow's `request.failed`/`request.timed_out`: operation
"POST /api/checkout", error type). Occurrences with the same environment, operation and error type fold into the one
open ticket for that failure, which keeps a count, the first/last time seen and recent request ids. A resolved or closed
ticket is never reopened: the next occurrence opens a new ticket. Nothing here starts an investigation (R-004).
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from psycopg import AsyncConnection

from app.audit import audit
from app.domain.ingest.vercel_logs import LogRow
from app.jobs.triggers import request_graph_sync

SOURCE = "webhook"
PREFIX = "app-errors:"
PAD = timedelta(minutes=5)
MAX_REQUESTS = 20


def _occurrences(rows: list[LogRow]) -> list[LogRow]:
    """One headline event per failed request: the request-level event when present, else the last error seen."""
    errors = [r for r in rows if r.source != "browser" and r.level in ("error", "fatal")]
    by_request: dict[str, LogRow] = {}
    for r in errors:
        k = r.request_id or r.source_record_key
        cur = by_request.get(k)
        if cur is None or str(r.extra.get("event_name") or "").startswith("request.") or not str(cur.extra.get("event_name") or "").startswith("request."):
            by_request[k] = r
    return list(by_request.values())


def _signature(r: LogRow) -> tuple[str, str, str]:
    return (r.env_key or "", r.service_hint or "unknown operation", str(r.extra.get("error_type") or "error"))


async def open_error_tickets(conn: AsyncConnection, *, organization_id: UUID, project_id: UUID, rows: list[LogRow]) -> list[UUID]:
    """Create or update tickets for the failed requests in one accepted batch. Returns the ticket ids touched."""
    groups: dict[tuple[str, str, str], list[LogRow]] = {}
    for r in _occurrences(rows):
        groups.setdefault(_signature(r), []).append(r)
    touched: list[UUID] = []
    now = datetime.now(UTC)
    for (env, op, etype), occ in groups.items():
        times = [r.event_time for r in occ if r.event_time] or [now]
        first, last = min(times), max(times)
        key = PREFIX + hashlib.sha256(json.dumps([str(project_id), env, op, etype]).encode()).hexdigest()[:24]
        req_ids = [r.request_id for r in occ if r.request_id]
        # Serialize concurrent batches for the same failure so they fold into one ticket.
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
        open_row = await (await conn.execute(
            "SELECT * FROM tickets WHERE organization_id=%s AND source=%s AND external_id LIKE %s AND status='open'"
            " ORDER BY created_at DESC LIMIT 1 FOR UPDATE", (organization_id, SOURCE, key + "%"))).fetchone()
        if open_row is not None:
            s = open_row["evidence_summary"] or {}
            recent = (req_ids + [x for x in s.get("recent_request_ids", []) if x not in req_ids])[:MAX_REQUESTS]
            seen_last = max(last, datetime.fromisoformat(s["last_seen"])) if s.get("last_seen") else last
            s.update(occurrences=int(s.get("occurrences", 0)) + len(occ), last_seen=seen_last.isoformat(), recent_request_ids=recent)
            await conn.execute(
                "UPDATE tickets SET evidence_summary=%s::jsonb, window_end=GREATEST(window_end, %s) WHERE id=%s",
                (json.dumps(s), seen_last + PAD, open_row["id"]))
            touched.append(open_row["id"])
            continue
        n = (await (await conn.execute(
            "SELECT count(*) AS n FROM tickets WHERE organization_id=%s AND source=%s AND external_id LIKE %s",
            (organization_id, SOURCE, key + "%"))).fetchone())["n"]
        head = occ[0]
        where = f" in {env}" if env else ""
        title = f"{op} failing{where}: {etype}"[:300]
        description = (
            f"Opened automatically by TRACE from errors the application reported through its log source.\n"
            f"Operation: {op}\nError type: {etype}\nEnvironment: {env or 'unknown'}\n"
            f"First seen: {first.isoformat()}\nApplication message: {head.message or ''}\n"
            "Severity is not set: rate it after review. Investigate when ready; nothing starts on its own."
        )
        summary = {"auto": True, "occurrences": len(occ), "first_seen": first.isoformat(), "last_seen": last.isoformat(),
                   "operation": op, "error_type": etype, "recent_request_ids": req_ids[:MAX_REQUESTS]}
        fields = {"title": title, "description": description, "severity": None, "environment_key": env or None,
                  "service_hint": op, "window_start": (first - PAD).isoformat(), "window_end": (last + PAD).isoformat()}
        h = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
        row = await (await conn.execute(
            "INSERT INTO tickets(organization_id,project_id,source,external_id,title,description,severity,environment_key,service_hint,"
            " source_created_at,window_start,window_end,content_hash,evidence_summary,evidence_sync)"
            " VALUES (%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s,%s,%s,%s::jsonb,'not_required') RETURNING id",
            (organization_id, project_id, SOURCE, f"{key}:{n + 1}", title, description, env or None, op, first,
             first - PAD, last + PAD, h, json.dumps(summary)))).fetchone()
        await conn.execute("INSERT INTO ticket_revisions(ticket_id,revision_number,content_hash,fields) VALUES (%s,1,%s,%s::jsonb)",
                           (row["id"], h, json.dumps(fields)))
        await audit(conn, organization_id=organization_id, project_id=project_id, actor=None, action="ticket.auto_opened",
                    target_type="ticket", target_id=row["id"], details={"operation": op, "error_type": etype, "occurrences": len(occ)})
        touched.append(row["id"])
    if touched:
        await request_graph_sync(conn, organization_id=organization_id, project_id=project_id)
    return touched
