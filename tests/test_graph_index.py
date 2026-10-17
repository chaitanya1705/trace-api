"""Indexing a bound GitHub repository into the graph. GitHub is a labeled FIXTURE (httpx.MockTransport); Postgres is the
embedded test server; Neo4j is the real instance (opt-in: TRACE_TEST_NEO4J=1). Archive safety tests need nothing external."""
from __future__ import annotations

import io
import os
import tarfile

import httpx
import pytest

import app.jobs.handlers  # noqa: F401  (registers graph.index)
from app.graph.archive import ArchiveError, extract_tarball
from app.jobs import worker
from tests.conftest import _creds
from tests.test_connections import API, Provider, _pem, _project, _run_jobs  # noqa: F401  (provider fixture needs _pem)

SHA = "a" * 40


def _tar(path, members):
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members:
            if data is None:  # symlink
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.SYMTYPE
                ti.linkname = "/etc/passwd"
                tf.addfile(ti)
                continue
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))


# ---- archive safety (no database, no network) --------------------------------------------------------------------
def test_extract_strips_top_folder_and_skips_links(tmp_path):
    a = str(tmp_path / "a.tgz")
    _tar(a, [("acme-pay-abc/src/a.py", b"def a():\n    return 1\n"), ("acme-pay-abc/link", None)])
    out = tmp_path / "out"
    out.mkdir()
    assert extract_tarball(a, str(out)) == 1
    assert (out / "src" / "a.py").read_text().startswith("def a")
    assert not (out / "link").exists()  # a symlink is never created or followed


@pytest.mark.parametrize("bad", ["root/../../evil.py", "/abs/evil.py", "root/a/../../../evil.py"])
def test_extract_refuses_path_escape(tmp_path, bad):
    a = str(tmp_path / "a.tgz")
    _tar(a, [(bad, b"x")])
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(ArchiveError):
        extract_tarball(a, str(out))
    assert not (tmp_path / "evil.py").exists()


def test_extract_refuses_non_archives(tmp_path):
    p = tmp_path / "x.tgz"
    p.write_bytes(b"not a tarball")
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(ArchiveError):
        extract_tarball(str(p), str(out))


# ---- end to end: connection -> bound repo -> queued run -> worker -> published graph -> read API ------------------
neo4j = pytest.mark.skipif(os.environ.get("TRACE_TEST_NEO4J") != "1", reason="set TRACE_TEST_NEO4J=1 to run against Neo4j")


class SourceProvider(Provider):
    """Adds the repository endpoints of api.github.com and the codeload redirect target. FIXTURE ONLY."""

    def __init__(self, archive: bytes):
        super().__init__()
        self.archive = archive
        self.commit_missing = False
        self.codeload_auth: list[str | None] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        u = req.url
        if u.host == "api.github.com" and u.path.startswith("/repos/acme/payments/commits/"):
            self.calls.append(f"GET {u.host}{u.path}")
            return httpx.Response(404, json={}) if self.commit_missing else httpx.Response(200, json={"sha": SHA})
        if u.host == "api.github.com" and u.path == f"/repos/acme/payments/tarball/{SHA}":
            self.calls.append(f"GET {u.host}{u.path}")
            return httpx.Response(302, headers={"location": f"https://codeload.github.com/acme/payments/legacy.tar.gz/{SHA}"})
        if u.host == "codeload.github.com":
            self.codeload_auth.append(req.headers.get("authorization"))
            return httpx.Response(200, content=self.archive)
        return super().handler(req)


@pytest.fixture
def source(container, tmp_path):
    a = str(tmp_path / "repo.tgz")
    _tar(a, [
        ("acme-payments-aaaa/src/pay.py", b"def charge():\n    return gateway()\n\ndef gateway():\n    return 1\n"),
        ("acme-payments-aaaa/src/api.py", b"from pay import charge\n\ndef handler():\n    return charge()\n"),
        ("acme-payments-aaaa/README.md", b"# payments\n"),
    ])
    p = SourceProvider(open(a, "rb").read())
    import tests.test_connections as tc
    container.extras["http_transport"] = httpx.MockTransport(p.handler)
    container.settings.github_app_id = "123"
    container.settings.github_app_private_key = tc._pem()
    return p


async def _bound_project(client, container, org):
    pid, h = await _project(client, org)
    cid = (await client.post(f"{API}/projects/{pid}/connections", headers=h, json={
        "provider": "github", "auth_method": "github_app", "config": {"installation_id": "4821"}})).json()["data"]["resource_id"]
    await _run_jobs(container)
    res = (await client.get(f"{API}/connections/{cid}/resources", headers=h)).json()["data"]
    r = await client.put(f"{API}/projects/{pid}/repositories", headers=h, json={"repositories": [{"provider_repository_id": res[0]["provider_repository_id"], "selected_ref": "main"}]})
    assert r.status_code == 200, r.text
    return pid, h


async def _drain(container):
    while await worker.run_one(container, "t", ["graph.index"]):
        pass


@neo4j
async def test_index_run_publishes_the_graph_and_is_readable(client, container, org, source):
    from neo4j import GraphDatabase

    from app.graph.migrate import apply_migrations
    from app.graph.repository import GraphRepository

    uri, user, pw, db = _creds()
    driver = GraphDatabase.driver(uri, auth=(user, pw))
    apply_migrations(driver, db)
    container.extras["neo4j"] = driver
    container.settings.neo4j_database = db
    pid, h = await _bound_project(client, container, org)
    try:
        r = await client.post(f"{API}/projects/{pid}/graph/index", headers=h, json={})
        assert r.status_code == 202
        run = r.json()["data"][0]
        assert run["status"] == "queued" and run["requested_ref"] == "main"
        again = (await client.post(f"{API}/projects/{pid}/graph/index", headers=h, json={})).json()["data"][0]
        assert again["id"] == run["id"]  # a second click returns the active run, never a duplicate

        await _drain(container)
        got = (await client.get(f"{API}/projects/{pid}/graph/index", headers=h)).json()["data"][0]
        assert got["status"] == "succeeded" and got["commit_sha"] == SHA and got["repository"] == "acme/payments"
        assert got["counts"]["symbols"] == 3 and got["error"] is None
        assert source.codeload_auth == [None]  # the credential is not forwarded to the download host

        graph = (await client.get(f"{API}/projects/{pid}/graph/code", headers=h)).json()["data"]
        names = {n["label"] for n in graph["nodes"]}
        assert {"charge", "gateway", "handler"} <= names and graph["builds"][0]["commit_sha"] == SHA
        assert any(e["relation"] == "CALLS" for e in graph["edges"])
    finally:
        GraphRepository(driver, db).delete_project(pid)
        driver.close()
        container.extras.pop("neo4j", None)


@neo4j
async def test_unknown_ref_fails_the_run_with_a_reason_and_keeps_nothing(client, container, org, source):
    from neo4j import GraphDatabase

    uri, user, pw, db = _creds()
    driver = GraphDatabase.driver(uri, auth=(user, pw))
    container.extras["neo4j"] = driver
    container.settings.neo4j_database = db
    pid, h = await _bound_project(client, container, org)
    source.commit_missing = True
    try:
        await client.post(f"{API}/projects/{pid}/graph/index", headers=h, json={})
        await _drain(container)
        got = (await client.get(f"{API}/projects/{pid}/graph/index", headers=h)).json()["data"][0]
        assert got["status"] == "failed" and got["error"]["code"] == "not_found" and got["build_id"] is None
        graph = (await client.get(f"{API}/projects/{pid}/graph/code", headers=h)).json()["data"]
        assert graph["status"] == "none"  # a failed run publishes nothing
    finally:
        driver.close()
        container.extras.pop("neo4j", None)


async def test_index_request_needs_selected_repositories(client, org):
    pid, h = await _project(client, org)
    r = await client.post(f"{API}/projects/{pid}/graph/index", headers=h, json={})
    assert r.status_code == 422  # nothing to index yet: tell the user to choose repositories first
