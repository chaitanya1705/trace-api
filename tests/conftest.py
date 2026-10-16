"""Test harness. Doubles in this file are TEST-ONLY and are never importable from app/ configuration paths."""
from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from app.auth.jwt import Principal
from app.config import Settings
from app.container import Container
from app.errors import Unauthenticated
from app.main import create_app
from app.storage.postgres.migrate import migrate
from app.storage.postgres.pool import open_pool
from app.storage.secrets import Vault, generate_key


class FakeVerifier:
    """TEST DOUBLE. Token format: 'test|<user-uuid>|<email>'. Production never constructs this."""

    async def verify(self, token: str) -> Principal:
        parts = token.split("|")
        if len(parts) != 3 or parts[0] != "test":
            raise Unauthenticated("Invalid credentials")
        return Principal(uuid.UUID(parts[1]), parts[2], parts[2].split("@")[0])


def token_for(user_id: uuid.UUID, email: str) -> dict[str, str]:
    return {"authorization": f"Bearer test|{user_id}|{email}"}


@pytest.fixture(scope="session")
def pg_dsn():
    import pgserver

    d = Path(tempfile.mkdtemp(prefix="trace-pg-"))
    server = pgserver.get_server(d, cleanup_mode="stop")
    uri = server.get_uri()
    yield uri
    server.cleanup()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def migrated_dsn(pg_dsn):
    await migrate(pg_dsn)
    return pg_dsn


@pytest.fixture
def settings() -> Settings:
    return Settings(trace_env="test", trace_secret_key=generate_key())


@pytest_asyncio.fixture
async def container(migrated_dsn, settings):
    pool = await open_pool(migrated_dsn, max_size=4)
    c = Container(settings=settings, pool=pool, verifier=FakeVerifier(), vault=Vault.from_settings(settings))
    yield c
    await pool.close()


@pytest_asyncio.fixture
async def client(container, settings):
    app = create_app(settings, container)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def add_member(container, org_id, role="investigator", email=None):
    uid = uuid.uuid4()
    email = email or f"u-{uid.hex[:6]}@example.com"
    async with container.pool.connection() as conn:
        await conn.execute("INSERT INTO memberships(organization_id,user_id,email,role) VALUES (%s,%s,%s,%s)",
                           (org_id, uid, email, role))
        await conn.commit()
    return uid, email


@pytest_asyncio.fixture
async def org(container):
    """A fresh organization with an owner, an admin-less world otherwise."""
    owner_id = uuid.uuid4()
    async with container.pool.connection() as conn:
        row = await (await conn.execute(
            "INSERT INTO organizations(name) VALUES (%s) RETURNING id", (f"org-{uuid.uuid4().hex[:6]}",)
        )).fetchone()
        await conn.execute(
            "INSERT INTO memberships(organization_id, user_id, email, role) VALUES (%s,%s,%s,'owner')",
            (row["id"], owner_id, f"owner-{owner_id.hex[:6]}@example.com"),
        )
        await conn.commit()
    return {"id": row["id"], "owner_id": owner_id, "owner_email": f"owner-{owner_id.hex[:6]}@example.com"}
