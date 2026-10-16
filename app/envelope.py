"""Success envelope: {data, meta:{request_id, as_of, next_cursor?, coverage?}} (A5)."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Generic, TypeVar

from fastapi import Request
from pydantic import BaseModel

T = TypeVar("T")


class Meta(BaseModel):
    request_id: str
    as_of: datetime
    next_cursor: str | None = None
    coverage: dict[str, Any] | None = None


class Envelope(BaseModel, Generic[T]):
    data: T
    meta: Meta


def wrap(request: Request, data: Any, *, next_cursor: str | None = None, coverage: dict | None = None) -> dict:
    return {
        "data": data,
        "meta": {
            "request_id": getattr(request.state, "request_id", "-"),
            "as_of": datetime.now(UTC),
            "next_cursor": next_cursor,
            "coverage": coverage,
        },
    }
