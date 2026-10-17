"""Investigation runner: GraphRAG root-cause analysis over the evidence graph.

The run is a fixed sequence of steps (scope -> deployments -> logs -> code -> changes -> blast radius -> hypotheses
-> impact -> report). Retrieval steps call only named, parameterized query templates; each tool execution records a
RetrievalManifest of exactly what it returned. The model then sees only those items, and every citation it makes is
checked against the run's manifest before a Claim may CITE it. The run itself (steps, tool executions, hypotheses,
assessments, findings, claims, gaps) is written to the same graph, so the investigation is part of the knowledge graph.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.graph import identity as I
from app.graph.evidence import revision_manifest, write_ticket
from app.graph.queries import Bounded, run_bounded
from app.graph.repository import GraphRepository, prepare_node, prepare_rel
from app.investigate import model as M

log = logging.getLogger(__name__)

_ROUTE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE) (\S+) (\d{3}) ")
CITABLE = ("evd_", "chk_")


@dataclass
class TicketIn:
    id: str
    title: str
    description: str
    severity: str | None
    environment_key: str | None
    service_hint: str | None
    window_start: datetime | None
    window_end: datetime | None
    opened_at: datetime
    revision_number: int


def _dt(v: Any) -> datetime | None:
    return v.to_native() if hasattr(v, "to_native") else v


def _hm(t: datetime | None) -> str:
    return t.astimezone(UTC).strftime("%H:%M UTC") if t else "unknown time"


def _jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    if isinstance(v, datetime):
        return v.isoformat()
    return v


class Run:
    def __init__(self, driver, database: str, project_id: str, control_run_id: str):
        self.driver, self.db, self.pid = driver, database, project_id
        self.repo = GraphRepository(driver, database)
        self.control_run_id = control_run_id
        self.id = I.entity_id("InvestigationRun", project_id, control_run_id)
        self.cutoff = datetime.now(UTC)
        self.items: set[str] = set()
        self.props: dict[str, Any] = {}
        self.seq = 0
        self.step_id: str | None = None
        self._step: dict[str, Any] = {}
        self._tools = 0

    # ---- graph helpers ---------------------------------------------------------------------------------------
    def q(self, name: str, **params) -> Bounded:
        return run_bounded(self.driver, self.db, name, project_id=self.pid, **params)

    def cypher(self, query: str, **params) -> list[dict]:
        with self.driver.session(database=self.db) as s:
            return [r.data() for r in s.run(query, parameters={"p": self.pid, **params})]

    def rel(self, t: str, fl: str, f: str, tl: str, to: str, *disc: str | int, **props) -> None:
        self.repo.write_relationships([prepare_rel(t, fl, f, tl, to, self.pid, I.edge_key(t, f, to, *disc), props)])

    def write_run(self, **changes) -> None:
        self.props.update(changes)
        self.repo.write_nodes([prepare_node("InvestigationRun", self.id, self.pid, self.props)])

    # ---- steps and tools -------------------------------------------------------------------------------------
    def step(self, kind: str) -> str:
        self.seq += 1
        self.step_id = I.entity_id("Step", self.pid, self.id, self.seq)
        self._step = {"run_id": self.id, "sequence": self.seq, "kind": kind, "status": "running", "started_at": datetime.now(UTC)}
        self.repo.write_nodes([prepare_node("Step", self.step_id, self.pid, self._step)])
        self.rel("HAS_STEP", "InvestigationRun", self.id, "Step", self.step_id, sequence=self.seq)
        return self.step_id

    def end_step(self, status: str = "completed") -> None:
        if self.step_id:
            self._step.update(status=status, finished_at=datetime.now(UTC))
            self.repo.write_nodes([prepare_node("Step", self.step_id, self.pid, self._step)])
        self.step_id = None

    def tool(self, name: str, template: str | None, params: dict, item_ids: list[str], coverage: dict, truncated: bool = False) -> str:
        assert self.step_id
        self._tools += 1
        exe = I.entity_id("ToolExecution", self.pid, self.step_id, name, self._tools)
        self.repo.write_nodes([prepare_node("ToolExecution", exe, self.pid, {
            "run_id": self.id, "step_id": self.step_id, "tool_name": name,
            "input_hash": I.canonical_digest(I.canonical_json(_jsonable(params))), "query_version": template or "none",
            "status": "completed", "coverage_json": I.canonical_json(_jsonable({**coverage, "rows": len(item_ids), "truncated": truncated})),
        })])
        self.rel("EXECUTED", "Step", self.step_id, "ToolExecution", exe, sequence=self._tools)
        ids = sorted(set(item_ids))
        rtm = self.repo.record_retrieval(self.pid, self.id, exe, ids, cutoff=self.cutoff, query_template=template or name, truncated=truncated)
        rels = [prepare_rel("RETRIEVED", "ToolExecution", exe, "EvidenceRecord" if i.startswith("evd_") else "SourceChunk", i, self.pid, I.edge_key("RETRIEVED", exe, i), {"manifest_id": rtm})
                for i in ids if i.startswith(CITABLE)]
        if rels:
            self.repo.write_relationships(rels)
        self.items.update(ids)
        return exe

    def gap(self, kind: str, description: str, scope: dict) -> str:
        gid = I.entity_id("EvidenceGap", self.pid, self.id, kind, description)
        self.repo.write_nodes([prepare_node("EvidenceGap", gid, self.pid, {
            "kind": kind, "description": description, "scope_json": I.canonical_json(_jsonable(scope)), "detected_at": datetime.now(UTC), "status": "open",
        })])
        self.rel("HAS_GAP", "InvestigationRun", self.id, "EvidenceGap", gid, scope="run")
        return gid


# ---- the investigation ---------------------------------------------------------------------------------------------
def investigate(driver, database: str, *, project_id: str, ticket: TicketIn, control_run_id: str, actor_id: str, api_key: str | None, model: str) -> str:
    r = Run(driver, database, project_id, control_run_id)
    try:
        _run(r, ticket, actor_id, api_key, model)
    except Exception as e:
        log.exception("investigation %s failed", control_run_id)
        try:
            r.end_step("failed")
            r.gap("partial_parse", f"The investigation stopped: {type(e).__name__}: {str(e)[:300]}", {"step": r.seq})
            r.write_run(status="failed")
        except Exception:
            log.exception("could not record failure of %s", control_run_id)
    return r.id


def _run(r: Run, t: TicketIn, actor_id: str, api_key: str | None, model: str) -> None:
    now = r.cutoff
    we = min(t.window_end or now, now)
    ws = t.window_start or (t.opened_at - timedelta(hours=2))
    asked_env = (t.environment_key or "").lower()

    # ---- 1. scope ----------------------------------------------------------------------------------------------
    r.write_run(control_run_id=r.control_run_id, environment_id=I.environment_id(r.pid, asked_env or "production"), window_start=ws, window_end=we, evidence_cutoff=now, status="running")
    tkt = write_ticket(r.repo, r.pid, control_ticket_id=t.id, title=t.title, reported_at=t.opened_at, revision_number=t.revision_number)
    r.rel("HAS_RUN", "Ticket", tkt, "InvestigationRun", r.id, request_actor_id=actor_id)
    r.step("scope")
    services = r.cypher("MATCH (s:Entity:Service {project_id: $p}) RETURN s.id AS id, s.service_key AS key, s.name AS name ORDER BY s.service_key")
    envs = {e["id"]: e["key"] for e in r.cypher("MATCH (e:Entity:Environment {project_id: $p}) RETURN e.id AS id, e.environment_key AS key")}
    # Where was evidence actually recorded in the ticket's window? The environment label on a ticket is a report, evidence is the record.
    seen = r.cypher(
        "MATCH (e:Entity:EvidenceRecord {project_id: $p})-[:OBSERVED_ON]->(s:Entity:Service) WHERE e.event_time >= datetime($ws) AND e.event_time < datetime($we) AND e.ingested_at <= datetime($cut) "
        "RETURN s.id AS service_id, e.environment_id AS environment_id, count(*) AS n ORDER BY n DESC", ws=(ws - timedelta(minutes=5)).isoformat(), we=(we + timedelta(minutes=5)).isoformat(), cut=now.isoformat())
    hint = (t.service_hint or "").lower()
    by_id = {x["id"]: x for x in services}
    pick = None
    for want_env in ([I.environment_id(r.pid, asked_env)] if asked_env else []) + [None]:
        for row in seen:
            sv = by_id.get(row["service_id"])
            if sv and (want_env is None or row["environment_id"] == want_env) and (not hint or hint in (sv["key"].lower(), sv["name"].lower()) or want_env is None or True):
                pick = row
                break
        if pick:
            break
    svc = by_id.get(pick["service_id"]) if pick else next((x for x in services if hint and (x["key"] == hint or x["name"].lower() == hint)), None) or (services[0] if len(services) == 1 else None)
    env_id = pick["environment_id"] if pick else I.environment_id(r.pid, asked_env or "production")
    env_key = envs.get(env_id) or asked_env or "production"
    env_ok = env_id in envs
    note = f" The ticket says {asked_env!r}, but its evidence was recorded in {env_key}." if asked_env and asked_env != env_key and pick else ""
    r.tool("resolve_scope", None, {"environment": env_key, "service_hint": t.service_hint, "window_start": ws, "window_end": we},
           [x for x in (env_id if env_ok else None, svc and svc["id"]) if x],
           {"summary": (f"Service {svc['name']} in {env_key}, window {_hm(ws)} to {_hm(we)}.{note}" if svc and env_ok else "No evidence and no matching service were found for this ticket's window."),
            "service": svc and svc["name"], "environment": env_key, "window_start": ws, "window_end": we, "cutoff": now, "ticket_environment": asked_env or None})
    if not (svc and env_ok):
        r.gap("source_disconnected", "No runtime evidence exists in the graph for this ticket's time window, so there is nothing to investigate yet. Connect a log source or sync evidence first.", {"window_start": ws, "window_end": we})
        r.end_step()
        _finish_insufficient(r, "No runtime evidence was found in the graph for this ticket's window.")
        return
    r.end_step()
    svc_id = svc["id"]

    # ---- 2. deployments ----------------------------------------------------------------------------------------
    r.step("deployments")
    b = r.q("deployments_in_window", service_id=svc_id, environment_id=env_id, window_start=(ws - timedelta(days=14)).isoformat(), window_end=we.isoformat(), limit=50)
    deps = [{**d, "event_time": _dt(d["event_time"])} for d in b.rows]
    meta = {x["id"]: x for x in r.cypher("MATCH (d:Entity:Deployment {project_id: $p}) WHERE d.id IN $ids RETURN d.id AS id, d.provider_id AS provider_id", ids=[d["id"] for d in deps])}
    for d in deps:
        d["provider_id"] = meta.get(d["id"], {}).get("provider_id")
    live = deps[-1] if deps else None
    in_window = [d for d in deps if d["event_time"] >= ws - timedelta(hours=6)]
    suspect_dep = in_window[-1] if in_window else None
    r.tool("deployments_in_window", b.template, {"window_days": 14}, [d["id"] for d in deps], {
        "summary": (f"{len(deps)} deployment(s) in the last 14 days. {live['provider_id']} (commit {(live['commit_sha'] or '?')[:7]}) went live at {_hm(live['event_time'])}"
                    + (f", {int((t.opened_at - live['event_time']).total_seconds() // 60)} min before the ticket." if live and live['event_time'] <= t.opened_at else ".")) if live else "No deployments recorded.",
        "deployments": [{"id": d["id"], "provider_id": d["provider_id"], "at": d["event_time"], "commit": d["commit_sha"], "revision_id": d["revision_id"]} for d in deps[-5:]],
    }, b.truncated)
    if not deps:
        r.gap("missing_baseline", "No deployment is recorded for this service, so code cannot be pinned to a revision.", {"service": svc_id})
    r.end_step()

    # ---- 3. logs and client observations ------------------------------------------------------------------------
    r.step("logs")
    # An explicit ticket window (a test run, a reported incident) is read tightly; without one the ticket's own time sets a wider look-back.
    lw_start = ws - timedelta(minutes=5) if t.window_start else ws - timedelta(hours=2)
    win = dict(service_id=svc_id, environment_id=env_id, window_start=lw_start.isoformat(), window_end=we.isoformat(), cutoff=now.isoformat())
    ob = r.q("evidence_by_operation", limit=100, **win)
    ops = [{**x, "first": _dt(x["first"]), "last": _dt(x["last"])} for x in ob.rows]
    total_events = sum(x["events"] for x in ops)
    b = r.q("log_events_in_window", include_info=False, limit=200, **win)
    events = [{**e, "event_time": _dt(e["event_time"])} for e in b.rows]
    pivot = suspect_dep["event_time"] if suspect_dep else None
    sigs: dict[tuple[str, str], dict] = {}
    requests = Counter()
    for e in events:
        m = _ROUTE.match(e["message"] or "")
        side = "after" if pivot and e["event_time"] >= pivot else "before"
        if m:
            requests[side] += 1
        text = _ROUTE.sub(lambda mm: f"{mm[1]} {mm[2]} {mm[3]} ", e["message"] or "")
        key = (e["severity"], re.sub(r"\d{3,}ms", "Nms", re.sub(r"^(GET|POST|PUT|PATCH|DELETE) \S+ ", "", text)))
        g = sigs.setdefault(key, {"severity": e["severity"], "signature": key[1], "count": 0, "before": 0, "after": 0, "ids": [], "routes": Counter(), "first": e["event_time"], "last": e["event_time"]})
        g["count"] += 1
        g[side] += 1
        g["ids"].append(e["id"])
        g["routes"][e["route"] or "-"] += 1
        g["last"] = e["event_time"]
    groups = sorted(sigs.values(), key=lambda g: ({"error": 0, "warning": 1}.get(g["severity"], 2), -g["count"]))
    errors = [g for g in groups if g["severity"] == "error"]
    r.tool("evidence_by_operation", ob.template, {"window_start": lw_start, "window_end": we}, [i for x in ops for i in x["sample_ids"]], {
        "summary": f"{total_events} log events from {_hm(lw_start)}: " + (f"{sum(g['count'] for g in groups)} warning/error event(s) in {len(groups)} signature(s)" if groups else "all informational, no warnings or errors")
                   + (f"; {sum(g['count'] for g in errors)} errors, {sum(g['before'] for g in errors)} before / {sum(g['after'] for g in errors)} after {suspect_dep['provider_id']}" if errors and suspect_dep else "") + ".",
        "operations": [{"severity": x["severity"], "operation": x["operation"], "events": x["events"]} for x in ops[:20]],
        "signatures": [{"severity": g["severity"], "signature": g["signature"], "count": g["count"], "before": g["before"], "after": g["after"], "routes": dict(g["routes"]), "first": g["first"], "last": g["last"]} for g in groups[:8]],
        "requests": dict(requests), "pivot": suspect_dep and suspect_dep["provider_id"],
    }, ob.truncated)
    if groups:
        r.tool("log_events_in_window", b.template, {"include_info": False}, [e["id"] for e in events], {"summary": f"{len(events)} warning/error events read."}, b.truncated)
    ro = r.q("request_observations", limit=200, **win)
    obs = ro.rows
    spans = [o for o in obs if "RequestSpan" in o["labels"]]
    meas = [o for o in obs if "Measurement" in o["labels"]]
    bodies = Counter()
    statuses = Counter()
    for o in spans:
        m = re.search(r"-> HTTP (\d+) \((\w+)\)", o["text"] or "")
        if m:
            statuses[f"HTTP {m[1]} {m[2]}"] += 1
        c = re.search(r'"coupon": "([^"]*)"', o["text"] or "")
        bodies[c[1] if c else "-"] += 1
    r.tool("request_observations", ro.template, {"window_start": lw_start, "window_end": we}, [o["id"] for o in obs], {
        "summary": (f"{len(spans)} observed request(s): " + ", ".join(f"{n} x {k}" for k, n in statuses.most_common(3)) + "; coupons " + ", ".join(f"{k} x{n}" for k, n in bodies.most_common(3)) + ". "
                    + "; ".join((m["text"] or "").split(" over ")[0] for m in meas[:4]) + ".") if obs else "No client-side request observations in the window.",
        "requests": len(spans), "statuses": dict(statuses), "coupons": dict(bodies), "measurements": [m["text"] for m in meas],
    }, ro.truncated)
    if b.truncated:
        r.gap("truncated_query", "More than 200 warning/error events in the window; only the first 200 were analysed.", {"template": b.template})
    if not obs:
        r.gap("source_disconnected", "No request-level observations were recorded for this window.", {"service": svc_id})
    r.end_step()

    # ---- 4. code: located by evidence, then by text search over the indexed source ---------------------------------------
    r.step("code")
    lc = r.q("code_located_in_window", limit=50, **win)
    located = lc.rows
    chunks: dict[str, dict] = {}

    def excerpt(revision_id: str, path: str, start: int, end: int, why: str) -> list[str]:
        ex = r.q("source_excerpt", revision_id=revision_id, path=path, start_line=start, end_line=end, limit=5)
        out = []
        for c in ex.rows:
            chunks.setdefault(c["id"], {**c, "path": path, "revision_id": revision_id, "why": why, "focus": (start, end)})
            out.append(c["id"])
        return out

    for s in located[:8]:
        excerpt(s["revision_id"], s["path"], s["start_line"], s["end_line"], f"{s['name']} ({s['events']} events locate it, {'/'.join(s['certainty'])})")
    r.tool("code_located_in_window", lc.template, {}, [s["id"] for s in located] + list(chunks), {
        "summary": (f"Evidence locates {len(located)} function(s) at the deployed revision: " + ", ".join(f"{s['name']} ({s['path']}:{s['start_line']}, {s['events']} events)" for s in located[:5]) + ".")
        if located else "No evidence in the window could be located in indexed code.",
        "symbols": [{k: s[k] for k in ("id", "name", "kind", "path", "start_line", "end_line", "events", "certainty")} for s in located[:12]],
    }, lc.truncated)
    # Full-text retrieval over the source: terms come from what was observed (coupon codes, words of the report), never from the model.
    stop = {"the", "a", "an", "of", "to", "is", "are", "in", "on", "for", "and", "or", "with", "from", "manual", "report", "run", "observed", "failures", "without", "checkout", "differ", "since"}
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9_]{3,}", t.title) if w.lower() not in stop]
    terms = list(dict.fromkeys([k for k in bodies if k not in ("-", "NONE", "None")] + words))[:6]
    text_hits: list[dict] = []
    search_revs = list(dict.fromkeys([s["revision_id"] for s in located] + [d["revision_id"] for d in in_window if d["revision_id"]]))
    if terms and search_revs:
        from app.graph.queries import escape_lucene

        sb = r.q("source_text_search", query=" OR ".join(escape_lucene(x) for x in terms), revision_ids=search_revs, candidates=200, limit=8)
        text_hits = sb.rows
        for h in text_hits[:5]:
            for c in r.cypher("MATCH (c:Entity:SourceChunk {project_id: $p, id: $id}) RETURN c.id AS id, c.content AS content, c.start_line AS start_line, c.end_line AS end_line, c.content_hash AS content_hash", id=h["id"]):
                chunks.setdefault(c["id"], {**c, "path": h["path"], "revision_id": search_revs[0], "why": f"text match for {', '.join(terms[:3])}", "focus": (c["start_line"], c["end_line"])})
        r.tool("source_text_search", sb.template, {"terms": terms}, [h["id"] for h in text_hits], {
            "summary": f"Searched the indexed source for {', '.join(repr(x) for x in terms)}: " + (", ".join(sorted({f"{h['path']}:{h['start_line']}" for h in text_hits[:5]})) if text_hits else "no matching code") + ".",
            "terms": terms, "hits": [{"path": h["path"], "start_line": h["start_line"], "end_line": h["end_line"]} for h in text_hits[:8]],
        }, sb.truncated)
    r.end_step()

    # ---- 5. changes that produced the deployed revision -------------------------------------------------------------
    r.step("changes")
    changes: list[dict] = []
    revs = list(dict.fromkeys(d["revision_id"] for d in in_window if d["revision_id"]))
    for rev in revs:
        changes += [{**c, "event_time": _dt(c["event_time"]), "revision_id": rev} for c in r.q("changes_into_revision", revision_id=rev, limit=10).rows]
    located_ids = {s["id"] for s in located}
    suspects: list[dict] = []
    diff_chunks: list[str] = []
    for c in changes:
        mods = [m for m in c["modified"] if m.get("id")]
        for m in mods:
            if m["role"] == "after" and m["id"] in located_ids:
                suspects.append({**m, "change_id": c["id"], "commit": c["ref"], "before_sha": c["before_sha"]})
        for m in mods:
            if any(m["name"] == s["name"] for s in suspects):
                diff_chunks += excerpt(m["revision_id"], m["path"], m["start_line"], m["end_line"], f"{m['name']} {'before' if m['role'] == 'before' else 'after'} commit {c['ref'][:7]}")
    for c in changes:  # the whole commit: module-level edits (constants, tables) matter as much as functions
        if any(s["change_id"] == c["id"] for s in suspects):
            for fo in c.get("files", []):
                if fo.get("id"):
                    diff_chunks += excerpt(fo["revision_id"], fo["path"], 1, 2000, f"{fo['path']} {fo['role']} commit {c['ref'][:7]}")
    sus_names = sorted({s["name"] for s in suspects})
    r.tool("changes_into_revision", "changes_into_revision@1", {"revisions": revs}, [c["id"] for c in changes] + [m["id"] for c in changes for m in c["modified"] if m.get("id")] + diff_chunks, {
        "summary": (f"Commit {suspects[0]['commit'][:7]} changed {', '.join(sus_names)}, and that is exactly where the errors' stack frames point." if suspects
                    else f"{len(changes)} change(s) in deployed revisions; none touch the code in the error stack frames." if changes else "No recorded commits for the deployed revisions."),
        "changes": [{"id": c["id"], "commit": c["ref"], "at": c["event_time"], "modified": sorted({m["name"] for m in c["modified"] if m.get("name")})} for c in changes],
        "suspects": [{"id": s["id"], "name": s["name"], "path": s["path"], "commit": s["commit"]} for s in suspects],
    })
    r.end_step()

    # ---- 6. blast radius ------------------------------------------------------------------------------------------
    r.step("blast_radius")
    hit_paths = {h["path"] for h in text_hits}
    target = suspects[0] if suspects else next((x for x in located if x["path"] in hit_paths), located[0] if located else None)
    callers: list[dict] = []
    if target:
        builds = [x["id"] for x in r.cypher("MATCH (b:Entity:GraphBuild {project_id: $p, revision_id: $r}) RETURN b.id AS id", r=target["revision_id"])]
        b = r.q("callers_2hop", seed_id=target["id"], build_ids=builds, limit=50)
        ids = list(dict.fromkeys(n for p in b.rows for n in p["nodes"] if n != target["id"]))
        names = {x["id"]: x for x in r.cypher("MATCH (s:Entity:CodeSymbol {project_id: $p})<-[:DEFINES]-(f:Entity:SourceFile) WHERE s.id IN $ids RETURN s.id AS id, s.qualified_name AS name, f.path AS path", ids=ids)}
        callers = [{**names[i], "observed": i in located_ids} for i in ids if i in names]
        r.tool("callers_2hop", b.template, {"seed": target["name"]}, [target["id"], *ids], {
            "summary": f"{len(callers)} caller(s) of {target['name']} within 2 hops: " + ", ".join(f"{c['name']}{(' (failing)' if errors else ' (in evidence)') if c['observed'] else ''}" for c in callers) + "." if callers else f"No callers of {target['name']}.",
            "target": {"id": target["id"], "name": target["name"]}, "callers": callers,
        }, b.truncated)
    traced = r.cypher("MATCH (e:Entity:RequestSpan {project_id: $p, service_id: $s}) RETURN count(e) AS n", s=svc_id)[0]["n"]
    if not traced:
        r.gap("source_disconnected", "No tracing source is connected, so request-level timing and downstream calls are not observed.", {"service": svc_id})
    r.end_step()

    # ---- 7. hypotheses (model, citations checked) -------------------------------------------------------------------
    r.step("hypotheses")
    final_rtm = r.repo.record_retrieval(r.pid, r.id, f"{r.id}:final", sorted(r.items), cutoff=now, query_template="run_manifest", truncated=False)
    refs: dict[str, str] = {}
    by_id: dict[str, str] = {}

    def ref(prefix: str, node_id: str) -> str:
        if node_id not in by_id:
            by_id[node_id] = f"{prefix}{sum(1 for k in refs if k.startswith(prefix)) + 1}"
            refs[by_id[node_id]] = node_id
        return by_id[node_id]

    ev_by_id = {e["id"]: e for e in events}
    lines = [f"TICKET ({t.severity or 'unrated'}, reported {_hm(t.opened_at)}): {t.title}\n{t.description}",
             f"\nSCOPE: service {svc['name']}, environment {env_key}, window {_hm(ws)} to {_hm(we)}, evidence cutoff {_hm(now)}."]
    lines.append(f"\nOPERATIONS LOGGED IN THE WINDOW ({total_events} log events, grouped by severity and component/operation/event):")
    for x in ops[:16]:
        lines.append(f"- [{x['severity']}] {x['operation']} x{x['events']} (example {ref('E', x['sample_ids'][0])})")
    lines.append(f"\nCLIENT-SIDE OBSERVATIONS (what a test client saw): {len(spans)} request(s); outcomes {dict(statuses)}; coupons {dict(bodies)}")
    for m in meas:
        lines.append(f"- {ref('E', m['id'])}: {m['text']}")
    for o in spans[:2]:
        lines.append(f"- example request {ref('E', o['id'])}: {o['text']}")
    lines.append("\nDEPLOYMENTS (provider metadata):")
    lines += [f"- {d['provider_id']} at {_hm(d['event_time'])}, commit {(d['commit_sha'] or 'unknown')[:7]}" for d in deps[-4:]]
    lines.append(f"\nLOG SIGNATURES (counts before / after {suspect_dep['provider_id'] if suspect_dep else 'n/a'}; requests before {requests['before']}, after {requests['after']}):")
    for g in groups[:8]:
        sample = [g["ids"][0]] + ([g["ids"][-1]] if len(g["ids"]) > 1 else [])
        if g["before"] and g["after"]:
            sample = [next(i for i in g["ids"] if pivot and ev_by_id[i]["event_time"] < pivot), next(i for i in g["ids"] if pivot and ev_by_id[i]["event_time"] >= pivot)]
        cites = ", ".join(f"{ref('E', i)} at {_hm(ev_by_id[i]['event_time'])}" for i in sample)
        rb = f"{100 * g['before'] / requests['before']:.0f}" if requests["before"] else "n/a"
        ra = f"{100 * g['after'] / requests['after']:.0f}" if requests["after"] else "n/a"
        lines.append(f"- [{g['severity']}] \"{g['signature']}\" x{g['count']}: before {g['before']} ({rb} per 100 requests), after {g['after']} ({ra} per 100 requests); "
                     f"routes {dict(g['routes'])}; examples {cites}")
    full = r.cypher("MATCH (e:Entity:LogEvent {project_id: $p}) WHERE e.id IN $ids RETURN e.id AS id, e.redacted_content AS text", ids=[refs[k] for k in refs if k.startswith("E")])
    lines.append("\nEXAMPLE EVENTS:")
    lines += [f"{by_id[x['id']]}: {x['text']}" for x in full]
    lines.append("\nCODE LOCATED BY STACK FRAMES (deployed revision):")
    lines += [f"- {s['name']} {s['path']}:{s['start_line']}-{s['end_line']} in {s['events']} events" for s in located[:6]]
    lines.append("\nCHANGES INTO THE DEPLOYED REVISION:")
    lines += [f"- commit {c['ref'][:7]} at {_hm(c['event_time'])} modified {', '.join(sorted({m['name'] for m in c['modified'] if m.get('name')})) or 'nothing indexed'}" for c in changes] or ["- none recorded"]
    if callers:
        lines.append(f"\nCALLERS OF {target['name']} (static call graph, 2 hops): " + ", ".join(f"{c['name']}{' [also in error stack frames]' if c['observed'] else ''}" for c in callers))
    lines.append("\nSOURCE EXCERPTS:")
    for cid, c in chunks.items():
        lo, hi = c["focus"]
        body = "\n".join(f"{c['start_line'] + i:>4} | {ln}" for i, ln in enumerate(c["content"].splitlines()) if lo - 2 <= c["start_line"] + i <= hi + 1)
        lines.append(f"{ref('S', cid)}: {c['path']} ({c['why']})\n{body}")
    gaps = r.cypher("MATCH (:Entity:InvestigationRun {id: $r})-[:HAS_GAP]->(g:Entity:EvidenceGap) RETURN g.kind AS kind, g.description AS d", r=r.id)
    if gaps:
        lines.append("\nKNOWN GAPS:\n" + "\n".join(f"- {g['kind']}: {g['d']}" for g in gaps))
    prompt = "\n".join(lines)

    used = f"model:{model}"
    try:
        out = M.ask(api_key, model, prompt)
    except M.ModelUnavailable as e:
        used = "deterministic_fallback"
        out = _fallback(suspects, groups, suspect_dep, by_id, refs, chunks)
        r.gap("source_disconnected", f"The language model was unavailable ({str(e)[:120]}); hypotheses come from graph rules only.", {"model": model})

    rejected: list[str] = []

    def resolve(xs: list[str]) -> list[str]:
        ok = [refs[x] for x in xs if x in refs]
        rejected.extend(x for x in xs if x not in refs)
        bad = set(r.repo.check_citations(r.pid, final_rtm, ok))  # the ontology gate: only what retrieval returned
        rejected.extend(sorted(bad))
        return [i for i in dict.fromkeys(ok) if i not in bad and i.startswith(CITABLE)]

    hyps = []
    for n, h in enumerate(out.get("hypotheses", [])[:4], 1):
        hid = I.entity_id("Hypothesis", r.pid, r.id, n)
        aid = I.entity_id("Assessment", r.pid, hid, 1)
        sup, con = resolve(h.get("supported_by", [])), resolve(h.get("contradicted_by", []))
        status = h.get("status") if h.get("status") in ("supported", "plausible", "contradicted", "insufficient") else "insufficient"
        r.repo.write_nodes([prepare_node("Hypothesis", hid, r.pid, {"run_id": r.id, "statement": h["statement"][:500], "created_step": r.seq})])
        r.repo.write_nodes([prepare_node("Assessment", aid, r.pid, {"hypothesis_id": hid, "sequence": 1, "status": status, "summary": h.get("summary", "")[:1000], "assessed_at": datetime.now(UTC)})])
        r.rel("CONSIDERS", "InvestigationRun", r.id, "Hypothesis", hid, created_step=r.seq)
        r.rel("HAS_ASSESSMENT", "Hypothesis", hid, "Assessment", aid, sequence=1)
        for i in sup:
            r.rel("SUPPORTED_BY", "Assessment", aid, "EvidenceRecord" if i.startswith("evd_") else "SourceChunk", i, explanation=h.get("summary", "")[:300], step_id=r.step_id)
        for i in con:
            r.rel("CONTRADICTED_BY", "Assessment", aid, "EvidenceRecord" if i.startswith("evd_") else "SourceChunk", i, explanation=h.get("summary", "")[:300], step_id=r.step_id)
        hyps.append({"id": hid, "status": status})
    r.tool(used, None, {"prompt_chars": len(prompt)}, [], {
        "summary": f"{len(hyps)} hypotheses: " + ", ".join(f"{x['status']}" for x in hyps) + (f". {len(rejected)} citation(s) rejected." if rejected else ". All citations verified against the retrieval manifest."),
        "model": used, "rejected_citations": rejected[:20], "references": len(refs),
    })
    r.end_step()

    # ---- 8. impact -------------------------------------------------------------------------------------------------
    r.step("impact")
    impact_ids: list[str] = []
    err_ids = [i for g in errors for i in g["ids"]]

    def impact(target_label: str, target_id: str, cats: list[str], scope: dict, evidence: list[str]) -> None:
        iid = I.entity_id("ImpactAssessment", r.pid, r.id, target_id)
        r.repo.write_nodes([prepare_node("ImpactAssessment", iid, r.pid, {"target_id": target_id, "categories": cats, "scope_json": I.canonical_json(_jsonable(scope)), "assessed_at": datetime.now(UTC), "run_id": r.id})])
        r.rel("TARGETS", "ImpactAssessment", iid, target_label, target_id)
        for e in evidence[:5]:
            r.rel("SUPPORTED_BY", "ImpactAssessment", iid, "EvidenceRecord", e, explanation="error events located here", step_id=r.step_id)
        impact_ids.append(iid)

    if errors:
        impact("Service", svc_id, ["observed_impact"], {"errors_after": sum(g["after"] for g in errors), "requests_after": requests["after"]}, err_ids)
    if target:
        impact("CodeSymbol", target["id"], ["direct_exposure", "observed_impact"] if errors else ["potential_propagation"], {"name": target["name"]}, err_ids)
    for c in callers:
        impact("CodeSymbol", c["id"], ["observed_impact"] if c["observed"] and errors else ["potential_propagation"], {"name": c["name"], "via": target["name"]}, err_ids if c["observed"] else [])
    rate = f"{sum(g['after'] for g in errors)}/{requests['after']}" if requests["after"] else "n/a"
    r.tool("impact_assessment", None, {"target": target and target["name"]}, impact_ids, {
        "summary": f"{svc['name']}: {rate} requests failed since {suspect_dep['provider_id'] if suspect_dep else 'the window start'}. "
                   + (f"Observed in {', '.join(c['name'] for c in callers if c['observed'])}; potential exposure via {', '.join(c['name'] for c in callers if not c['observed']) or 'none'}." if callers else ""),
        "failure_rate": rate,
    })
    r.end_step()

    # ---- 9. report -------------------------------------------------------------------------------------------------
    r.step("report")
    f = out.get("finding", {})
    fid = I.entity_id("Finding", r.pid, r.id, 1)
    claims, dropped = [], 0
    for c in f.get("claims", [])[:10]:
        cites = resolve(c.get("citations", []))
        kind = c.get("kind") if c.get("kind") in ("observed_summary", "inference", "recommendation") else "inference"
        if kind != "recommendation" and not cites:
            dropped += 1  # a factual claim with no verifiable citation is not written
            continue
        claims.append((c["text"][:600], kind, cites))
    limitations = list(f.get("limitations", []))[:6] + ([f"{dropped} claim(s) were removed because none of their citations could be verified."] if dropped else [])
    status = f.get("status") if f.get("status") in ("root_cause_identified", "insufficient_evidence", "no_failure_observed") else "insufficient_evidence"
    r.repo.write_nodes([prepare_node("Finding", fid, r.pid, {"run_id": r.id, "sequence": 1, "status": status, "summary": (f.get("summary") or "")[:1500], "limitations_json": I.canonical_json(limitations)})])
    r.rel("PRODUCES", "InvestigationRun", r.id, "Finding", fid, sequence=1)
    meta = {x["id"]: x for x in r.cypher(
        "MATCH (n:Entity {project_id: $p}) WHERE n.id IN $ids OPTIONAL MATCH (f:Entity:SourceFile {id: n.file_id}) "
        "RETURN n.id AS id, n.content_hash AS h, coalesce(f.path + ':' + toString(n.start_line) + '-' + toString(n.end_line), toString(n.event_time)) AS loc",
        ids=[i for _, _, cs in claims for i in cs])}
    for n, (text, kind, cites) in enumerate(claims, 1):
        cid = I.entity_id("Claim", r.pid, fid, n)
        r.repo.write_nodes([prepare_node("Claim", cid, r.pid, {"run_id": r.id, "text": text, "kind": kind})])
        r.rel("HAS_CLAIM", "Finding", fid, "Claim", cid, sequence=n)
        for i in cites:
            r.rel("CITES", "Claim", cid, "EvidenceRecord" if i.startswith("evd_") else "SourceChunk", i, locator=meta.get(i, {}).get("loc") or "", content_hash=meta.get(i, {}).get("h") or "", retrieval_manifest_id=final_rtm)
    res = out.get("resolution", {})
    if res.get("actions"):
        pid_ = I.entity_id("ResolutionPlan", r.pid, fid, 1)
        r.repo.write_nodes([prepare_node("ResolutionPlan", pid_, r.pid, {"run_id": r.id, "finding_id": fid, "version": 1, "actions_json": I.canonical_json(res["actions"][:8]),
                                                                       "preconditions_json": "[]", "criteria_json": I.canonical_json(res.get("verify", [])[:6])})])
        r.rel("ADDRESSES", "ResolutionPlan", pid_, "Finding", fid, version=1)
    r.tool("write_finding", None, {}, [], {"summary": f"Finding: {status.replace('_', ' ')}. {len(claims)} cited claim(s)" + (f", {dropped} removed for missing citations." if dropped else "."), "status": status})
    r.end_step()
    man = revision_manifest(r.repo, r.pid, suspect_dep["revision_id"]) if suspect_dep and suspect_dep["revision_id"] else None
    r.write_run(status="completed", **({"manifest_id": man} if man else {}))


def _finish_insufficient(r: Run, why: str) -> None:
    r.step("report")
    fid = I.entity_id("Finding", r.pid, r.id, 1)
    r.repo.write_nodes([prepare_node("Finding", fid, r.pid, {"run_id": r.id, "sequence": 1, "status": "insufficient_evidence", "summary": why, "limitations_json": I.canonical_json([why])})])
    r.rel("PRODUCES", "InvestigationRun", r.id, "Finding", fid, sequence=1)
    r.tool("write_finding", None, {}, [], {"summary": why, "status": "insufficient_evidence"})
    r.end_step()
    r.write_run(status="completed")


def _fallback(suspects, groups, dep, by_id, refs, chunks) -> dict:
    """Graph-rule hypotheses when the model is unavailable: the change-in-stack-frames path, and noise that predates the deployment."""
    e = lambda g: [by_id[i] for i in g["ids"] if i in by_id][:2]  # noqa: E731
    s = [k for k, v in refs.items() if k.startswith("S")]
    hyps, claims = [], []
    errs = [g for g in groups if g["severity"] == "error"]
    if suspects and errs and dep:
        hyps.append({"statement": f"Commit {suspects[0]['commit'][:7]} (deployed as {dep['provider_id']}) broke {suspects[0]['name']}.", "status": "supported",
                     "summary": f"Errors start after {dep['provider_id']} and their stack frames end in {suspects[0]['name']}, which that commit modified.", "supported_by": e(errs[0]) + s[:2], "contradicted_by": []})
        claims.append({"text": f"{errs[0]['after']} errors '{errs[0]['signature']}' after {dep['provider_id']}, {errs[0]['before']} before.", "kind": "observed_summary", "citations": e(errs[0])})
        claims.append({"text": f"The failing frames are in {suspects[0]['name']}, changed by commit {suspects[0]['commit'][:7]}.", "kind": "inference", "citations": e(errs[0]) + s[:2]})
    for g in groups:
        if g["severity"] == "warning" and g["before"] and g["after"]:
            hyps.append({"statement": f"'{g['signature']}' causes the failures.", "status": "contradicted",
                         "summary": f"It occurs before ({g['before']}) and after ({g['after']}) the deployment at a similar rate.", "supported_by": [], "contradicted_by": e(g)})
    ok = bool(suspects and errs)
    return {"hypotheses": hyps, "finding": {"status": "root_cause_identified" if ok else "insufficient_evidence",
                                             "summary": hyps[0]["summary"] if ok else "The graph does not connect the errors to a change.", "claims": claims,
                                             "limitations": ["Produced by graph rules without the language model."]},
            "resolution": {"actions": [f"Revert or fix {suspects[0]['name']} from commit {suspects[0]['commit'][:7]}."] if ok else [], "verify": []}}
