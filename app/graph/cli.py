"""Push a local repository into the evidence graph and inspect it.

    python -m app.graph.cli index D:/Trace/trace-web --project trace-web-local --name trace-web
    python -m app.graph.cli stats  --project trace-web-local
    python -m app.graph.cli find   --project trace-web-local --name authorize
    python -m app.graph.cli callers --project trace-web-local --name authorize --hops 2
    python -m app.graph.cli clear  --project trace-web-local

`--project` is any string you choose for local testing; ids embed it, so projects never mix. Nothing in the repository
is executed; only text files are read.
"""
from __future__ import annotations

import argparse
import json
import sys

from app.config import Settings
from app.graph.driver import create_driver
from app.graph.indexer import index_repository
from app.graph.migrate import apply_migrations
from app.graph.queries import run_template
from app.graph.repository import GraphRepository


def _connect():
    s = Settings()
    d = create_driver(s)
    if d is None:
        print("NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD are not all set")
        sys.exit(2)
    d.verify_connectivity()
    apply_migrations(d, s.neo4j_database)
    return d, s.neo4j_database


def _symbol(d, db, project: str, name: str):
    with d.session(database=db) as ses:
        return list(ses.run(
            "MATCH (s:Entity:CodeSymbol {project_id: $p}) WHERE s.qualified_name = $n OR s.qualified_name ENDS WITH $dn "
            "MATCH (f:Entity:SourceFile {id: s.file_id}) RETURN s.id AS id, s.qualified_name AS name, s.kind AS kind, f.path AS path, s.start_line AS line LIMIT 20",
            p=project, n=name, dn="." + name))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="app.graph.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("index")
    a.add_argument("path")
    a.add_argument("--project", required=True)
    a.add_argument("--name", required=True, help="repository name, e.g. trace-web")
    a.add_argument("--commit", default=None)
    for c in ("stats", "clear"):
        sub.add_parser(c).add_argument("--project", required=True)
    f = sub.add_parser("find")
    f.add_argument("--project", required=True)
    f.add_argument("--name", required=True)
    c = sub.add_parser("callers")
    c.add_argument("--project", required=True)
    c.add_argument("--name", required=True)
    c.add_argument("--hops", type=int, default=2)
    args = ap.parse_args(argv)

    d, db = _connect()
    repo = GraphRepository(d, db)
    try:
        if args.cmd == "index":
            rep = index_repository(repo, project_id=args.project, root=args.path, repository_name=args.name, commit_sha=args.commit)
            print(json.dumps(rep.as_dict(), indent=2))
        elif args.cmd == "stats":
            print(json.dumps(repo.counts(args.project), indent=2))
        elif args.cmd == "clear":
            print("deleted nodes:", repo.delete_project(args.project))
        elif args.cmd == "find":
            for r in _symbol(d, db, args.project, args.name):
                print(f"{r['kind']:9} {r['name']:40} {r['path']}:{r['line']}  {r['id']}")
        elif args.cmd == "callers":
            hits = _symbol(d, db, args.project, args.name)
            if not hits:
                print("no such symbol")
                return 1
            builds = [r["id"] for r in d.session(database=db).run("MATCH (b:GraphBuild {project_id: $p, status: 'published'}) RETURN b.id AS id", p=args.project)]
            for h in hits:
                paths = run_template(d, db, f"callers_{args.hops}hop", project_id=args.project, seed_id=h["id"], build_ids=builds, limit=50)
                print(f"== {h['name']} ({h['path']}:{h['line']}) : {len(paths)} caller path(s)")
                ids = sorted({n for p in paths for n in p["nodes"]})
                with d.session(database=db) as ses:
                    names = {r["id"]: f'{r["q"]} ({r["path"]}:{r["l"]})' for r in ses.run(
                        "MATCH (s:Entity:CodeSymbol {project_id: $p}) WHERE s.id IN $ids MATCH (f:Entity:SourceFile {id: s.file_id}) "
                        "RETURN s.id AS id, s.qualified_name AS q, f.path AS path, s.start_line AS l", p=args.project, ids=ids)}
                for p in paths:
                    print("   ", " -> ".join(names.get(n, n) for n in p["nodes"]), "|", ", ".join(sorted({r["derivation"] for r in p["relationships"]})))
    finally:
        d.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
