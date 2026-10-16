"""Neo4j driver factory. Returns None when Neo4j is not configured so dependent endpoints can answer 503 honestly."""
from __future__ import annotations

import logging

from app.config import Settings

log = logging.getLogger("trace.graph")


def create_driver(settings: Settings):
    if not (settings.neo4j_uri and settings.neo4j_username and settings.neo4j_password):
        return None
    from neo4j import GraphDatabase

    try:
        return GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_username, settings.neo4j_password), max_connection_lifetime=1800)
    except Exception as e:  # malformed URI etc.: report the class only
        log.warning("neo4j driver not created", extra={"error": type(e).__name__})
        return None


def migrate_graph(settings: Settings, driver) -> list[int]:
    """Apply pending graph migrations. A graph that cannot be reached must not stop the API from starting."""
    from app.graph.migrate import apply_migrations

    try:
        return apply_migrations(driver, settings.neo4j_database)
    except Exception as e:  # class only: messages can echo hosts
        log.warning("graph migrations not applied", extra={"error": type(e).__name__})
        return []
