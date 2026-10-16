"""Capability-specific readiness (R-015): never one misleading green badge. Pure function, unit-tested."""
from __future__ import annotations

from collections.abc import Iterable

from app.domain.projects.schemas import ReadinessItem

_CONN_STATE = {
    "connected": "ok",
    "connected_with_warnings": "warning",
    "failed": "failed",
    "expired": "failed",
    "revoked": "failed",
    "verifying": "todo",
    "draft": "todo",
    "disconnected": "todo",
}


def compute_readiness(
    *,
    has_environments: bool,
    connections: Iterable[tuple[str, str]],  # (kind, status)
    log_scope_total: int,
    log_scope_gaps: int,
    repo_count: int,
    graph_status: str | None,  # None | not_indexed|queued|running|ready|ready_with_gaps|failed|cancelled
    skipped: Iterable[str] = (),
) -> list[ReadinessItem]:
    conns = {k: s for k, s in connections}
    skipped = set(skipped)
    out = [ReadinessItem(key="basics", label="Basics", state="ok" if has_environments else "todo")]

    def conn_item(key: str, label: str, kind: str) -> ReadinessItem:
        s = conns.get(kind)
        if s is None:
            return ReadinessItem(key=key, label=label, state="skipped" if key in skipped else "todo",
                                 detail="Not connected")
        return ReadinessItem(key=key, label=label, state=_CONN_STATE.get(s, "todo"), detail=s.replace("_", " "))

    out.append(conn_item("hosting", "Hosting (Vercel)", "hosting"))
    if log_scope_total == 0:
        out.append(ReadinessItem(key="log_scope", label="Log scope",
                                 state="skipped" if "log_scope" in skipped else "todo", detail="No workloads selected"))
    elif log_scope_gaps:
        out.append(ReadinessItem(key="log_scope", label="Log scope", state="warning",
                                 detail=f"{log_scope_gaps} of {log_scope_total} workloads without usable logs"))
    else:
        out.append(ReadinessItem(key="log_scope", label="Log scope", state="ok", detail=f"{log_scope_total} workloads"))
    out.append(conn_item("code", "Code (GitHub)", "code"))
    gstate = {"ready": "ok", "ready_with_gaps": "warning", "failed": "failed"}.get(graph_status or "", "todo")
    out.append(ReadinessItem(key="code_graph", label="Code graph", state=gstate,
                             detail=(graph_status or "not_indexed").replace("_", " ")))
    return out
