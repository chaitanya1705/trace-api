"""Read models for investigations: lists, the full run view (steps, hypotheses, finding, impact, timeline, coverage,
evidence and the evidence subgraph) and single evidence items. Everything is read back from the graph."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

SEP = "\n---\n"  # ConversationAnswer.text holds "question SEP answer"


def _dt(v: Any) -> str | None:
    if v is None:
        return None
    v = v.to_native() if hasattr(v, "to_native") else v
    return v.isoformat() if isinstance(v, datetime) else str(v)


def _rows(s, q: str, **p) -> list[dict]:
    return [r.data() for r in s.run(q, parameters=p)]


RUN_LIST = (
    "MATCH (t:Entity:Ticket)-[:HAS_RUN]->(r:Entity:InvestigationRun) WHERE r.project_id IN $pids AND t.project_id = r.project_id "
    "OPTIONAL MATCH (r)-[:HAS_STEP]->(st:Entity:Step) "
    "WITH t, r, count(st) AS steps, max(st.sequence) AS last_seq "
    "OPTIONAL MATCH (r)-[:HAS_STEP]->(ls:Entity:Step {sequence: last_seq}) "
    "OPTIONAL MATCH (r)-[:PRODUCES]->(f:Entity:Finding) "
    "OPTIONAL MATCH (r)-[:HAS_GAP]->(g:Entity:EvidenceGap) "
    "RETURN r.id AS id, r.project_id AS project_id, r.control_run_id AS control_run_id, r.status AS status, r.created_at AS created_at, "
    "r.window_start AS window_start, r.window_end AS window_end, t.control_ticket_id AS ticket_id, t.title AS title, "
    "steps, ls.kind AS last_step, f.status AS finding_status, f.summary AS finding_summary, count(DISTINCT g) AS gaps "
    "ORDER BY r.created_at DESC LIMIT $limit"
)


def list_runs(driver, db: str, project_ids: list[str], *, ticket_id: str | None = None, limit: int = 200) -> list[dict]:
    with driver.session(database=db) as s:
        rows = _rows(s, RUN_LIST, pids=project_ids, limit=limit)
    out = []
    for r in rows:
        if ticket_id and r["ticket_id"] != ticket_id:
            continue
        out.append({**r, "created_at": _dt(r["created_at"]), "window_start": _dt(r["window_start"]), "window_end": _dt(r["window_end"])})
    return out


def active_run(driver, db: str, project_id: str, ticket_id: str) -> str | None:
    with driver.session(database=db) as s:
        row = s.run(
            "MATCH (t:Entity:Ticket {project_id: $p, control_ticket_id: $t})-[:HAS_RUN]->(r:Entity:InvestigationRun) WHERE r.status IN ['queued', 'running'] "
            "RETURN r.control_run_id AS id LIMIT 1", p=project_id, t=ticket_id).single()
        return row["id"] if row else None


def _evidence_view(n: dict) -> dict:
    labels = n.get("labels") or []
    if "SourceChunk" in labels:
        return {"id": n["id"], "type": "code", "title": f"{n.get('path')}:{n.get('start_line')}-{n.get('end_line')}", "src": f"commit {(n.get('commit') or '')[:7]}",
                "path": n.get("path"), "start_line": n.get("start_line"), "end_line": n.get("end_line"), "commit": n.get("commit"), "content": n.get("content"),
                "content_hash": n.get("content_hash")}
    return {"id": n["id"], "type": "log", "title": n.get("message") or "", "src": f"{n.get('severity')} · {_dt(n.get('event_time'))}", "severity": n.get("severity"),
            "event_time": _dt(n.get("event_time")), "content": n.get("text"), "route": n.get("route"), "request_id": n.get("request_id"),
            "deployment": n.get("deployment"), "content_hash": n.get("content_hash")}


EVIDENCE_Q = (
    "MATCH (n:Entity {project_id: $p}) WHERE n.id IN $ids "
    "OPTIONAL MATCH (f:Entity:SourceFile {project_id: $p, id: n.file_id}) OPTIONAL MATCH (rv:Entity:Revision {project_id: $p, id: f.revision_id}) "
    "OPTIONAL MATCH (n)-[:ASSOCIATED_WITH]->(d:Entity:Deployment) "
    "RETURN n.id AS id, labels(n) AS labels, n.message AS message, n.severity AS severity, n.event_time AS event_time, n.redacted_content AS text, "
    "n.source_locator AS route, n.request_id AS request_id, d.provider_id AS deployment, n.content AS content, n.content_hash AS content_hash, "
    "f.path AS path, n.start_line AS start_line, n.end_line AS end_line, rv.commit_sha AS commit"
)


def evidence(driver, db: str, project_id: str, ids: list[str]) -> list[dict]:
    with driver.session(database=db) as s:
        return [_evidence_view(r) for r in _rows(s, EVIDENCE_Q, p=project_id, ids=ids)]


def run_detail(driver, db: str, project_id: str, control_run_id: str) -> dict | None:
    with driver.session(database=db) as s:
        head = _rows(s, "MATCH (t:Entity:Ticket {project_id: $p})-[:HAS_RUN]->(r:Entity:InvestigationRun {project_id: $p, control_run_id: $c}) "
                        "RETURN r AS r, t AS t", p=project_id, c=control_run_id)
        if not head:
            return None
        run, tk = dict(head[0]["r"]), dict(head[0]["t"])
        rid = run["id"]
        steps = _rows(s, "MATCH (:Entity:InvestigationRun {id: $r})-[:HAS_STEP]->(st:Entity:Step) OPTIONAL MATCH (st)-[e:EXECUTED]->(x:Entity:ToolExecution) "
                         "OPTIONAL MATCH (m:Entity:RetrievalManifest {execution_id: x.id}) "
                         "WITH st, x, e, m ORDER BY e.sequence RETURN st AS st, collect(CASE WHEN x IS NULL THEN NULL ELSE {x: x, items: m.item_ids_json} END) AS tools "
                         "ORDER BY st.sequence", r=rid)
        hyps = _rows(s, "MATCH (:Entity:InvestigationRun {id: $r})-[:CONSIDERS]->(h:Entity:Hypothesis)-[:HAS_ASSESSMENT]->(a:Entity:Assessment) "
                        "OPTIONAL MATCH (a)-[:SUPPORTED_BY]->(se) OPTIONAL MATCH (a)-[:CONTRADICTED_BY]->(ce) "
                        "RETURN h.id AS id, h.statement AS statement, a.status AS status, a.summary AS summary, collect(DISTINCT se.id) AS supported_by, collect(DISTINCT ce.id) AS contradicted_by "
                        "ORDER BY h.id", r=rid)
        fnd = _rows(s, "MATCH (:Entity:InvestigationRun {id: $r})-[:PRODUCES]->(f:Entity:Finding) OPTIONAL MATCH (f)-[hc:HAS_CLAIM]->(c:Entity:Claim) "
                       "OPTIONAL MATCH (c)-[ct:CITES]->(e) WITH f, hc, c, collect({id: e.id, locator: ct.locator}) AS cites ORDER BY hc.sequence "
                       "OPTIONAL MATCH (pl:Entity:ResolutionPlan)-[:ADDRESSES]->(f) "
                       "RETURN f AS f, pl AS pl, collect(CASE WHEN c IS NULL THEN NULL ELSE {text: c.text, kind: c.kind, citations: [x IN cites WHERE x.id IS NOT NULL]} END) AS claims", r=rid)
        gaps = _rows(s, "MATCH (:Entity:InvestigationRun {id: $r})-[:HAS_GAP]->(g:Entity:EvidenceGap) RETURN g.kind AS kind, g.description AS description, g.status AS status ORDER BY g.detected_at", r=rid)
        impact = _rows(s, "MATCH (i:Entity:ImpactAssessment {project_id: $p, run_id: $r})-[:TARGETS]->(t) OPTIONAL MATCH (i)-[:SUPPORTED_BY]->(e) "
                          "RETURN i.target_id AS target_id, coalesce(t.qualified_name, t.name) AS name, [l IN labels(t) WHERE l <> 'Entity'][0] AS label, i.categories AS categories, "
                          "i.scope_json AS scope, collect(e.id)[..3] AS evidence", p=project_id, r=rid)
        answers = _rows(s, "MATCH (a:Entity:ConversationAnswer {project_id: $p, run_id: $r}) OPTIONAL MATCH (a)-[ct:CITES]->(e) "
                           "RETURN a.message_id AS id, a.conversation_id AS question, a.text AS text, a.created_at AS at, collect({id: e.id, locator: ct.locator}) AS citations ORDER BY a.created_at", p=project_id, r=rid)

    tools_out = []
    all_items: set[str] = set()
    step_out = []
    for row in steps:
        st = dict(row["st"])
        tl = []
        for t in row["tools"]:
            if not t:
                continue
            x = dict(t["x"])
            items = json.loads(t["items"]) if t.get("items") else []
            all_items.update(items)
            cov = json.loads(x.get("coverage_json") or "{}")
            tl.append({"name": x["tool_name"], "template": (x.get("query_version") if x.get("query_version") not in (None, "none") else None), "coverage": cov, "items": len(items), "item_ids": items[:200]})
            tools_out.append(tl[-1])
        started, finished = st.get("started_at"), st.get("finished_at")
        dur = (finished.to_native() - started.to_native()).total_seconds() if finished and started else None
        step_out.append({"sequence": st["sequence"], "kind": st["kind"], "status": st["status"], "started_at": _dt(started), "finished_at": _dt(finished), "duration_s": dur, "tools": tl})

    f = fnd[0] if fnd else None
    finding = None
    resolution = None
    if f:
        fn = dict(f["f"])
        finding = {"status": fn["status"], "summary": fn["summary"], "limitations": json.loads(fn.get("limitations_json") or "[]"), "claims": [c for c in f["claims"] if c]}
        if f["pl"]:
            pl = dict(f["pl"])
            resolution = {"actions": json.loads(pl["actions_json"]), "verify": json.loads(pl["criteria_json"])}

    cited = {c["id"] for cl in (finding or {}).get("claims", []) for c in cl["citations"]}
    cited |= {i for h in hyps for i in (*h["supported_by"], *h["contradicted_by"])}
    cited |= {c["id"] for a in answers for c in a["citations"] if c.get("id")}
    ev = evidence(driver, db, project_id, sorted(cited)) if cited else []

    cov_of = {t["name"]: t["coverage"] for t in tools_out}
    deps = (cov_of.get("deployments_in_window") or {}).get("deployments", [])
    logs = cov_of.get("log_events_in_window") or {}
    code = cov_of.get("code_located_by_events") or {}
    changes = cov_of.get("changes_into_revision") or {}
    blast = cov_of.get("callers_2hop") or {}
    gap_kinds = {g["kind"] for g in gaps}
    sigs = logs.get("signatures", [])
    coverage = [
        ["Error pattern and timing", "good" if any(x["severity"] == "error" for x in sigs) else "warn" if sigs else "miss"],
        ["Stack trace to code", "good" if code.get("symbols") else "miss"],
        ["Version tag on lines (deployment → commit)", "good" if any(d.get("commit") for d in deps) else "miss"],
        ["Commit history for the deployed revision", "good" if changes.get("changes") else "miss"],
        ["Request traces (latency, downstream calls)", "miss" if "source_disconnected" in gap_kinds else "good"],
    ]

    timeline: list[dict] = []
    for c in changes.get("changes", []):
        timeline.append({"lane": "Changes", "at": c["at"], "label": f"Commit {c['commit'][:7]}", "detail": ", ".join(c["modified"]), "tone": "v"})
    for d in deps:
        timeline.append({"lane": "Changes", "at": d["at"], "label": f"Deploy {d['provider_id']}", "detail": f"commit {(d.get('commit') or '?')[:7]}", "tone": "v"})
    for x in sigs:
        if x["severity"] != "info":
            timeline.append({"lane": "Failures" if x["severity"] == "error" else "Runtime", "at": x["first"], "until": x["last"], "label": x["signature"][:80],
                             "detail": f"{x['count']} events ({x['before']} before, {x['after']} after)", "tone": "r" if x["severity"] == "error" else "k"})
    timeline.append({"lane": "Investigation", "at": _dt(tk.get("reported_at")), "label": "Ticket reported", "detail": tk.get("title"), "tone": "b"})
    if step_out:
        timeline.append({"lane": "Investigation", "at": step_out[0]["started_at"], "until": step_out[-1]["finished_at"], "label": "TRACE investigation", "detail": f"{len(step_out)} steps", "tone": "g"})
    timeline.sort(key=lambda e: e["at"] or "")

    return {
        "run": {"id": rid, "control_run_id": run["control_run_id"], "status": run["status"], "window_start": _dt(run.get("window_start")), "window_end": _dt(run.get("window_end")),
                "evidence_cutoff": _dt(run.get("evidence_cutoff")), "created_at": _dt(run.get("created_at"))},
        "ticket": {"id": tk["control_ticket_id"], "title": tk["title"], "reported_at": _dt(tk.get("reported_at"))},
        "steps": step_out, "hypotheses": hyps, "finding": finding, "resolution": resolution, "gaps": gaps,
        "impact": [{**i, "scope": json.loads(i["scope"] or "{}")} for i in impact],
        "blast": {"target": blast.get("target"), "callers": blast.get("callers", []), "suspects": changes.get("suspects", []), "located": code.get("symbols", []),
                  "failure_rate": (cov_of.get("impact_assessment") or {}).get("failure_rate")},
        "coverage": coverage, "timeline": timeline, "evidence": ev, "items": len(all_items),
        "answers": [{"id": a["id"], "question": (a["text"] or "").split(SEP, 1)[0], "answer": (a["text"] or "").split(SEP, 1)[-1], "at": _dt(a["at"]),
                     "citations": [c for c in a["citations"] if c.get("id")]} for a in answers],
        "graph": evidence_graph(driver, db, project_id, rid),
    }


def evidence_graph(driver, db: str, project_id: str, run_id: str) -> dict:
    """The subgraph the run reasoned over: ticket -> run, deployment -> revision <- change -> modified code, error events
    LOCATES code, callers of the suspect. Log events are collapsed into one node per (severity, located symbol)."""
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def node(i: str, label: str, kind: str, **extra) -> None:
        nodes.setdefault(i, {"id": i, "label": label, "kind": kind, **extra})

    with driver.session(database=db) as s:
        for r in _rows(s, "MATCH (t:Entity:Ticket)-[:HAS_RUN]->(r:Entity:InvestigationRun {id: $r}) RETURN t.id AS t, t.title AS title", r=run_id):
            node(r["t"], r["title"], "ticket")
            node(run_id, "Investigation", "run")
            edges.append({"source": r["t"], "target": run_id, "type": "HAS_RUN"})
        exe = _rows(s, "MATCH (:Entity:InvestigationRun {id: $r})-[:HAS_STEP]->(:Entity:Step)-[:EXECUTED]->(x:Entity:ToolExecution) "
                       "MATCH (m:Entity:RetrievalManifest {execution_id: x.id}) RETURN x.tool_name AS tool, m.item_ids_json AS items", r=run_id)
        items = {t["tool"]: json.loads(t["items"]) for t in exe}
        dep_ids = items.get("deployments_in_window", [])
        for r in _rows(s, "MATCH (d:Entity:Deployment {project_id: $p}) WHERE d.id IN $ids OPTIONAL MATCH (d)-[:DEPLOYS_REVISION]->(v:Entity:Revision) "
                          "OPTIONAL MATCH (d)-[:DEPLOYS_SERVICE]->(sv:Entity:Service) "
                          "RETURN d.id AS d, d.provider_id AS name, d.event_time AS at, v.id AS v, v.commit_sha AS sha, sv.id AS sv, sv.name AS svn", p=project_id, ids=dep_ids):
            node(r["d"], r["name"], "deployment", at=_dt(r["at"]))
            if r["sv"]:
                node(r["sv"], r["svn"], "service")
                edges.append({"source": r["d"], "target": r["sv"], "type": "DEPLOYS_SERVICE"})
            if r["v"]:
                node(r["v"], (r["sha"] or "")[:7], "revision")
                edges.append({"source": r["d"], "target": r["v"], "type": "DEPLOYS_REVISION"})
        chg_items = items.get("changes_into_revision", [])
        for r in _rows(s, "MATCH (c:Entity:Change {project_id: $p}) WHERE c.id IN $ids OPTIONAL MATCH (c)-[:AFTER_REVISION]->(v:Entity:Revision) "
                          "OPTIONAL MATCH (c)-[m:MODIFIES {before_after_role: 'after'}]->(sym:Entity:CodeSymbol) "
                          "RETURN c.id AS c, c.source_id AS sha, v.id AS v, collect({id: sym.id, name: sym.qualified_name}) AS mods", p=project_id, ids=chg_items):
            node(r["c"], f"commit {r['sha'][:7]}", "change")
            if r["v"]:
                edges.append({"source": r["c"], "target": r["v"], "type": "AFTER_REVISION"})
            for m in r["mods"]:
                if m.get("id"):
                    node(m["id"], m["name"], "symbol", status="changed")
                    edges.append({"source": r["c"], "target": m["id"], "type": "MODIFIES"})
        sym_ids = [i for i in items.get("code_located_in_window", []) if i.startswith("sym_")]
        for r in _rows(s, "MATCH (run:Entity:InvestigationRun {id: $r}) MATCH (e:Entity:LogEvent {project_id: $p})-[l:LOCATES]->(sym:Entity:CodeSymbol {project_id: $p}) "
                          "WHERE sym.id IN $syms AND e.event_time >= run.window_start - duration('PT5M') AND e.event_time < run.window_end + duration('PT5M') "
                          "RETURN e.severity AS sev, sym.id AS sym, sym.qualified_name AS name, count(DISTINCT e) AS n, min(e.message) AS msg", p=project_id, r=run_id, syms=sym_ids):
            gid = f"logs:{r['sev']}:{r['sym']}"
            node(gid, f"{r['n']} {r['sev']} event{'s' if r['n'] > 1 else ''}", "logs", severity=r["sev"], count=r["n"], detail=r["msg"])
            node(r["sym"], r["name"], "symbol")
            edges.append({"source": gid, "target": r["sym"], "type": "LOCATES"})
        caller_ids = items.get("callers_2hop", [])
        if caller_ids:
            for r in _rows(s, "MATCH (a:Entity:CodeSymbol {project_id: $p})-[c:CALLS]->(b:Entity:CodeSymbol {project_id: $p}) WHERE a.id IN $ids AND b.id IN $ids "
                              "RETURN DISTINCT a.id AS a, a.qualified_name AS an, b.id AS b, b.qualified_name AS bn", p=project_id, ids=caller_ids):
                node(r["a"], r["an"], "symbol")
                node(r["b"], r["bn"], "symbol")
                edges.append({"source": r["a"], "target": r["b"], "type": "CALLS"})
    seen = set()
    uniq = []
    for e in edges:
        k = (e["source"], e["target"], e["type"])
        if k not in seen and e["source"] in nodes and e["target"] in nodes:
            seen.add(k)
            uniq.append(e)
    return {"nodes": list(nodes.values()), "edges": uniq}
