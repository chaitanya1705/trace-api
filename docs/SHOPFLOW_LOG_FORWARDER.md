# Sending ShopFlow's logs to TRACE (no Vercel plan needed)

Audience: whoever owns the `shopflow-backend` repository. This adds a small, optional forwarder. It changes **no business logic** and does not touch the demo defects.

TRACE gives each project a signed endpoint (Projects > the project > Logs > "Send logs from your app"). ShopFlow already records every
diagnostics event per request (`app/observability/logger.py`, kept in `ctx.events`) and has a request middleware (`app/main.py`), so forwarding is one file and three lines.

## 1. Environment variables (Vercel > shopflow-backend > Settings > Environment Variables)

| Name | Value |
|---|---|
| `TRACE_INGEST_URL` | the endpoint URL shown in TRACE |
| `TRACE_INGEST_SECRET` | the signing secret shown (once) in TRACE |

Redeploy after adding them. If either is missing the forwarder does nothing.

## 2. New file `app/observability/forward.py`

Copy it from TRACE (the same snippet is shown in the UI), or from here:

```python
"""Forwards each request's diagnostics events to TRACE. Off unless both variables are set; never raises into the request path."""
import asyncio
import hashlib
import hmac
import json
import os
import urllib.request


def _post(url: str, secret: str, events: list[dict]) -> None:
    body = json.dumps({"events": events}, default=str).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"content-type": "application/json", "x-trace-signature": signature, "user-agent": "app-trace-forwarder/1"},
    )
    urllib.request.urlopen(request, timeout=2).read()


async def forward(events) -> None:
    url, secret = os.environ.get("TRACE_INGEST_URL"), os.environ.get("TRACE_INGEST_SECRET")
    if not (url and secret and events):
        return
    try:
        await asyncio.to_thread(_post, url, secret, [e.model_dump(mode="json") for e in events])
    except Exception:
        pass  # logging to TRACE must never break or slow a request beyond its short timeout
```

## 3. `app/main.py`: in the `request_context` middleware

```python
from app.observability.forward import forward   # new import

    @application.middleware("http")
    async def request_context(request: Request, call_next) -> Response:
        ctx = ...
        token = context.begin(ctx)
        try:
            return await call_next(request)
        except Exception as exc:
            return errors.capture_unexpected(request, exc)
        finally:
            await forward(ctx.events)      # new: send this request's events before the context ends
            context.end(token)
```

## Notes
- Cost: at most one extra HTTPS call per request, bounded by a 2 second timeout; failures are swallowed.
- Idempotent: each event has its own `event_id`; resending the same events stores nothing twice.
- The events keep their own `event_time`, `request_id`, `run_id`, `source_revision` and `deployment_id`.
- Rotating the secret in TRACE invalidates the old one immediately; update `TRACE_INGEST_SECRET` and redeploy.
- Tests in the ShopFlow repo that assert the middleware does not do network calls may need `TRACE_INGEST_URL` unset (the default).
