"""Best-effort secret redaction for text and JSON before it is stored or shown to a model (spec: redact first).
Pattern based, so it reduces risk rather than guaranteeing removal."""
from __future__ import annotations

import re
from typing import Any

_PATTERNS = [
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{12,}"), r"\1 [REDACTED]"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"), "[REDACTED:github-token]"),
    (re.compile(r"\bvcp_[A-Za-z0-9]{16,}\b"), "[REDACTED:vercel-token]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "[REDACTED:api-key]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED:aws-key]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "[REDACTED:jwt]"),
    (re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|token|authorization)(\s*[=:]\s*)(?!\[REDACTED)[^\s,;&\"']{4,}"), r"\1\2[REDACTED]"),
    (re.compile(r"(?i)(://[^/\s:@]+:)[^@\s/]{3,}@"), r"\1[REDACTED]@"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[REDACTED:private-key]"),
]
_SENSITIVE_KEYS = re.compile(r"(?i)(password|passwd|secret|api[_-]?key|authorization|cookie|private[_-]?key|access[_-]?token|refresh[_-]?token)")


def redact_text(s: str) -> str:
    for rx, rep in _PATTERNS:
        s = rx.sub(rep, s)
    return s


def redact_json(v: Any, _depth: int = 0) -> Any:
    if _depth > 12:
        return "[TRUNCATED]"
    if isinstance(v, str):
        return redact_text(v)
    if isinstance(v, list):
        return [redact_json(x, _depth + 1) for x in v]
    if isinstance(v, dict):
        return {k: ("[REDACTED]" if _SENSITIVE_KEYS.search(str(k)) and isinstance(x, (str, int)) else redact_json(x, _depth + 1))
                for k, x in v.items()}
    return v
