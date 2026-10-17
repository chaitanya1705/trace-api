"""Generic structured events pushed by the monitored application itself (signed webhook, R-016 'Other').

Accepted shape (aliases in brackets) - it deliberately matches ShopFlow's own diagnostics events so forwarding needs no mapping:
  event_id [id]            required, the application's own unique id for the event (dedupe key)
  event_time [time,timestamp]  ISO-8601 or epoch seconds/ms; if absent the time stays UNKNOWN (never replaced by receive time)
  message                  required text (redacted, capped)
  level                    INFO|WARNING|ERROR|CRITICAL (any case)
  request_id, run_id, component_id [service], operation, event_name, duration_ms, error_type, source_revision [revision], deployment_id,
  environment              environment key; otherwise the source's configured default environment applies
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from app.domain.ingest.vercel_logs import MAX_MESSAGE, LogRow
from app.domain.tickets.pulselab import parse_time
from app.redact import redact_text

_LEVELS = {"INFO": "info", "DEBUG": "info", "WARNING": "warning", "WARN": "warning", "ERROR": "error", "CRITICAL": "fatal", "FATAL": "fatal"}


def _s(v: Any, n: int = 200) -> str | None:
    return str(v)[:n] if v is not None and v != "" else None


def normalize_app_event(ev: Any, default_env: str | None) -> LogRow | None:
    """None = unusable (not an object, no id, or no message)."""
    if not isinstance(ev, dict):
        return None
    key = _s(ev.get("event_id") or ev.get("id"), 120)
    msg = ev.get("message")
    if not key or not isinstance(msg, str) or not msg:
        return None
    et = parse_time(ev.get("event_time") if ev.get("event_time") is not None else (ev.get("time") if ev.get("time") is not None else ev.get("timestamp")))
    text = redact_text(msg)
    truncated = len(text) > MAX_MESSAGE
    text = text[:MAX_MESSAGE]
    etype = _s(ev.get("error_type"), 120)
    if etype:
        text = f"{text} [{redact_text(etype)}]"
    extra = {k: ev[k] for k in ("operation", "event_name", "run_id", "component_id", "error_type", "duration_ms") if ev.get(k) is not None}
    rev = ev.get("source_revision") or ev.get("revision")
    if rev:
        extra["source_revision"] = str(rev)[:80]
    if ev.get("service") and "component_id" not in extra:
        extra["component_id"] = str(ev["service"])[:80]
    extra["via"] = "app_push"
    if truncated:
        extra["message_truncated"] = True
    env = _s(ev.get("environment"), 40)
    sc = ev.get("status_code")
    return LogRow(
        source_record_key=key, event_time=et, level=_LEVELS.get(str(ev.get("level") or "").upper()), source="app", provider_project_id="",
        deployment_id=_s(ev.get("deployment_id"), 120), request_id=_s(ev.get("request_id"), 120),
        status_code=sc if isinstance(sc, int) and not isinstance(sc, bool) else None,
        service_hint=_s(ev.get("operation") or extra.get("component_id"), 300), message=text,
        content_hash=hashlib.sha256(json.dumps({"k": key, "t": ev.get("event_time"), "m": text}, sort_keys=True, default=str).encode()).hexdigest(),
        env_key=(env.lower() if env else default_env), extra=extra)
