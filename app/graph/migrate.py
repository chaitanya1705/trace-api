"""Versioned graph migrations (ontology O9/O12).

Files in migrations/graph are `NNNN_name.cypher`, one or more `;`-terminated idempotent statements. Applied versions are
recorded as (:GraphMigration {version, name, checksum, applied_at}). A recorded migration whose file changed is an error:
migrations are append-only so retained runs and citations keep their meaning.

Run: python -m app.graph.migrate
"""
from __future__ import annotations

import hashlib
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("trace.graph")

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations" / "graph"
_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.cypher$")


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    checksum: str
    statements: tuple[str, ...]


def split_statements(text: str) -> tuple[str, ...]:
    """Split on `;` outside comments. Cypher line comments start with `//`."""
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("//")]
    body = "\n".join(lines)
    return tuple(s.strip() for s in body.split(";") if s.strip())


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    out: list[Migration] = []
    seen: set[int] = set()
    for p in sorted(directory.glob("*.cypher")):
        m = _NAME.match(p.name)
        if not m:
            raise MigrationError(f"migration file name must look like 0001_name.cypher: {p.name}")
        version = int(m.group(1))
        if version in seen:
            raise MigrationError(f"duplicate migration version {version}")
        seen.add(version)
        raw = p.read_text(encoding="utf-8")
        stmts = split_statements(raw)
        if not stmts:
            raise MigrationError(f"migration {p.name} has no statements")
        out.append(Migration(version, m.group(2), hashlib.sha256(raw.encode()).hexdigest(), stmts))
    if [m.version for m in out] != list(range(1, len(out) + 1)):
        raise MigrationError("migration versions must be contiguous from 0001")
    return out


_TRACKER = "CREATE CONSTRAINT graph_migration_version IF NOT EXISTS FOR (m:GraphMigration) REQUIRE m.version IS UNIQUE"


def apply_migrations(driver, database: str, migrations: list[Migration] | None = None) -> list[int]:
    """Apply pending migrations in order. Returns the versions applied now."""
    migrations = migrations if migrations is not None else load_migrations()
    applied_now: list[int] = []
    with driver.session(database=database) as s:
        s.run(_TRACKER).consume()
        done = {r["version"]: r["checksum"] for r in s.run("MATCH (m:GraphMigration) RETURN m.version AS version, m.checksum AS checksum")}
        for m in migrations:
            if m.version in done:
                if done[m.version] != m.checksum:
                    raise MigrationError(f"migration {m.version:04d}_{m.name} was changed after it was applied")
                continue
            for stmt in m.statements:
                s.run(stmt).consume()
            s.run(
                "MERGE (g:GraphMigration {version: $v}) SET g.name = $n, g.checksum = $c, g.applied_at = datetime()",
                v=m.version, n=m.name, c=m.checksum,
            ).consume()
            applied_now.append(m.version)
            log.info("graph migration applied", extra={"version": m.version})
    return applied_now


def main() -> int:
    from neo4j import GraphDatabase

    from app.config import Settings

    s = Settings()
    if not (s.neo4j_uri and s.neo4j_username and s.neo4j_password):
        print("NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD are not all set")
        return 2
    with GraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_username, s.neo4j_password)) as d:
        d.verify_connectivity()
        applied = apply_migrations(d, s.neo4j_database)
    print("applied:", applied or "nothing pending")
    return 0


if __name__ == "__main__":
    sys.exit(main())
