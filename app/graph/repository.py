"""Writes to the evidence graph. Every write is validated against the ontology first; labels and relationship types are
interpolated into Cypher only after being checked against the ontology allowlist (they cannot be query parameters),
and all values travel as parameters. Callers pass already-authorized project ids (authorization is the backend's job).
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.graph import ontology as O
from app.graph.identity import edge_key
from app.graph.validation import GraphValidationError, validate_node, validate_relationship

BATCH = 500


class GraphWriteError(RuntimeError):
    """A write that is well-formed but refused (missing endpoint, immutable content conflict)."""


@dataclass(frozen=True)
class NodeWrite:
    label: str
    id: str
    project_id: str
    props: dict[str, Any]


@dataclass(frozen=True)
class RelWrite:
    type: str
    from_label: str
    from_id: str
    to_label: str
    to_id: str
    project_id: str
    key: str
    props: dict[str, Any]


_VOLATILE = frozenset({"created_at", "ingested_at", "schema_version"})


def fingerprint(props: dict[str, Any]) -> str:
    """Hash of the content-bearing properties of an immutable node (volatile bookkeeping fields excluded)."""
    from app.graph.identity import canonical_json

    body = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in props.items() if k not in _VOLATILE}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def _chunks(items: list, n: int = BATCH):
    for i in range(0, len(items), n):
        yield items[i : i + n]


def prepare_node(label: str, id: str, project_id: str, props: dict[str, Any], *, now: datetime | None = None) -> NodeWrite:
    """Fill the common fields and validate. `created_at` is the time TRACE wrote the node, not an event time."""
    full = {"id": id, "project_id": project_id, "schema_version": O.SCHEMA_VERSION, "created_at": now or datetime.now(UTC), **props}
    return NodeWrite(label, id, project_id, validate_node(label, full))


def prepare_rel(type_: str, from_label: str, from_id: str, to_label: str, to_id: str, project_id: str, key: str, props: dict[str, Any] | None = None) -> RelWrite:
    full = {"project_id": project_id, "key": key, **(props or {})}
    return RelWrite(type_, from_label, from_id, to_label, to_id, project_id, key, validate_relationship(type_, from_label, to_label, full))


class GraphRepository:
    """Thin, synchronous wrapper over a neo4j driver (writes run in the worker; the API reads through query templates)."""

    def __init__(self, driver, database: str):
        self._driver = driver
        self._db = database

    # ---- nodes -------------------------------------------------------------------------------------------------
    def write_nodes(self, nodes: list[NodeWrite]) -> int:
        """Idempotent MERGE by id.

        - An id that already exists under another project is refused (ids embed the project, so this means a bug or forgery).
        - Immutable labels are written once; writing the same id again with different content is refused. Volatile fields
          (created_at, ingested_at) do not count as content, so re-ingesting the same record is a harmless no-op.
        """
        by_label: dict[str, list[NodeWrite]] = defaultdict(list)
        for n in nodes:
            by_label[n.label].append(n)
        written = 0
        with self._driver.session(database=self._db) as s:
            for label, group in by_label.items():
                spec = O.NODES[label]
                labels = "".join(f":`{x}`" for x in spec.labels)  # allowlisted: spec.labels come from the ontology
                for part in _chunks(group):
                    rows = [{"id": n.id, "project_id": n.project_id, "props": n.props, "fp": fingerprint(n.props)} for n in part]
                    head = (
                        "UNWIND $rows AS row "
                        f"MERGE (n:`{O.BASE_LABEL}` {{id: row.id}}) "
                        "ON CREATE SET n.project_id = row.project_id, n._fresh = true "
                        "WITH n, row WHERE n.project_id = row.project_id "
                    )
                    if spec.immutable:
                        cypher = head + (
                            "WITH n, row, coalesce(n._fresh, false) AS fresh "
                            f"SET n += CASE WHEN fresh THEN row.props ELSE {{}} END, n{labels} "
                            "SET n._fp = coalesce(n._fp, row.fp) "
                            "REMOVE n._fresh "
                            "RETURN row.id AS id, (n._fp <> row.fp) AS conflict"
                        )
                    else:
                        cypher = head + f"SET n += row.props, n{labels} REMOVE n._fresh RETURN row.id AS id, false AS conflict"
                    res = list(s.run(cypher, rows=rows))
                    if len(res) != len(part):
                        raise GraphWriteError("node id already belongs to another project")
                    if any(r["conflict"] for r in res):
                        raise GraphWriteError(f"immutable {label} was rewritten with different content")
                    written += len(res)
        return written

    def write_relationships(self, rels: list[RelWrite]) -> int:
        """Idempotent MERGE by (type, key). Both endpoints must already exist in the same project."""
        by_type: dict[str, list[RelWrite]] = defaultdict(list)
        for r in rels:
            by_type[r.type].append(r)
        written = 0
        with self._driver.session(database=self._db) as s:
            for type_, group in by_type.items():
                if type_ not in O.RELATIONSHIPS:
                    raise GraphValidationError([f"unknown relationship {type_}"])
                for part in _chunks(group):
                    self._check_scope_and_evidence(s, type_, part)
                    rows = [{"from_id": r.from_id, "to_id": r.to_id, "project_id": r.project_id, "key": r.key, "props": r.props} for r in part]
                    cypher = (
                        "UNWIND $rows AS row "
                        f"MATCH (a:`{O.BASE_LABEL}` {{id: row.from_id, project_id: row.project_id}}) "
                        f"MATCH (b:`{O.BASE_LABEL}` {{id: row.to_id, project_id: row.project_id}}) "
                        f"MERGE (a)-[r:`{type_}` {{key: row.key}}]->(b) "
                        "SET r += row.props "
                        "RETURN row.key AS key"
                    )
                    got = {r["key"] for r in s.run(cypher, rows=rows)}
                    missing = [r for r in part if r.key not in got]
                    if missing:
                        raise GraphWriteError(f"{type_}: {len(missing)} relationship(s) reference an endpoint that does not exist in the project")
                    written += len(got)
        return written

    def _check_scope_and_evidence(self, session, type_: str, part: list[RelWrite]) -> None:
        """O1: a scope relationship must agree with the scalar scope field on its source node. O4: evidence_ids must exist."""
        field_ = O.SCOPE_AGREEMENT.get(type_)
        if field_:
            rows = [{"a": r.from_id, "b": r.to_id, "pid": r.project_id} for r in part]
            bad = session.run(
                f"UNWIND $rows AS row MATCH (a:`{O.BASE_LABEL}` {{id: row.a, project_id: row.pid}}) "
                f"WHERE a.`{field_}` IS NOT NULL AND a.`{field_}` <> row.b RETURN count(*) AS n",
                rows=rows,
            ).single()["n"]
            if bad:
                raise GraphWriteError(f"{type_}: relationship contradicts the node's own {field_}")
        ids = {(r.project_id, i) for r in part for f in O.EVIDENCE_ID_FIELDS for i in (r.props.get(f) or [])}
        for pid in {p for p, _ in ids}:
            want = sorted(i for p, i in ids if p == pid)
            have = {x["id"] for x in session.run(f"MATCH (n:`{O.BASE_LABEL}`) WHERE n.project_id = $pid AND n.id IN $ids RETURN n.id AS id", pid=pid, ids=want)}
            if set(want) - have:
                raise GraphWriteError(f"{type_}: evidence_ids reference entities that do not exist in the project")

    # ---- builds and manifests (O3, O7) ------------------------------------------------------------------------
    def set_build_status(self, project_id: str, build_id: str, status: str) -> None:
        """staging -> published | failed; published -> superseded. A failed rebuild leaves the previous published build alone."""
        if status not in O.BUILD_STATUSES:
            raise GraphValidationError(["GraphBuild.status: value outside the controlled vocabulary"])
        allowed = {"published": ("staging",), "failed": ("staging",), "superseded": ("published",), "staging": ()}[status]
        with self._driver.session(database=self._db) as s:
            row = s.run("MATCH (b:GraphBuild {id: $id, project_id: $pid}) RETURN b.status AS st", id=build_id, pid=project_id).single()
            if row is None:
                raise GraphWriteError("build not found in the project")
            if row["st"] not in allowed:
                raise GraphWriteError(f"build cannot move from {row['st']} to {status}")
            s.run("MATCH (b:GraphBuild {id: $id, project_id: $pid}) SET b.status = $st", id=build_id, pid=project_id, st=status).consume()

    def create_manifest(self, project_id: str, entries: list[dict[str, str]]) -> str:
        """A RevisionManifest selects exactly the published builds of one analysis; staging/failed builds are refused."""
        from app.graph.identity import canonical_json, manifest_hash, manifest_id

        if not entries:
            raise GraphWriteError("a manifest needs at least one entry")
        with self._driver.session(database=self._db) as s:
            rows = s.run(
                "MATCH (b:GraphBuild) WHERE b.project_id = $pid AND b.id IN $ids RETURN b.id AS id, b.status AS st, b.revision_id AS rev",
                pid=project_id, ids=[e["build_id"] for e in entries],
            )
            found = {r["id"]: r for r in rows}
        for e in entries:
            b = found.get(e["build_id"])
            if b is None or b["st"] != "published" or b["rev"] != e["revision_id"]:
                raise GraphWriteError("manifest entries must be published builds of the stated revision")
        h = manifest_hash(entries)
        mid = manifest_id(project_id, h)
        self.write_nodes([prepare_node("RevisionManifest", mid, project_id, {"entries_json": canonical_json(sorted(entries, key=canonical_json)), "manifest_hash": h})])
        self.write_relationships([
            prepare_rel("SELECTS_BUILD", "RevisionManifest", mid, "GraphBuild", e["build_id"], project_id, edge_key("SELECTS_BUILD", mid, e["build_id"]), {"repository_id": e["repository_id"]})
            for e in entries
        ])
        return mid

    def manifest_build_ids(self, project_id: str, manifest_node_id: str) -> list[str]:
        with self._driver.session(database=self._db) as s:
            return [r["id"] for r in s.run(
                "MATCH (m:RevisionManifest {id: $m, project_id: $pid})-[:SELECTS_BUILD]->(b:GraphBuild {project_id: $pid, status: 'published'}) RETURN b.id AS id ORDER BY id",
                m=manifest_node_id, pid=project_id,
            )]

    # ---- citations (O5, O11) ----------------------------------------------------------------------------------
    def citable_ids(self, project_id: str, retrieval_manifest_id: str) -> set[str]:
        """Ids a claim may cite: exactly the items in the retrieval manifest."""
        import json

        with self._driver.session(database=self._db) as s:
            row = s.run("MATCH (m:RetrievalManifest {id: $m, project_id: $pid}) RETURN m.item_ids_json AS j", m=retrieval_manifest_id, pid=project_id).single()
        return set(json.loads(row["j"])) if row else set()

    def check_citations(self, project_id: str, retrieval_manifest_id: str, cited_ids: list[str]) -> list[str]:
        """Return cited ids that are NOT in the manifest (a citation outside what was retrieved is rejected)."""
        ok = self.citable_ids(project_id, retrieval_manifest_id)
        return [c for c in cited_ids if c not in ok]

    # ---- maintenance ------------------------------------------------------------------------------------------
    def delete_project(self, project_id: str, batch: int = 5000) -> int:
        """Remove every node of one project (disconnect/retention). Returns the number of nodes deleted."""
        total = 0
        with self._driver.session(database=self._db) as s:
            while True:
                n = s.run(
                    "MATCH (n:Entity {project_id: $pid}) WITH n LIMIT $batch DETACH DELETE n RETURN count(*) AS n", pid=project_id, batch=batch
                ).single()["n"]
                total += n
                if n < batch:
                    return total

    def counts(self, project_id: str) -> dict[str, int]:
        with self._driver.session(database=self._db) as s:
            rows = s.run(
                "MATCH (n:Entity {project_id: $pid}) UNWIND labels(n) AS l WITH l WHERE l <> 'Entity' RETURN l AS label, count(*) AS n ORDER BY label", pid=project_id
            )
            return {r["label"]: r["n"] for r in rows}
