"""Dependency that yields (driver, database) or answers 503 when Neo4j is not configured."""
from __future__ import annotations

from fastapi import Request

from app.errors import DependencyUnavailable


async def neo4j_driver(request: Request):
    c = request.app.state.container
    d = c.extras.get("neo4j")
    if d is None:
        raise DependencyUnavailable("The evidence graph (Neo4j) is not configured")
    return d, c.settings.neo4j_database
