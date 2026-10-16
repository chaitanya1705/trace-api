"""Process-wide dependencies. Production adapters only; test doubles are injected from tests/ (A6/Context)."""
from __future__ import annotations

from dataclasses import dataclass, field

from psycopg_pool import AsyncConnectionPool

from app.auth.jwt import TokenVerifier
from app.config import Settings
from app.storage.secrets import Vault


@dataclass
class Container:
    settings: Settings
    pool: AsyncConnectionPool | None = None
    verifier: TokenVerifier | None = None
    vault: Vault | None = None
    extras: dict[str, object] = field(default_factory=dict)  # neo4j driver, http clients, ... added by later tasks
