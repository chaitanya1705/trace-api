"""Opaque keyset cursors and bounded page sizes (shared behaviour contract, PRD section 3)."""
from __future__ import annotations

import base64
import json
from typing import Any

from app.errors import Unprocessable

MAX_PAGE = 100
DEFAULT_PAGE = 25


def clamp_limit(limit: int | None) -> int:
    return max(1, min(limit or DEFAULT_PAGE, MAX_PAGE))


def encode_cursor(values: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(values, default=str, separators=(",", ":")).encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> dict[str, Any] | None:
    if not cursor:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except Exception:  # noqa: BLE001
        raise Unprocessable("Invalid cursor", code="invalid_cursor") from None
