"""Index a repository checkout into the evidence graph (ontology O3, O7).

Flow: walk files (safe paths only, nothing is executed) -> extract symbols/edges -> resolve names across files ->
stage everything under one GraphBuild -> validate counts -> publish the build -> create a RevisionManifest.
A failed run marks the build `failed` and leaves any previously published build untouched.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath

from app.graph import identity as I
from app.graph.extract import KNOWN_UNPARSED, LANGUAGES, OUTPUT_SCHEMA_VERSION, BuiltinExtractor, Extractor, FileResult
from app.graph.ontology import ONTOLOGY_VERSION
from app.graph.repository import GraphRepository, NodeWrite, RelWrite, prepare_node, prepare_rel

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build", ".pytest_cache", ".ruff_cache", "coverage", ".turbo", "out", "egg-info"}
MAX_FILE_BYTES = 512 * 1024
MAX_FILES = 5000
CHUNK_LINES = 60
MAX_AMBIGUOUS = 5
TEXT_EXT = set(LANGUAGES) | set(KNOWN_UNPARSED) | {".md", ".json", ".yaml", ".yml", ".toml", ".sql", ".css", ".html", ".cypher", ".sh", ".txt"}


@dataclass
class IndexReport:
    project_id: str
    repository_id: str
    revision_id: str
    build_id: str
    manifest_id: str
    commit_sha: str
    files: int = 0
    chunks: int = 0
    symbols: int = 0
    edges: dict[str, int] = field(default_factory=dict)
    gaps: dict[str, int] = field(default_factory=dict)
    skipped: int = 0
    status: str = "published"
    extractor: str = ""

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def read_commit_sha(path: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=15)
        sha = out.stdout.strip()
        return sha if out.returncode == 0 and len(sha) >= 7 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _is_dirty(path: str) -> bool:
    try:
        out = subprocess.run(["git", "-C", path, "status", "--porcelain"], capture_output=True, text=True, timeout=30)
        return out.returncode == 0 and bool(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def walk_repo(root: str) -> tuple[list[tuple[str, str]], int]:
    """(relative posix path, text) for readable text files, plus the number skipped. Never follows symlinks out of the tree."""
    out: list[tuple[str, str]] = []
    skipped = 0
    root_real = os.path.realpath(root)
    for dirpath, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info"))
        for f in sorted(files):
            full = os.path.join(dirpath, f)
            ext = os.path.splitext(f)[1].lower()
            if ext not in TEXT_EXT or os.path.islink(full):
                skipped += 1
                continue
            if not os.path.realpath(full).startswith(root_real):
                skipped += 1
                continue
            try:
                if os.path.getsize(full) > MAX_FILE_BYTES:
                    skipped += 1
                    continue
                with open(full, encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError):
                skipped += 1
                continue
            rel = os.path.relpath(full, root).replace("\\", "/")
            out.append((I.normalize_path(rel), text))
            if len(out) >= MAX_FILES:
                return out, skipped + 1
    return out, skipped


def _community_of(path: str, depth: int) -> str:
    dirs = path.split("/")[:-1]
    return "/".join(dirs[:depth]) if dirs else "(root)"


def _community_depth(paths: list[str]) -> int:
    """Group by top-level folder; when one folder holds most of the code (a lone src/), group one level deeper."""
    if not paths:
        return 1
    tops = [p.split("/")[0] if "/" in p else "(root)" for p in paths]
    biggest = max(set(tops), key=tops.count)
    return 2 if tops.count(biggest) / len(tops) > 0.8 and biggest != "(root)" else 1


def _chunks(text: str) -> list[tuple[int, int, str]]:
    lines = text.splitlines(keepends=True)
    return [(i + 1, min(i + CHUNK_LINES, len(lines)), "".join(lines[i : i + CHUNK_LINES])) for i in range(0, len(lines), CHUNK_LINES)] or []


def _resolve_import(src_path: str, target: str, paths: set[str]) -> str | None:
    """Map an import specifier to a file in the repo, or None for external packages."""
    if ":" in target:  # python "from <mod>:<name>"
        target = target.split(":", 1)[0]
    if target.startswith((".", "/")) and "/" in target or target.startswith("./") or target.startswith("../"):
        base = PurePosixPath(src_path).parent
        joined = os.path.normpath(str(base / target)).replace("\\", "/")
        for c in (joined, *(joined + e for e in (".ts", ".tsx", ".js", ".jsx", ".py")), *(joined + "/index" + e for e in (".ts", ".tsx", ".js"))):
            if c in paths:
                return c
        return None
    if target.startswith("@/"):
        joined = "src/" + target[2:]
        for c in (joined, *(joined + e for e in (".ts", ".tsx", ".js", ".jsx")), *(joined + "/index" + e for e in (".ts", ".tsx"))):
            if c in paths:
                return c
        return None
    if target.startswith(".") and "/" not in target:  # python relative: ".mod" / "..pkg.mod"
        dots = len(target) - len(target.lstrip("."))
        base = PurePosixPath(src_path).parent
        for _ in range(dots - 1):
            base = base.parent
        mod = target.lstrip(".").replace(".", "/")
        joined = str(base / mod) if mod else str(base)
        for c in (joined + ".py", joined + "/__init__.py"):
            if c in paths:
                return c
        return None
    mod = target.replace(".", "/")  # python absolute module
    for c in (mod + ".py", mod + "/__init__.py"):
        if c in paths:
            return c
    return None


def index_repository(
    repo: GraphRepository,
    *,
    project_id: str,
    root: str,
    repository_name: str,
    organization_id: str = "local",
    project_name: str | None = None,
    provider: str = "local",
    provider_repository_id: str | None = None,
    canonical_url: str | None = None,
    commit_sha: str | None = None,
    extractor: Extractor | None = None,
) -> IndexReport:
    extractor = extractor or BuiltinExtractor()
    files, skipped = walk_repo(root)
    if not files:
        raise ValueError("no readable source files found")
    tree_hash = hashlib.sha256("".join(f"{p}\0{hashlib.sha256(t.encode()).hexdigest()}" for p, t in files).encode()).hexdigest()
    sha = commit_sha or read_commit_sha(root)
    if commit_sha is None:
        # A local checkout with uncommitted edits is NOT its HEAD commit: give it its own revision so a commit's content stays immutable.
        sha = (f"{sha}+wip-{tree_hash[:12]}" if sha and _is_dirty(root) else sha) or tree_hash
    now = datetime.now(UTC)
    prj = I.project_node_id(project_id)
    rep = I.repository_id(project_id, provider, provider_repository_id or repository_name)
    rev = I.revision_id(project_id, rep, sha)
    with repo._driver.session(database=repo._db) as s:
        attempt = s.run("MATCH (b:GraphBuild {project_id: $p, revision_id: $r}) RETURN count(b) AS n", p=project_id, r=rev).single()["n"] + 1
    bld = I.graph_build_id(project_id, rev, f"{extractor.name}@{extractor.version}", OUTPUT_SCHEMA_VERSION, attempt)
    ev = f"{extractor.name}@{extractor.version}"

    nodes: list[NodeWrite] = [
        prepare_node("Project", prj, project_id, {"organization_id": organization_id, "name": project_name or project_id}, now=now),
        prepare_node("Repository", rep, project_id, {"provider": provider, "provider_repository_id": provider_repository_id or repository_name, "canonical_url": canonical_url or f"{provider}://{repository_name}"}, now=now),
        prepare_node("Revision", rev, project_id, {"repository_id": rep, "commit_sha": sha.lower()}, now=now),
        prepare_node("GraphBuild", bld, project_id, {"revision_id": rev, "extractor_version": ev, "output_schema_version": OUTPUT_SCHEMA_VERSION, "ontology_version": ONTOLOGY_VERSION, "status": "staging", "counts": "{}"}, now=now),
    ]
    rels: list[RelWrite] = []

    def rel(t: str, fl: str, f: str, tl: str, to: str, **props):
        rels.append(prepare_rel(t, fl, f, tl, to, project_id, I.edge_key(t, f, to, *[str(v) for v in props.values() if isinstance(v, (str, int))]), props))

    rel("HAS_REPOSITORY", "Project", prj, "Repository", rep, scope_source=provider)
    rel("HAS_REVISION", "Repository", rep, "Revision", rev)
    rel("INDEXES", "GraphBuild", bld, "Revision", rev)

    report = IndexReport(project_id, rep, rev, bld, "", sha, skipped=skipped, extractor=ev)
    gaps: dict[str, list[str]] = defaultdict(list)
    results: dict[str, FileResult] = {}
    file_ids: dict[str, str] = {}
    paths = {p for p, _ in files}

    for path, text in files:
        fid = I.source_file_id(project_id, rev, path)
        file_ids[path] = fid
        ext = os.path.splitext(path)[1].lower()
        lang = LANGUAGES.get(ext) or KNOWN_UNPARSED.get(ext) or "text"
        nodes.append(prepare_node("SourceFile", fid, project_id, {"revision_id": rev, "path": path, "content_hash": I.content_hash(text), "language": lang}, now=now))
        rel("HAS_FILE", "Revision", rev, "SourceFile", fid)
        report.files += 1
        for a, b, body in _chunks(text):
            cid = I.source_chunk_id(project_id, fid, a, b, I.content_hash(body))
            nodes.append(prepare_node("SourceChunk", cid, project_id, {"file_id": fid, "start_line": a, "end_line": b, "content": body, "content_hash": I.content_hash(body)}, now=now))
            rel("HAS_CHUNK", "SourceFile", fid, "SourceChunk", cid)
            report.chunks += 1
        if ext in KNOWN_UNPARSED:
            gaps["unsupported_language"].append(path)
            continue
        res = extractor.extract(path, text)
        if res is None:
            continue
        if res.partial:
            gaps["partial_parse"].append(path)
        results[path] = res

    # symbols and structure
    sym_ids: dict[tuple[str, str], str] = {}  # (path, qualified_name) -> id
    by_name: dict[str, list[tuple[str, str, str]]] = defaultdict(list)  # simple name -> (path, qualified, id)
    communities: dict[str, str] = {}
    depth = _community_depth(list(results))
    for path, res in results.items():
        top = _community_of(path, depth)
        if top not in communities:
            communities[top] = I.entity_id("CodeCommunity", project_id, bld, top)
            nodes.append(prepare_node("CodeCommunity", communities[top], project_id, {"build_id": bld, "label": top}, now=now))
        for sy in res.symbols:
            sid = I.code_symbol_id(project_id, file_ids[path], sy.qualified_name, sy.kind, sy.start_line)
            sym_ids[(path, sy.qualified_name)] = sid
            by_name[sy.name].append((path, sy.qualified_name, sid))
            nodes.append(prepare_node("CodeSymbol", sid, project_id, {"file_id": file_ids[path], "qualified_name": sy.qualified_name, "kind": sy.kind, "start_line": sy.start_line, "end_line": sy.end_line}, now=now))
            ctx = {"build_id": bld, "derivation": "extracted", "source_locator": f"{path}:{sy.start_line}", "extractor_version": ev}
            rel("DEFINES", "SourceFile", file_ids[path], "CodeSymbol", sid, build_id=bld, source_locator=f"{path}:{sy.start_line}")
            rel("MEMBER_OF", "CodeSymbol", sid, "CodeCommunity", communities[top], build_id=bld)
            report.symbols += 1
            if sy.parent and (path, sy.parent) in sym_ids:
                rel("CONTAINS", "CodeSymbol", sym_ids[(path, sy.parent)], "CodeSymbol", sid, **ctx)
                report.edges["CONTAINS"] = report.edges.get("CONTAINS", 0) + 1

    def count(t: str):
        report.edges[t] = report.edges.get(t, 0) + 1

    # cross-file resolution
    for path, res in results.items():
        for e in res.edges:
            loc = f"{path}:{e.line}"
            ctx = {"build_id": bld, "source_locator": loc, "extractor_version": ev}
            if e.kind == "imports":
                tgt = _resolve_import(path, e.target_name, paths)
                if tgt and tgt != path:
                    rel("IMPORTS", "SourceFile", file_ids[path], "SourceFile", file_ids[tgt], derivation="resolved", **ctx)
                    count("IMPORTS")
                continue
            src_id = sym_ids.get((path, e.source))
            if src_id is None:
                continue
            simple = e.target_name.split(".")[-1].split(":")[-1]
            cands = [c for c in by_name.get(simple, []) if c[2] != src_id]
            if e.kind == "inherits":
                cands = [c for c in cands]
            if not cands:
                continue  # external or built-in: not part of the repository graph
            same = [c for c in cands if c[0] == path]
            if len(same) == 1:
                chosen, deriv = [same[0]], "resolved"
            elif len(cands) == 1:
                chosen, deriv = cands, "inferred"
            else:
                chosen, deriv = cands[:MAX_AMBIGUOUS], "ambiguous"
            t = "CALLS" if e.kind == "calls" else "INHERITS_FROM"
            for c in chosen:
                rel(t, "CodeSymbol", src_id, "CodeSymbol", c[2], derivation=deriv, **ctx)
                count(t)

    # coverage gaps are recorded, not hidden
    for kind, plist in gaps.items():
        gid = I.entity_id("EvidenceGap", project_id, bld, kind)
        nodes.append(prepare_node("EvidenceGap", gid, project_id, {
            "kind": kind, "description": f"{len(plist)} file(s): {', '.join(sorted(plist)[:5])}{' …' if len(plist) > 5 else ''}",
            "scope_json": json.dumps({"build_id": bld, "count": len(plist)}), "detected_at": now, "status": "open"}, now=now))
        report.gaps[kind] = len(plist)

    try:
        repo.write_nodes(nodes)
        repo.write_relationships(rels)
        counts = {"files": report.files, "chunks": report.chunks, "symbols": report.symbols, "edges": sum(report.edges.values()), "gaps": sum(report.gaps.values())}
        with repo._driver.session(database=repo._db) as s:
            s.run("MATCH (b:GraphBuild {id: $id, project_id: $p}) SET b.counts = $c", id=bld, p=project_id, c=json.dumps(counts)).consume()
            if s.run("MATCH (:Entity:SourceFile {project_id: $p, revision_id: $r}) RETURN count(*) AS n", p=project_id, r=rev).single()["n"] != report.files:
                raise RuntimeError("staged file count does not match the extraction")
            # Latest published build per repository wins: earlier published builds of ANY revision of this repository are superseded.
            old = [r["id"] for r in s.run(
                "MATCH (b:GraphBuild {project_id: $p, status: 'published'})-[:INDEXES]->(:Revision {project_id: $p, repository_id: $rep}) RETURN b.id AS id",
                p=project_id, rep=rep)]
        repo.set_build_status(project_id, bld, "published")
        for b in old:
            repo.set_build_status(project_id, b, "superseded")
    except Exception:
        try:
            repo.set_build_status(project_id, bld, "failed")
        finally:
            raise
    report.manifest_id = repo.create_manifest(project_id, [{"repository_id": rep, "revision_id": rev, "build_id": bld}])
    return report
