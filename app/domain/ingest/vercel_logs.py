"""Normalize Vercel log-drain records into evidence rows (pure functions; unit-tested).

Rules: event time comes only from the provider timestamp (never ingest time); text is redacted then capped; the environment is
mapped from the record's own environment/customEnvironment against the project's environments, never guessed; unknown values
stay NULL.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.errors import Unprocessable
from app.redact import redact_text

MAX_MESSAGE = 8192
MAX_EVENTS_PER_BATCH = 2000
LEVELS = {"info", "warning", "error", "fatal"}


def signature_ok(secret: str, body: bytes, header: str | None) -> bool:
    """Vercel: x-vercel-signature = hex HMAC-SHA1 of the raw body with the drain signature secret."""
    if not header:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
    return hmac.compare_digest(expected, header.strip().lower())


def parse_body(raw: bytes) -> list[dict[str, Any]]:
    """JSON array or NDJSON. Anything else is rejected whole; individual bad records are counted later, not silently dropped."""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return []
    try:
        doc = json.loads(text)
        items = doc if isinstance(doc, list) else [doc]
    except ValueError:
        items = []
        for i, line in enumerate(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except ValueError:
                raise Unprocessable(f"Line {i + 1} is not valid JSON", code="invalid_payload") from None
    if len(items) > MAX_EVENTS_PER_BATCH:
        raise Unprocessable(f"Batch has more than {MAX_EVENTS_PER_BATCH} events", code="batch_too_large")
    return items


@dataclass
class LogRow:
    source_record_key: str
    event_time: datetime | None
    level: str | None
    source: str | None
    provider_project_id: str
    deployment_id: str | None
    request_id: str | None
    status_code: int | None
    service_hint: str | None
    message: str | None
    content_hash: str
    env_key: str | None
    extra: dict[str, Any]


def _ts(v: Any) -> datetime | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def normalize(rec: Any) -> LogRow | None:
    """None = unusable record (no id or project id) -> counted as rejected by the caller."""
    if not isinstance(rec, dict) or not rec.get("id") or not rec.get("projectId"):
        return None
    msg = rec.get("message")
    truncated = False
    if isinstance(msg, str):
        msg = redact_text(msg)
        if len(msg) > MAX_MESSAGE:
            msg, truncated = msg[:MAX_MESSAGE], True
    else:
        msg = None
    proxy = rec.get("proxy") if isinstance(rec.get("proxy"), dict) else {}
    sc = rec.get("statusCode")
    level = rec.get("level") if rec.get("level") in LEVELS else None
    extra = {k: rec[k] for k in ("type", "entrypoint", "executionRegion", "host", "branch", "traceId", "spanId", "projectName") if rec.get(k) is not None}
    if proxy:
        extra["proxy"] = {k: proxy[k] for k in ("method", "path", "statusCode", "region") if proxy.get(k) is not None}
        if "path" in extra["proxy"]:
            extra["proxy"]["path"] = redact_text(str(extra["proxy"]["path"]))[:500]
    if truncated:
        extra["message_truncated"] = True
    env = rec.get("customEnvironment") or rec.get("environment")
    core = {"id": rec["id"], "ts": rec.get("timestamp"), "msg": msg, "lvl": level, "src": rec.get("source"), "dep": rec.get("deploymentId")}
    return LogRow(
        source_record_key=str(rec["id"]),
        event_time=_ts(rec.get("timestamp")),
        level=level,
        source=rec.get("source") if isinstance(rec.get("source"), str) else None,
        provider_project_id=str(rec["projectId"]),
        deployment_id=rec.get("deploymentId") if isinstance(rec.get("deploymentId"), str) else None,
        request_id=rec.get("requestId") if isinstance(rec.get("requestId"), str) else None,
        status_code=sc if isinstance(sc, int) and not isinstance(sc, bool) else None,
        service_hint=(str(rec.get("path") or rec.get("entrypoint") or "")[:300] or None),
        message=msg,
        content_hash=hashlib.sha256(json.dumps(core, sort_keys=True, default=str).encode()).hexdigest(),
        env_key=str(env).strip().lower() if env else None,
        extra=extra,
    )


def structured(msg: str | None) -> dict[str, Any]:
    """Apps that log JSON (like ShopFlow's diagnostics) keep their fields; plain text returns {}. Values are already redacted text."""
    if not msg or not msg.lstrip().startswith("{"):
        return {}
    try:
        d = json.loads(msg)
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def normalize_runtime(rec: Any, *, deployment_id: str, provider_project_id: str, env_key: str | None) -> LogRow | None:
    """Row from the runtime-logs endpoint ({level,message,rowId,source,timestampInMs,requestMethod,requestPath,responseStatusCode,domain})."""
    if not isinstance(rec, dict) or not isinstance(rec.get("message"), str):
        return None
    raw = rec["message"]
    truncated = bool(rec.get("messageTruncated"))
    msg = redact_text(raw)
    if len(msg) > MAX_MESSAGE:
        msg, truncated = msg[:MAX_MESSAGE], True
    st = structured(msg)
    key = str(st.get("event_id") or hashlib.sha256(f"{deployment_id}|{rec.get('rowId')}|{msg}".encode()).hexdigest()[:40])
    sc = rec.get("responseStatusCode")
    level = {"warning": "warning", "error": "error", "fatal": "fatal", "info": "info"}.get(str(rec.get("level")))
    extra = {k: st[k] for k in ("operation", "event_name", "run_id", "component_id", "error_type", "duration_ms", "source_revision") if st.get(k) is not None}
    for k in ("requestMethod", "domain"):
        if rec.get(k):
            extra[k] = rec[k]
    if truncated:
        extra["message_truncated"] = True
    et = _ts(rec.get("timestampInMs"))
    shown = st.get("message") if isinstance(st.get("message"), str) else None
    return LogRow(
        source_record_key=key, event_time=et, level=level or ({"ERROR": "error", "WARNING": "warning", "INFO": "info"}.get(str(st.get("level")).upper())),
        source=rec.get("source") if isinstance(rec.get("source"), str) else None, provider_project_id=provider_project_id, deployment_id=deployment_id,
        request_id=st.get("request_id") if isinstance(st.get("request_id"), str) else None,
        status_code=sc if isinstance(sc, int) and not isinstance(sc, bool) and sc >= 0 else None,
        service_hint=(str(rec.get("requestPath") or st.get("operation") or "")[:300] or None), message=shown or msg,
        content_hash=hashlib.sha256(json.dumps({"k": key, "ts": rec.get("timestampInMs"), "m": msg}, sort_keys=True).encode()).hexdigest(), env_key=env_key, extra=extra,
    )
