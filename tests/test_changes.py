"""Changes and patch proposals. Patch text, rows and the source used below are labeled TEST FIXTURES.

Part 1 needs nothing external: edit validation, diff and hash. Part 2 uses the embedded Postgres only: approvals, policy,
stale-hash rejection, revocation, patch download and the honest draft-PR state. Part 3 uses the real Neo4j (opt-in) to
prove a proposal is built from exact indexed source and stored as an immutable PatchVersion."""
from __future__ import annotations

import json
import os
import uuid

import pytest

from app.domain.changes import propose as P
from tests.conftest import add_member, token_for

API = "/api/v1"
BASE = "b" * 40
SRC = "def discount(total, pct):\n    return total * pct / 100\n\n\ndef price(total, coupon):\n    return total - discount(total, coupon.percent_off)\n"


def _src(text: str = SRC, path: str = "shop/pricing.py") -> P.Source:
    return P.Source(path, "rev_x", BASE, text, "h" * 64)


def _out(**kw):
    base = {"defect_identified": True, "title": "Guard missing percentage", "rationale": "percent_off is None for fixed coupons", "risk": "low", "risk_reason": "one function", "tests": ["pytest -k coupon"], "rollback": "revert",
            "edits": [{"path": "shop/pricing.py", "find": "    return total * pct / 100\n", "replace": "    return total * (pct or 0) / 100\n", "explanation": "treat None as 0"}]}
    return {**base, **kw}


# ---- part 1: validation, no services -------------------------------------------------------------------------------
def test_valid_edit_builds_a_diff_a_hash_and_a_real_syntax_check():
    prop = P.make_proposal(_out(), [_src()])
    assert "-    return total * pct / 100" in prop.diff and "+    return total * (pct or 0) / 100" in prop.diff
    assert prop.files[0]["additions"] == 1 and prop.files[0]["deletions"] == 1
    assert prop.checks == [{"path": "shop/pricing.py", "check": "python_syntax", "result": "passed"}]
    assert prop.content_hash == P.make_proposal(_out(), [_src()]).content_hash          # deterministic
    assert prop.content_hash != P.make_proposal(_out(edits=[{**_out()["edits"][0], "replace": "    return total * (pct or 1) / 100\n"}]), [_src()]).content_hash


def test_edits_outside_the_shown_files_or_not_matching_exactly_once_are_rejected():
    e = _out()["edits"][0]
    with pytest.raises(P.ProposalError) as x:
        P.make_proposal(_out(edits=[{**e, "path": "shop/other.py"}, {**e, "find": "nope"}, {**e, "path": ".env", "find": "A", "replace": "B"}]), [_src()])
    assert x.value.code == "no_verifiable_change"
    twice = "x = 1\nx = 1\n"
    with pytest.raises(P.ProposalError):
        P.make_proposal(_out(edits=[{"path": "a.py", "find": "x = 1", "replace": "x = 2", "explanation": ""}]), [_src(twice, "a.py")])


def test_a_change_that_does_not_parse_is_never_saved():
    bad = _out(edits=[{**_out()["edits"][0], "replace": "    return total * (pct or 0 / 100\n"}])
    with pytest.raises(P.ProposalError) as x:
        P.make_proposal(bad, [_src()])
    assert x.value.code == "syntax_check_failed"


def test_a_model_that_finds_no_defect_cannot_produce_a_change_even_if_it_offers_edits():
    with pytest.raises(P.ProposalError) as e:
        P.make_proposal(_out(defect_identified=False, rationale="The checkout works as expected."), [_src()])
    assert e.value.code == "no_defect"


def test_no_edits_means_no_proposal_and_says_why():
    with pytest.raises(P.ProposalError) as x:
        P.make_proposal(_out(edits=[], rationale="The evidence does not show which rule is wrong."), [_src()])
    assert "does not show which rule" in x.value.message


# ---- part 2: approvals and policy on Postgres ----------------------------------------------------------------------
pg = pytest.mark.db


async def _change(client, org, container, name="Chg"):
    h = token_for(org["owner_id"], org["owner_email"])
    p = (await client.post(f"{API}/projects", headers=h, json={"name": name})).json()["data"]
    pid = uuid.UUID(p["id"])
    run = uuid.uuid4()
    async with container.pool.connection() as conn:
        ch = await (await conn.execute(
            "INSERT INTO change_proposals(organization_id, project_id, run_id, title, rationale, risk, created_by) VALUES (%s,%s,%s,'Fix','why','low',%s) RETURNING id",
            (org["id"], pid, run, org["owner_id"]))).fetchone()
        v = await (await conn.execute(
            "INSERT INTO patch_versions(change_id, version, base_commit, base_revision_id, files, diff, content_hash, created_by) VALUES (%s,1,%s,'rev_x',%s::jsonb,%s,%s,%s) RETURNING content_hash",
            (ch["id"], BASE, json.dumps([{"path": "a.py", "additions": 1, "deletions": 1, "diff": "d", "before_hash": "h"}]), "--- a/a.py\n+++ b/a.py\n", "a" * 64, org["owner_id"]))).fetchone()
        await conn.commit()
    return h, p["id"], str(ch["id"]), v["content_hash"]


async def _approver(client, container, org, pid, h):
    uid, email = await add_member(container, org["id"], "viewer")
    members = [{"user_id": str(org["owner_id"]), "role": "admin"}, {"user_id": str(org["owner_id"]), "role": "investigator"}, {"user_id": str(uid), "role": "approver"}]
    assert (await client.put(f"{API}/projects/{pid}/members", headers=h, json={"members": members})).status_code == 200
    return token_for(uid, email)


@pg
async def test_author_cannot_approve_and_needs_the_approver_role(client, org, container):
    h, pid, cid, ph = await _change(client, org, container)
    d = (await client.get(f"{API}/changes/{cid}", headers=h)).json()["data"]
    assert d["eligibility"]["can_approve"] is False and "approver role" in d["eligibility"]["reason"]
    denied = await client.post(f"{API}/changes/{cid}/approvals", headers=h, json={"decision": "approve", "patch_hash": ph})
    assert denied.status_code == 403
    assert d["draft_pr"]["available"] is False and d["current"]["test_status"] == "not_run"   # nothing was run, nothing was opened


@pg
async def test_independent_approver_approves_hash_bound_and_stale_hash_is_refused(client, org, container):
    h, pid, cid, ph = await _change(client, org, container, "Chg2")
    ha = await _approver(client, container, org, pid, h)
    stale = await client.post(f"{API}/changes/{cid}/approvals", headers=ha, json={"decision": "approve", "patch_hash": "f" * 64})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_patch"
    ok = await client.post(f"{API}/changes/{cid}/approvals", headers=ha, json={"decision": "approve", "patch_hash": ph})
    assert ok.status_code == 200 and ok.json()["data"]["status"] == "approved" and ok.json()["data"]["approved_count"] == 1
    again = await client.post(f"{API}/changes/{cid}/approvals", headers=ha, json={"decision": "approve", "patch_hash": ph})
    assert again.status_code == 403                                                    # one decision per person per version
    back = await client.post(f"{API}/changes/{cid}/approvals/revoke", headers=ha)
    assert back.status_code == 200 and back.json()["data"]["status"] == "proposed" and back.json()["data"]["approved_count"] == 0


@pg
async def test_required_approval_count_and_solo_policy(client, org, container):
    h, pid, cid, ph = await _change(client, org, container, "Chg3")
    assert (await client.put(f"{API}/projects/{pid}/change-policy", headers=h, json={"required_approvals": 2, "author_can_approve": False})).status_code == 200
    ha = await _approver(client, container, org, pid, h)
    one = (await client.post(f"{API}/changes/{cid}/approvals", headers=ha, json={"decision": "approve", "patch_hash": ph})).json()["data"]
    assert one["status"] == "proposed" and one["approved_count"] == 1 and one["policy"]["required_approvals"] == 2   # one of two is not approved
    # a solo project may opt in to the author approving, when the author also holds the approver role
    solo = [{"user_id": str(org["owner_id"]), "role": r} for r in ("admin", "investigator", "approver")]
    assert (await client.put(f"{API}/projects/{pid}/members", headers=h, json={"members": solo})).status_code == 200
    blocked = (await client.get(f"{API}/changes/{cid}", headers=h)).json()["data"]["eligibility"]
    assert blocked["can_approve"] is False and "independent approver" in blocked["reason"]
    assert (await client.put(f"{API}/projects/{pid}/change-policy", headers=h, json={"required_approvals": 1, "author_can_approve": True})).status_code == 200
    assert (await client.get(f"{API}/changes/{cid}", headers=h)).json()["data"]["eligibility"]["can_approve"] is True


@pg
async def test_reject_withdraw_patch_download_and_draft_pr_is_honestly_unavailable(client, org, container):
    h, pid, cid, ph = await _change(client, org, container, "Chg4")
    ha = await _approver(client, container, org, pid, h)
    patch = await client.get(f"{API}/changes/{cid}/patch", headers=h)
    assert patch.status_code == 200 and "Proposed only. Not applied, not tested." in patch.text and f"base commit {BASE}" in patch.text
    not_yet = await client.post(f"{API}/changes/{cid}/draft-pr", headers=h)
    assert not_yet.status_code == 409 and not_yet.json()["error"]["code"] == "not_approved"
    await client.post(f"{API}/changes/{cid}/approvals", headers=ha, json={"decision": "approve", "patch_hash": ph})
    pr = await client.post(f"{API}/changes/{cid}/draft-pr", headers=h)
    assert pr.status_code == 503 and pr.json()["error"]["code"] == "write_connection_missing"      # no write credential exists; nothing was written
    wd = await client.post(f"{API}/changes/{cid}/withdraw", headers=h)
    assert wd.json()["data"]["status"] == "withdrawn"
    listed = (await client.get(f"{API}/changes", headers=h)).json()["data"]
    assert [c["status"] for c in listed if c["id"] == cid] == ["withdrawn"]


@pg
async def test_changes_are_invisible_outside_the_project_and_organization(client, org, container):
    h, pid, cid, _ = await _change(client, org, container, "Chg5")
    other_uid, other_email = await add_member(container, org["id"], "viewer")
    assert (await client.get(f"{API}/changes/{cid}", headers=token_for(other_uid, other_email))).status_code == 404   # org member without a project grant
    assert cid not in [c["id"] for c in (await client.get(f"{API}/changes", headers=token_for(other_uid, other_email))).json()["data"]]


# ---- part 3: from exact indexed source, real Neo4j ------------------------------------------------------------------
@pytest.mark.skipif(not os.environ.get("TRACE_TEST_NEO4J"), reason="needs a real Neo4j (set TRACE_TEST_NEO4J=1)")
def test_proposal_is_built_from_exact_indexed_source_and_stored_as_an_immutable_patch_version(repo, pid, driver, tmp_path, monkeypatch):
    from app.api.v1.changes import _persist_patch
    from app.graph.indexer import index_repository
    from app.graph.repository import prepare_node, prepare_rel
    from tests.conftest import _creds

    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "pricing.py").write_text(SRC)
    rep = index_repository(repo, project_id=pid, root=str(tmp_path), repository_name="fixture-shop", commit_sha=BASE)
    db = _creds()[3]
    with driver.session(database=db) as s:
        sym = s.run("MATCH (c:Entity:CodeSymbol {project_id: $p, qualified_name: 'discount'}) RETURN c.id AS id", p=pid).single()["id"]
    run_id = str(uuid.uuid4())
    graph_run = P.I.entity_id("InvestigationRun", pid, run_id)
    repo.write_nodes([prepare_node("InvestigationRun", graph_run, pid, {"control_run_id": run_id, "environment_id": "e", "window_start": rep and __import__("datetime").datetime.now(__import__("datetime").UTC),
                                                                      "window_end": __import__("datetime").datetime.now(__import__("datetime").UTC), "evidence_cutoff": __import__("datetime").datetime.now(__import__("datetime").UTC), "status": "completed"})])
    detail = {"ticket": {"title": "Receipts wrong"}, "finding": {"status": "root_cause_identified", "summary": "discount uses None", "claims": []}, "resolution": None, "hypotheses": [],
              "steps": [{"tools": [{"name": "code_located_in_window", "item_ids": [sym], "coverage": {}}]}]}
    monkeypatch.setattr(P.R, "run_detail", lambda *a, **k: detail)
    prop = P.propose(driver, db, pid, run_id, api_key="k", model="m", llm=lambda *_: _out())
    assert prop.base_commit == BASE and "(pct or 0)" in prop.diff and prop.files[0]["before_hash"] == P.I.content_hash(SRC)
    # a finding that is not a root cause cannot produce a change
    detail["finding"]["status"] = "insufficient_evidence"
    with pytest.raises(P.ProposalError) as x:
        P.propose(driver, db, pid, run_id, api_key="k", model="m", llm=lambda *_: _out())
    assert x.value.code == "no_root_cause"
    # stored as an immutable PatchVersion tied to a plan; writing the same version again is a no-op, a different hash is a new node
    detail["finding"]["status"] = "root_cause_identified"
    repo.write_nodes([prepare_node("Finding", P.I.entity_id("Finding", pid, graph_run, 1), pid, {"run_id": graph_run, "sequence": 1, "status": "root_cause_identified", "summary": "s", "limitations_json": "[]"})])
    repo.write_relationships([prepare_rel("PRODUCES", "InvestigationRun", graph_run, "Finding", P.I.entity_id("Finding", pid, graph_run, 1), pid, P.I.edge_key("PRODUCES", graph_run, P.I.entity_id("Finding", pid, graph_run, 1)), {"sequence": 1})])
    a = _persist_patch(driver, db, pid, run_id, prop, 1, "row-1")
    assert _persist_patch(driver, db, pid, run_id, prop, 1, "row-1") == a
    assert repo.counts(pid)["PatchVersion"] == 1 and repo.counts(pid)["ResolutionPlan"] == 1
