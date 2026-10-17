"""Map PulseLab's incident export (schema_version 1.0) to TRACE ticket fields.

PulseLab publishes {schema_version, ticket, target, requests, backend_events, evidence_gaps}. Field names inside
`ticket` are read tolerantly (aliases) because the exporter is built separately; anything unrecognised stays in the
redacted payload rather than being dropped or guessed. Nothing here invents severity, environment or time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.errors import Unprocessable

SEVERITIES = {"SEV-1", "SEV-2", "SEV-3", "SEV-4"}


@dataclass
class TicketFields:
    external_id: str | None
    title: str
    description: str = ""
    severity: str | None = None
    environment_key: str | None = None
    service_hint: str | None = None
    source_created_at: datetime | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    evidence_summary: dict[str, Any] = field(default_factory=dict)


def _first(d: dict, *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def parse_time(v: Any) -> datetime | None:
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)):  # epoch seconds or milliseconds
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=UTC)
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (ValueError, OSError, OverflowError):
        return None  # unparseable time stays unknown; never replaced with "now"


def _count(v: Any) -> int | None:
    return len(v) if isinstance(v, list) else None


def parse_export(doc: Any) -> TicketFields:
    if not isinstance(doc, dict) or not isinstance(doc.get("ticket"), dict):
        raise Unprocessable("Not a PulseLab incident export: expected an object with a 'ticket' object", code="invalid_export")
    t = doc["ticket"]
    title = _first(t, "title", "summary")
    if not title:
        raise Unprocessable("Export ticket has no title", code="invalid_export")
    ext = _first(t, "ticket_id", "id", "run_id")
    sev = _first(t, "severity", "reported_severity")
    target = doc.get("target") if isinstance(doc.get("target"), dict) else {}
    start = parse_time(_first(t, "started_at", "window_start"))
    end = parse_time(_first(t, "ended_at", "window_end"))
    summary = {
        "schema_version": doc.get("schema_version"),
        "run_id": _first(t, "run_id"),
        "target_url": _first(t, "target_url") or _first(target, "url", "target_url"),
        "observed_counts": t.get("observed_counts"),
        "relevant_request_ids": (t.get("relevant_request_ids") or [])[:100] if isinstance(t.get("relevant_request_ids"), list) else None,
        "evidence_availability": t.get("evidence_availability"),
        "requests": _count(doc.get("requests")),
        "backend_events": _count(doc.get("backend_events")),
        "evidence_gaps": _count(doc.get("evidence_gaps")),
        "target": {k: target[k] for k in list(target)[:10]} if target else None,
    }
    return TicketFields(
        external_id=str(ext) if ext is not None else None,
        title=str(title).strip()[:300],
        description=str(_first(t, "description", "details") or ""),
        severity=sev if sev in SEVERITIES else None,
        environment_key=(str(_first(t, "environment", "environment_label", "env") or _first(target, "environment") or "").strip().lower() or None),
        service_hint=(str(_first(t, "affected_operation", "operation", "service") or "").strip() or None),
        source_created_at=parse_time(_first(t, "created_at")) or start,
        window_start=start,
        window_end=end,
        evidence_summary=summary,
    )
