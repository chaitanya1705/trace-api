"""Validation of nodes and relationships against the ontology before anything is written.

Messages name fields and labels only, never values: property values can be redacted log text or other sensitive content.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.graph import ontology as O


class GraphValidationError(ValueError):
    """Raised with a list of problems (field/label names only)."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def _is_instant(field_name: str) -> bool:
    return field_name in O.TIMESTAMP_FIELDS or field_name.endswith(O.TIMESTAMP_SUFFIXES)


def _scalar(v: Any) -> bool:
    return isinstance(v, (str, int, float, bool))


def _check_value(label: str, name: str, v: Any, problems: list[str]) -> Any:
    """Return the storable form of a property value, recording problems."""
    if v is None:
        return None
    if _is_instant(name):
        if not isinstance(v, datetime):
            problems.append(f"{label}.{name}: expected a datetime")
            return v
        if v.tzinfo is None:
            problems.append(f"{label}.{name}: datetime must be timezone-aware (UTC)")
            return v
        return v.astimezone(UTC)
    if isinstance(v, dict):
        problems.append(f"{label}.{name}: nested maps are not stored; serialise as a JSON string")
        return v
    if isinstance(v, (list, tuple)):
        if not all(_scalar(x) for x in v):
            problems.append(f"{label}.{name}: lists may hold only scalars")
        return list(v)
    if not _scalar(v):
        problems.append(f"{label}.{name}: unsupported value type")
    return v


def validate_node(label: str, props: dict[str, Any]) -> dict[str, Any]:
    """Check `props` against the spec for `label`; return the storable properties (None values dropped, instants in UTC)."""
    spec = O.NODES.get(label)
    if spec is None:
        raise GraphValidationError([f"unknown label {label}"])
    problems: list[str] = []
    allowed = spec.fields
    for k in props:
        if k not in allowed:
            problems.append(f"{label}.{k}: not part of the ontology")
    nullable = set(O.NULLABLE_REQUIRED.get(label, ()))
    for k in (*O.COMMON_FIELDS, *spec.required):
        if k in nullable:
            if k not in props:
                problems.append(f"{label}.{k}: key is required (null means unknown)")
        elif props.get(k) is None or props.get(k) == "":
            problems.append(f"{label}.{k}: required")
    for k in nullable:
        if k not in props and k in spec.optional:
            problems.append(f"{label}.{k}: key is required (null means unknown)")
    if spec.one_of and not any(props.get(k) is not None for k in spec.one_of):
        problems.append(f"{label}: one of {', '.join(spec.one_of)} is required")
    out: dict[str, Any] = {}
    for k, v in props.items():
        stored = _check_value(label, k, v, problems)
        if stored is not None:
            out[k] = stored
    for (lab, fld), allowed_values in O.ENUMS.items():
        if lab not in spec.labels or fld not in props or props[fld] is None:
            continue
        vals = props[fld] if isinstance(props[fld], (list, tuple)) else [props[fld]]
        if any(x not in allowed_values for x in vals):
            problems.append(f"{label}.{fld}: value outside the controlled vocabulary")
    if problems:
        raise GraphValidationError(problems)
    return out


def validate_relationship(rel_type: str, from_label: str, to_label: str, props: dict[str, Any]) -> dict[str, Any]:
    spec = O.RELATIONSHIPS.get(rel_type)
    if spec is None:
        raise GraphValidationError([f"unknown relationship {rel_type}"])
    problems: list[str] = []
    if from_label not in O.NODES or to_label not in O.NODES:
        raise GraphValidationError([f"{rel_type}: unknown endpoint label"])
    if not (O.labels_of(from_label) & spec.sources):
        problems.append(f"{rel_type}: source label {from_label} not allowed")
    if not (O.labels_of(to_label) & spec.targets):
        problems.append(f"{rel_type}: target label {to_label} not allowed")
    for k in props:
        if k not in spec.context:
            problems.append(f"{rel_type}.{k}: not part of the relationship context")
    for k in ("project_id", "key", *spec.required):
        if props.get(k) is None or props.get(k) == "":
            problems.append(f"{rel_type}.{k}: required")
    out: dict[str, Any] = {}
    for k, v in props.items():
        stored = _check_value(rel_type, k, v, problems)
        if stored is not None:
            out[k] = stored
    if "derivation" in props and props["derivation"] not in O.DERIVATIONS:
        problems.append(f"{rel_type}.derivation: value outside the controlled vocabulary")
    if "verification" in props and props["verification"] not in O.VERIFICATIONS:
        problems.append(f"{rel_type}.verification: value outside the controlled vocabulary")
    if "evidence_ids" in props and not isinstance(props["evidence_ids"], (list, tuple)):
        problems.append(f"{rel_type}.evidence_ids: must be a list of ids")
    if "origin" in props and props["origin"] not in O.ORIGINS:
        problems.append(f"{rel_type}.origin: value outside the controlled vocabulary")
    if problems:
        raise GraphValidationError(problems)
    return out
