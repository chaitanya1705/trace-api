"""Check that the configured Neo4j instance is reachable. Prints server facts only, never credentials.

Run: .venv/Scripts/python scripts/neo4j_ping.py
"""
from __future__ import annotations

import sys

from neo4j import GraphDatabase

from app.config import Settings


def main() -> int:
    s = Settings()
    if not (s.neo4j_uri and s.neo4j_username and s.neo4j_password):
        print("NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD are not all set")
        return 2
    scheme = s.neo4j_uri.split("://", 1)[0]
    try:
        with GraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_username, s.neo4j_password)) as d:
            d.verify_connectivity()
            with d.session(database=s.neo4j_database) as ses:
                comp = ses.run("CALL dbms.components() YIELD name, versions, edition RETURN name, versions[0] AS v, edition").single()
                n = ses.run("MATCH (n) RETURN count(n) AS n").single()["n"]
    except Exception as e:  # report the class only; messages can echo hosts
        print(f"connection failed: {type(e).__name__}")
        return 1
    print(f"ok scheme={scheme} database={s.neo4j_database} server={comp['name']} {comp['v']} {comp['edition']} nodes={n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
