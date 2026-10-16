"""Connector contract (A10). Adapters verify by *safe reads that prove the requested scope* and report what the
credential can actually do. Format checks alone never establish a connection (R-016)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

StageStatus = Literal["ok", "warn", "fail", "skip"]


@dataclass
class Stage:
    key: str
    title: str
    status: StageStatus
    message: str
    api: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"key": self.key, "title": self.title, "status": self.status, "message": self.message, "api": self.api}


@dataclass
class VerifyResult:
    stages: list[Stage]
    effective_scopes: dict[str, Any] = field(default_factory=dict)
    config_updates: dict[str, Any] = field(default_factory=dict)
    expires_at: Any = None  # None = Unknown (never invented)

    @property
    def outcome(self) -> Literal["connected", "connected_with_warnings", "failed"]:
        if any(s.status == "fail" for s in self.stages):
            return "failed"
        return "connected_with_warnings" if any(s.status == "warn" for s in self.stages) else "connected"

    @property
    def first_failure(self) -> Stage | None:
        return next((s for s in self.stages if s.status == "fail"), None)


class ProviderError(Exception):
    """Maps provider failures to retry semantics. `retryable` True only for transient conditions."""

    def __init__(self, code: str, message: str, *, retryable: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.code, self.message, self.retryable, self.retry_after = code, message, retryable, retry_after


def raise_for_status(resp, what: str) -> None:
    s = resp.status_code
    if s < 400:
        return
    ra = resp.headers.get("retry-after")
    retry_after = float(ra) if ra and ra.replace(".", "", 1).isdigit() else None
    if s in (401,):
        raise ProviderError("bad_credentials", f"{what}: credential rejected (401)")
    if s == 403 and resp.headers.get("x-ratelimit-remaining") == "0":
        raise ProviderError("rate_limited", f"{what}: rate limited", retryable=True, retry_after=retry_after)
    if s == 403:
        raise ProviderError("permission_missing", f"{what}: permission missing (403)")
    if s == 404:
        raise ProviderError("not_found", f"{what}: not found or not visible to this credential (404)")
    if s == 429:
        raise ProviderError("rate_limited", f"{what}: rate limited", retryable=True, retry_after=retry_after)
    if s >= 500:
        raise ProviderError("provider_unavailable", f"{what}: provider error ({s})", retryable=True, retry_after=retry_after)
    raise ProviderError("provider_error", f"{what}: unexpected response ({s})")
