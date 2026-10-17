"""Open tickets automatically when the monitored application reports failing requests through its signed log source.

Only app-reported (trusted) ERROR/FATAL events count; browser observations never open a ticket. One failed request is
one occurrence, named by its request-level event (ShopFlow's `request.failed`/`request.timed_out`: operation
"POST /api/checkout", error type). A ticket covers one failure (environment, operation, error type) for one minute
(user, 2026-10-17): an occurrence within WINDOW of an open ticket's first occurrence folds into it (count, last seen,
recent request ids); a later one opens a new ticket. A resolved or closed ticket is never reopened. Nothing here starts
an investigation (R-004).
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
WINDOW = timedelta(minutes=1)
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


async def _fold(conn: AsyncConnection, ticket: dict, at: datetime, request_id: str | None) -> None:
    s = ticket["evidence_summary"] or {}
    recent = ([request_id] if request_id else []) + [x for x in s.get("recent_request_ids", []) if x != request_id]
    last = max(at, datetime.fromisoformat(s["last_seen"])) if s.get("last_seen") else at
    if s.get("first_seen") and at < datetime.fromisoformat(s["first_seen"]):
        s["first_seen"] = at.isoformat()
    s.update(occurrences=int(s.get("occurrences", 0)) + 1, last_seen=last.isoformat(), recent_request_ids=recent[:MAX_REQUESTS])
    await conn.execute("UPDATE tickets SET evidence_summary=%s::jsonb, window_end=GREATEST(window_end, %s) WHERE id=%s",
                       (json.dumps(s), last + PAD, ticket["id"]))
    ticket["evidence_summary"] = s


async def _open(conn: AsyncConnection, *, organization_id: UUID, project_id: UUID, key: str, sig: tuple[str, str, str], at: datetime, r: LogRow) -> dict:
    env, op, etype = sig
    n = (await (await conn.execute(
        "SELECT count(*) AS n FROM tickets WHERE organization_id=%s AND source=%s AND external_id LIKE %s",
        (organization_id, SOURCE, key + "%"))).fetchone())["n"]
    where = f" in {env}" if env else ""
    title = f"{op} failing{where}: {etype} ({at.strftime('%H:%M')} UTC)"[:300]
    description = (
        f"Opened automatically by TRACE from errors the application reported through its log source.\n"
        f"Operation: {op}\nError type: {etype}\nEnvironment: {env or 'unknown'}\n"
        f"First seen: {at.isoformat()}\nApplication message: {r.message or ''}\n"
        f"Errors of this kind within {int(WINDOW.total_seconds())} seconds of the first one are added here; later ones open a new ticket.\n"
        "Severity is not set: rate it after review. Investigate when ready; nothing starts on its own."
    )
    summary = {"auto": True, "occurrences": 1, "first_seen": at.isoformat(), "last_seen": at.isoformat(), "window_seconds": int(WINDOW.total_seconds()),
               "operation": op, "error_type": etype, "recent_request_ids": [r.request_id] if r.request_id else []}
    fields = {"title": title, "description": description, "severity": None, "environment_key": env or None,
              "service_hint": op, "window_start": (at - PAD).isoformat(), "window_end": (at + PAD).isoformat()}
    h = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
    row = await (await conn.execute(
        "INSERT INTO tickets(organization_id,project_id,source,external_id,title,description,severity,environment_key,service_hint,"
        " source_created_at,window_start,window_end,content_hash,evidence_summary,evidence_sync)"
        " VALUES (%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s,%s,%s,%s::jsonb,'not_required') RETURNING *",
        (organization_id, project_id, SOURCE, f"{key}:{n + 1}", title, description, env or None, op, at,
         at - PAD, at + PAD, h, json.dumps(summary)))).fetchone()
    await conn.execute("INSERT INTO ticket_revisions(ticket_id,revision_number,content_hash,fields) VALUES (%s,1,%s,%s::jsonb)",
                       (row["id"], h, json.dumps(fields)))
    await audit(conn, organization_id=organization_id, project_id=project_id, actor=None, action="ticket.auto_opened",
                target_type="ticket", target_id=row["id"], details={"operation": op, "error_type": etype})
    return dict(row)


async def open_error_tickets(conn: AsyncConnection, *, organization_id: UUID, project_id: UUID, rows: list[LogRow]) -> list[UUID]:
    """Create or update tickets for the failed requests in one accepted batch. Returns the ticket ids touched."""
    groups: dict[tuple[str, str, str], list[LogRow]] = {}
    for r in _occurrences(rows):
        groups.setdefault(_signature(r), []).append(r)
    touched: list[UUID] = []
    now = datetime.now(UTC)
    for sig, occ in groups.items():
        key = PREFIX + hashlib.sha256(json.dumps([str(project_id), *sig]).encode()).hexdigest()[:24]
        # Serialize concurrent batches for the same failure so they agree on which ticket is current.
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
        current = await (await conn.execute(
            "SELECT * FROM tickets WHERE organization_id=%s AND source=%s AND external_id LIKE %s AND status='open'"
            " ORDER BY created_at DESC LIMIT 1 FOR UPDATE", (organization_id, SOURCE, key + "%"))).fetchone()
        current = dict(current) if current else None
        for r in sorted(occ, key=lambda x: x.event_time or now):
            at = r.event_time or now
            first = datetime.fromisoformat(current["evidence_summary"]["first_seen"]) if current and (current["evidence_summary"] or {}).get("first_seen") else None
            # A late-delivered earlier error still belongs to the current ticket; only one past the minute opens a new one.
            if current is not None and first is not None and at < first + WINDOW:
                await _fold(conn, current, at, r.request_id)
            else:
                current = await _open(conn, organization_id=organization_id, project_id=project_id, key=key, sig=sig, at=at, r=r)
            if current["id"] not in touched:
                touched.append(current["id"])
    if touched:
        await request_graph_sync(conn, organization_id=organization_id, project_id=project_id)
    return touched
