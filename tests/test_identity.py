import uuid

import pytest

from tests.conftest import token_for

pytestmark = pytest.mark.db


async def test_ready_reports_components_without_detail(client):
    r = await client.get("/health/ready")
    assert r.json()["components"]["database"] == "ok"
    assert (await client.get("/health/live")).json() == {"status": "ok"}


async def test_unauthenticated_and_error_envelope(client):
    r = await client.get("/api/v1/me")
    assert r.status_code == 401
    body = r.json()
    assert body["error"]["code"] == "unauthenticated" and body["request_id"]
    assert r.headers["x-request-id"] == body["request_id"]


async def test_validation_error_never_echoes_input(client, org):
    h = token_for(org["owner_id"], org["owner_email"])
    r = await client.post(f"/api/v1/organizations/{org['id']}/invitations", headers=h,
                          json={"email": "not-an-email-SECRETVALUE", "role": "viewer"})
    assert r.status_code == 422 and "SECRETVALUE" not in r.text


async def test_signup_grants_no_access(client):
    stranger = uuid.uuid4()
    me = await client.get("/api/v1/me", headers=token_for(stranger, "new@example.com"))
    assert me.status_code == 200 and me.json()["data"]["memberships"] == []


async def test_invitation_flow_single_use_and_email_bound(client, org):
    owner = token_for(org["owner_id"], org["owner_email"])
    inv = await client.post(f"/api/v1/organizations/{org['id']}/invitations", headers=owner,
                            json={"email": "Ada@Example.com", "role": "investigator"})
    assert inv.status_code == 201
    token = inv.json()["data"]["token"]

    # a different signed-in email cannot use the token, and gets the same generic error
    wrong = await client.post("/api/v1/invitations/accept", json={"token": token},
                              headers=token_for(uuid.uuid4(), "mallory@example.com"))
    assert wrong.status_code == 422 and wrong.json()["error"]["message"] == "Invitation is invalid or has expired"

    ada = uuid.uuid4()
    ok = await client.post("/api/v1/invitations/accept", json={"token": token}, headers=token_for(ada, "ada@example.com"))
    assert ok.status_code == 200 and ok.json()["data"]["role"] == "investigator"
    me = await client.get("/api/v1/me", headers=token_for(ada, "ada@example.com"))
    assert me.json()["data"]["memberships"][0]["role"] == "investigator"

    again = await client.post("/api/v1/invitations/accept", json={"token": token}, headers=token_for(ada, "ada@example.com"))
    assert again.status_code == 422


async def test_admin_cannot_mint_owner_and_nonmember_sees_404(client, org, container):
    admin_id = uuid.uuid4()
    async with container.pool.connection() as conn:
        await conn.execute("INSERT INTO memberships(organization_id,user_id,email,role) VALUES (%s,%s,%s,'admin')",
                           (org["id"], admin_id, "adm@example.com"))
        await conn.commit()
    r = await client.post(f"/api/v1/organizations/{org['id']}/invitations", headers=token_for(admin_id, "adm@example.com"),
                          json={"email": "x@example.com", "role": "owner"})
    assert r.status_code == 403
    r = await client.post(f"/api/v1/organizations/{org['id']}/invitations", headers=token_for(uuid.uuid4(), "o@example.com"),
                          json={"email": "x@example.com", "role": "viewer"})
    assert r.status_code == 404


async def test_revoked_member_loses_access_even_with_valid_token(client, org, container):
    uid = uuid.uuid4()
    async with container.pool.connection() as conn:
        await conn.execute("INSERT INTO memberships(organization_id,user_id,email,role) VALUES (%s,%s,%s,'viewer')",
                           (org["id"], uid, "v@example.com"))
        await conn.commit()
    h = token_for(uid, "v@example.com")
    assert len((await client.get("/api/v1/me", headers=h)).json()["data"]["memberships"]) == 1
    async with container.pool.connection() as conn:
        await conn.execute("UPDATE memberships SET status='revoked', revoked_at=now() WHERE user_id=%s", (uid,))
        await conn.commit()
    assert (await client.get("/api/v1/me", headers=h)).json()["data"]["memberships"] == []


async def test_audit_is_append_only(container, org):
    async with container.pool.connection() as conn:
        await conn.execute("INSERT INTO audit_events(organization_id, action, result) VALUES (%s,'t','success')", (org["id"],))
        await conn.commit()
        with pytest.raises(Exception, match="append-only"):
            await conn.execute("DELETE FROM audit_events WHERE organization_id=%s", (org["id"],))
