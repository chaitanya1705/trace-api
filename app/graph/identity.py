"""Deterministic identities (ontology O3).

Keys are built from length-delimited parts and hashed with SHA-256, so ("ab", "c") and ("a", "bc") can never collide the
way naive delimiter joining would, and user-controlled strings (paths, messages) cannot forge another entity's key.
Every id embeds the project, so identical paths, symbols or commits in two projects are distinct entities.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from typing import Any

ID_HEX = 32  # 128 bits of the digest; ample for non-adversarial ids, short enough to read in logs

# Prefixes make ids self-describing and keep the namespaces of different labels apart.
_PREFIX = {
    "Project": "prj", "Environment": "env", "Service": "svc", "Resource": "res", "Journey": "jrn", "SourceConnection": "con",
    "Repository": "rep", "Revision": "rev", "GraphBuild": "bld", "RevisionManifest": "man", "SourceFile": "fil",
    "SourceChunk": "chk", "CodeSymbol": "sym", "CodeCommunity": "com", "SchemaEntity": "sch", "Deployment": "dep",
    "Change": "chg", "EvidenceRecord": "evd", "EvidenceGap": "gap", "Ticket": "tkt", "InvestigationRun": "run",
    "Step": "stp", "ToolExecution": "exe", "RetrievalManifest": "rtm", "Hypothesis": "hyp", "Assessment": "asm",
    "Claim": "clm", "Finding": "fnd", "ImpactAssessment": "imp", "HumanCheck": "chc", "ResolutionPlan": "pln",
    "PatchVersion": "pat", "ExternalChangeTicket": "ect", "PullRequest": "prq", "VerificationRun": "ver",
    "ConversationAnswer": "ans", "Review": "rvw",
}


_EVIDENCE_SUBTYPES = frozenset({"LogEvent", "Measurement", "RequestSpan", "HumanObservation"})


def _enc(part: Any) -> bytes:
    if part is None:
        raise ValueError("identity parts must not be None; an unknown part cannot identify anything")
    if isinstance(part, bool):
        raise TypeError("identity parts must be str or int")
    if isinstance(part, int):
        b = f"i{part}".encode()
    elif isinstance(part, str):
        b = b"s" + part.encode("utf-8")
    else:
        raise TypeError(f"identity part must be str or int, got {type(part).__name__}")
    return str(len(b)).encode() + b":" + b


def canonical_digest(*parts: str | int) -> str:
    """SHA-256 hex of the length-delimited parts."""
    h = hashlib.sha256()
    for p in parts:
        h.update(_enc(p))
    return h.hexdigest()


def entity_id(label: str, project_id: str, *parts: str | int) -> str:
    """`<prefix>_<32 hex>` for a node of `label` in `project_id`."""
    if label in _EVIDENCE_SUBTYPES:
        label = "EvidenceRecord"  # a LogEvent is an EvidenceRecord: one identity per provider record
    if label not in _PREFIX:
        raise ValueError(f"no identity rule for label {label!r}")
    if not project_id:
        raise ValueError("project_id is required")
    return f"{_PREFIX[label]}_{canonical_digest(label, project_id, *parts)[:ID_HEX]}"


def content_hash(content: str | bytes) -> str:
    """SHA-256 of the exact UTF-8 bytes (no newline or Unicode normalisation: a citation pins what was read)."""
    data = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> str:
    """Stable JSON for hashing: sorted keys, no whitespace, UTF-8 safe."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ---- typed constructors (O3) -----------------------------------------------------------------------------------
def project_node_id(project_id: str) -> str:
    return entity_id("Project", project_id, project_id)


def environment_id(project_id: str, environment_key: str) -> str:
    return entity_id("Environment", project_id, environment_key)


def service_id(project_id: str, service_key: str) -> str:
    return entity_id("Service", project_id, service_key)


def resource_id(project_id: str, provider: str, resource_type: str, provider_resource_id: str) -> str:
    return entity_id("Resource", project_id, provider, resource_type, provider_resource_id)


def repository_id(project_id: str, provider: str, provider_repository_id: str) -> str:
    return entity_id("Repository", project_id, provider, provider_repository_id)


def revision_id(project_id: str, repository_node_id: str, commit_sha: str) -> str:
    """project + repository + commit. A project has no single universal SHA."""
    return entity_id("Revision", project_id, repository_node_id, commit_sha.lower())


def graph_build_id(project_id: str, revision_node_id: str, extractor_version: str, output_schema_version: str, attempt: int) -> str:
    return entity_id("GraphBuild", project_id, revision_node_id, extractor_version, output_schema_version, attempt)


def source_file_id(project_id: str, revision_node_id: str, path: str) -> str:
    """Revision + normalised, case-preserving repo path (case folding follows repo semantics, never applied here)."""
    return entity_id("SourceFile", project_id, revision_node_id, normalize_path(path))


def source_chunk_id(project_id: str, file_node_id: str, start_line: int, end_line: int, chunk_content_hash: str) -> str:
    return entity_id("SourceChunk", project_id, file_node_id, start_line, end_line, chunk_content_hash)


def code_symbol_id(project_id: str, file_node_id: str, qualified_name: str, kind: str, disambiguator: str | int) -> str:
    """File + qualified name + kind + signature/location, so overloads and same-named symbols stay distinct."""
    return entity_id("CodeSymbol", project_id, file_node_id, qualified_name, kind, disambiguator)


def evidence_id(project_id: str, source_connection_id: str, provider_record_id: str) -> str:
    return entity_id("EvidenceRecord", project_id, source_connection_id, provider_record_id)


def evidence_key_from_content(source_fields: Sequence[str | int]) -> tuple[str, str]:
    """For providers without a record id: hash stable source fields. Returns (record_key, dedupe_method).

    The method is stored on the node because content-hash dedupe has known collision limits (two genuinely
    different events with identical fields look the same).
    """
    return "ch_" + canonical_digest(*source_fields)[:ID_HEX], "content_hash"


def extraction_edge_key(build_id: str, source_id: str, relation: str, target_id: str, source_locator: str) -> str:
    """Build + source + relation + target + locator: distinct call sites remain distinct edges."""
    return canonical_digest("edge", build_id, source_id, relation, target_id, source_locator)


def edge_key(relation: str, source_id: str, target_id: str, *discriminators: str | int) -> str:
    """Key of a non-extraction edge; the discriminators are the context that makes two edges different facts."""
    return canonical_digest("rel", relation, source_id, target_id, *discriminators)


def manifest_hash(entries: Iterable[dict[str, str]]) -> str:
    """Hash of the sorted repository/revision/build entries, independent of the order they were listed in."""
    norm = sorted(({"repository_id": e["repository_id"], "revision_id": e["revision_id"], "build_id": e["build_id"]} for e in entries), key=canonical_json)
    return canonical_digest("manifest", canonical_json(norm))


def manifest_id(project_id: str, hash_hex: str) -> str:
    return entity_id("RevisionManifest", project_id, hash_hex)


def normalize_path(path: str) -> str:
    """Repo-relative, forward slashes, no leading './' or '/'. Rejects traversal so a path can never escape the repo."""
    p = path.replace("\\", "/")
    parts: list[str] = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise ValueError("path traversal is not allowed in a repository path")
        parts.append(seg)
    if not parts:
        raise ValueError("empty repository path")
    return "/".join(parts)
