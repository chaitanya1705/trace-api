"""Safe error envelope (A5): {error:{code,message,retryable,details},request_id}. No stack traces or secrets."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("trace.errors")


class TraceError(Exception):
    status = 500
    code = "internal_error"
    retryable = False

    def __init__(self, message: str, *, details: dict[str, Any] | None = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if code:
            self.code = code


class Unauthenticated(TraceError):
    status, code = 401, "unauthenticated"


class Forbidden(TraceError):
    status, code = 403, "forbidden"


class NotFound(TraceError):
    """Also used for resources the caller may not know exist (404 absent-or-inaccessible)."""

    status, code = 404, "not_found"


class Conflict(TraceError):
    status, code = 409, "conflict"


class Unprocessable(TraceError):
    status, code = 422, "validation_error"


class RateLimited(TraceError):
    status, code, retryable = 429, "rate_limited", True


class DependencyUnavailable(TraceError):
    status, code, retryable = 503, "dependency_unavailable", True


def request_id_of(request: Request) -> str:
    return getattr(request.state, "request_id", "-")


def _body(request: Request, code: str, message: str, retryable: bool, details: dict[str, Any] | None):
    return {
        "error": {"code": code, "message": message, "retryable": retryable, "details": details or {}},
        "request_id": request_id_of(request),
    }


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(TraceError)
    async def _trace(request: Request, exc: TraceError):
        return JSONResponse(
            _body(request, exc.code, exc.message, exc.retryable, exc.details), status_code=exc.status
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        # Report where and what rule failed, never the submitted value (could be a secret).
        issues = [{"loc": [str(p) for p in e["loc"]], "type": e["type"], "msg": e["msg"]} for e in exc.errors()]
        return JSONResponse(
            _body(request, "validation_error", "Request failed validation", False, {"issues": issues}),
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        code = {401: "unauthenticated", 403: "forbidden", 404: "not_found", 405: "method_not_allowed"}.get(
            exc.status_code, "http_error"
        )
        return JSONResponse(_body(request, code, str(exc.detail), False, None), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("unhandled error", extra={"request_id": request_id_of(request)})
        return JSONResponse(
            _body(request, "internal_error", "Unexpected error. Quote the request ID when reporting.", True, None),
            status_code=500,
        )
