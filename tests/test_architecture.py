"""Database schema capture (against a REAL Postgres: the embedded test server), system map, deployment sync (fixture Vercel responses)."""
import httpx
import psycopg
import pytest

import app.jobs.handlers  # noqa: F401
import app.jobs.handlers.deployments  # noqa: F401
import app.jobs.handlers.schema  # noqa: F401
from app.connectors import postgres_db as pgdb
from app.jobs import worker
from tests.conftest import token_for
from tests.test_connections import Provider

pytestmark = pytest.mark.db
API = "/api/v1"
KINDS = ["connection.verify", "schema.capture", "deployments.sync"]


async def drain(container):
    while await worker.run_one(container, "t", KINDS):
        pass


async def _project(client, org, envs=("production", "preview")):
    h = token_for(org["owner_id"], org["owner_email"])
    p = (await client.post(f"{API}/projects", headers=h, json={"name": f"Arch{org['id'].hex[:6]}", "environments": [{"name": e} for e in envs],
                                                              "services": [{"name": "api"}, {"name": "web"}]})).json()["data"]
    return p["id"], h


@pytest.fixture
async def shop_db(pg_dsn):
    """A small throwaway database shaped like a shop, in the same embedded server."""
    admin = await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True)
    await admin.execute("DROP DATABASE IF EXISTS shop_fixture WITH (FORCE)")
    await admin.execute("CREATE DATABASE shop_fixture")
    dsn = pg_dsn.rsplit("/", 1)[0] + "/shop_fixture"
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as c:
        await c.execute("""
            CREATE TABLE customers (id serial PRIMARY KEY, email text NOT NULL UNIQUE, api_key text DEFAULT 'sk-supersecretsupersecret1234');
            CREATE TABLE orders (id serial PRIMARY KEY, customer_id int NOT NULL REFERENCES customers(id), total_cents int NOT NULL DEFAULT 0);
            CREATE INDEX orders_customer ON orders(customer_id);
            CREATE VIEW big_orders AS SELECT * FROM orders WHERE total_cents > 1000;
            INSERT INTO customers(email) VALUES ('secret-row-data@example.com');""")
    yield dsn
    await admin.execute("DROP DATABASE IF EXISTS shop_fixture WITH (FORCE)")
    await admin.close()


async def test_introspection_reads_structure_never_rows(shop_db):
    snap = await pgdb.introspect(shop_db)
    t = {x["name"]: x for x in snap["tables"]}
    assert set(t) == {"customers", "orders", "big_orders"} and t["big_orders"]["kind"] == "view"
    assert t["orders"]["primary_key"] == ["id"] and t["orders"]["foreign_keys"][0]["ref_table"] == "customers"
    assert t["orders"]["foreign_keys"][0]["columns"] == ["customer_id"] and t["customers"]["unique"] == [["email"]]
    assert any(i["name"] == "orders_customer" for i in t["orders"]["indexes"])
    blob = str(snap)
    assert "secret-row-data" not in blob and "supersecretsupersecret" not in blob     # no row data; defaults are redacted
    # the session is read-only: a write through the same options must fail
    conn = await pgdb._connect(shop_db)
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        await conn.execute("CREATE TABLE nope(x int)")
    await conn.close()


async def test_verify_reports_privileges_and_bad_credentials(shop_db):
    ok = await pgdb.verify(shop_db)
    assert ok.outcome in ("connected", "connected_with_warnings") and [s.key for s in ok.stages][:2] == ["connect", "privileges"]
    assert ok.stages[-1].status == "skip"                                             # rows: explicitly not read
    bad = await pgdb.verify("postgresql://nobody:wrong@127.0.0.1:1/none")
    assert bad.outcome == "failed"
    assert (await pgdb.verify("not a dsn at all")).outcome == "failed"


async def test_connect_database_capture_and_serve_schema(client, container, org, shop_db):
    pid, h = await _project(client, org)
    empty = (await client.get(f"{API}/projects/{pid}/schema", headers=h)).json()["data"]
    assert empty["connected"] is False and empty["captured"] is False and empty["snapshot"] is None
    r = await client.post(f"{API}/projects/{pid}/connections", headers=h, json={"provider": "postgres", "auth_method": "postgres_dsn", "secret": shop_db + "?password=pw-fixture-12345"})
    assert r.status_code == 202, r.text
    cid = r.json()["data"]["resource_id"]
    await drain(container)
    c = (await client.get(f"{API}/connections/{cid}", headers=h)).json()["data"]
    assert c["status"] in ("connected", "connected_with_warnings") and c["config"]["database"] == "shop_fixture" and "password" not in str(c)
    password = "pw-fixture-12345"
    assert password not in (await client.get(f"{API}/connections/{cid}", headers=h)).text
    assert password not in (await client.get(f"{API}/projects/{pid}/connections", headers=h)).text
    s = (await client.get(f"{API}/projects/{pid}/schema", headers=h)).json()["data"]
    assert s["captured"] and s["snapshot"]["table_count"] == 3 and s["snapshot"]["database"] == "shop_fixture" and "structure only" in s["provenance"]
    # refresh queues a new capture; permissions: a viewer cannot refresh
    assert (await client.post(f"{API}/projects/{pid}/schema/refresh", headers=h)).status_code == 202
    await drain(container)


class VercelDeps(Provider):
    def handler(self, req):
        if req.url.host == "api.vercel.com" and req.url.path == "/v7/deployments":
            return httpx.Response(200, json={"deployments": [
                {"uid": "dpl_a", "projectId": "prj_1", "name": "api", "target": "production", "readyState": "READY", "created": 1760000000000, "ready": 1760000060000,
                 "url": "api.vercel.app", "meta": {"githubCommitSha": "218241b0cd512df5abcd288394858bc16ae8d659", "githubOrg": "acme", "githubRepo": "shop", "githubCommitRef": "main"}},
                {"uid": "dpl_b", "projectId": "prj_1", "name": "api", "target": None, "readyState": "READY", "created": 1759990000000, "meta": {}},
            ], "pagination": {}})
        return super().handler(req)


async def test_system_map_and_deployment_sync_label_origins_and_gaps(client, container, org):
    pid, h = await _project(client, org)
    container.extras["http_transport"] = httpx.MockTransport(VercelDeps().handler)
    v = (await client.post(f"{API}/projects/{pid}/connections", headers=h, json={"provider": "vercel", "auth_method": "vercel_token", "secret": "vcp_good_token"})).json()["data"]["resource_id"]
    g = (await client.post(f"{API}/projects/{pid}/connections", headers=h, json={"provider": "github", "auth_method": "github_pat", "secret": "github_pat_12345678901234567890"})).json()["data"]["resource_id"]
    await drain(container)
    # selections go through the validated endpoints
    container.settings.github_app_id = None
    vres = (await client.get(f"{API}/connections/{v}/resources", headers=h)).json()["data"]
    assert (await client.put(f"{API}/projects/{pid}/log-scope", headers=h, json={"environments": {"production": [{"provider_resource_id": vres[0]["provider_resource_id"], "service_key": "api"}]}})).status_code == 200
    async with container.pool.connection() as conn:   # bind a repository directly (discovery for PATs is fixture-limited)
        await conn.execute("INSERT INTO repository_bindings(organization_id,project_id,connection_id,provider_repository_id,full_name,default_branch,selected_ref) VALUES (%s,%s,%s,'9','acme/shop','main','main')", (org["id"], pid, g))
        await conn.commit()
    sync = await client.post(f"{API}/projects/{pid}/deployments/sync", headers=h)
    assert sync.status_code == 202
    await drain(container)
    deps = (await client.get(f"{API}/projects/{pid}/deployments", headers=h)).json()
    by = {d["id"]: d for d in deps["data"]}
    assert by["dpl_a"]["commit_sha"].startswith("218241b") and by["dpl_a"]["environment_key"] == "production" and by["dpl_a"]["revision_mapping"] == "provider_metadata"
    assert by["dpl_b"]["commit_sha"] is None and by["dpl_b"]["revision_mapping"] == "unknown" and by["dpl_b"]["environment_key"] == "preview"
    assert deps["meta"]["coverage"]["without_commit_sha"] == 1
    m = (await client.get(f"{API}/projects/{pid}/system-map", headers=h)).json()
    kinds = {n["kind"] for n in m["data"]["nodes"]}
    assert {"environment", "service", "hosting", "repository", "deployment"} <= kinds
    rel = {(e["relation"], e["origin"]) for e in m["data"]["edges"]}
    assert ("RUNS_ON", "configured") in rel and ("DEPLOYS_REVISION", "provider_metadata") in rel and ("HAS_DEPLOYMENT", "provider_metadata") in rel
    gaps = {x["kind"] for x in m["data"]["gaps"]}
    assert {"repository_unmapped", "logs_not_confirmed"} <= gaps                      # unmapped repo and unknown logs are gaps, not links
    assert not any(e["relation"] == "CALLS" for e in m["data"]["edges"])             # nothing "observed" is invented
    assert "not drawn" in m["data"]["provenance"]


async def test_empty_project_system_map_and_sync_needs_scope(client, container, org):
    pid, h = await _project(client, org)
    m = (await client.get(f"{API}/projects/{pid}/system-map", headers=h)).json()
    assert {n["kind"] for n in m["data"]["nodes"]} == {"environment", "service"} and m["data"]["edges"] == []
    assert (await client.post(f"{API}/projects/{pid}/deployments/sync", headers=h)).status_code == 409   # no hosting connection yet
    assert (await client.post(f"{API}/projects/{pid}/schema/refresh", headers=h)).json()["error"]["code"] == "no_database"
