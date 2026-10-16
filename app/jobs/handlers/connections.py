"""connection.verify: real staged provider checks, then atomic state change (R-016 lifecycle)."""
from __future__ import annotations

import json
from uuid import UUID

from app.audit import audit
from app.connectors import github as gh
from app.connectors import vercel as vc
from app.domain.connections import service as svc
from app.jobs.queue import PermanentJobError
from app.jobs.worker import JobContext, handler


@handler("connection.verify")
async def verify_connection(ctx: JobContext) -> dict:
    c = ctx.container
    cid = UUID(ctx.job["payload"]["connection_id"])
    cred_ver = ctx.job["payload"].get("credential_version")
    async with c.pool.connection() as conn:
        row = await (await conn.execute("SELECT * FROM connections WHERE id=%s", (cid,))).fetchone()
        if row is None or row["status"] == "disconnected":
            raise PermanentJobError("connection_gone", "Connection was removed or disconnected")
        if cred_ver is None and row["auth_method"] in svc.SECRET_METHODS:
            r = await (await conn.execute(
                "SELECT version FROM credential_versions WHERE connection_id=%s AND status IN ('active','pending')"
                " ORDER BY (status='active') DESC, version DESC LIMIT 1", (cid,))).fetchone()
            cred_ver = r["version"] if r else None
        rotating = cred_ver is not None and row["active_credential_version"] not in (None, cred_ver)
        if not rotating:
            await conn.execute("UPDATE connections SET status='verifying' WHERE id=%s", (cid,))
        check = await (await conn.execute(
            "INSERT INTO connection_checks(organization_id,connection_id,outcome) VALUES (%s,%s,'running') RETURNING id",
            (row["organization_id"], cid))).fetchone()
        await conn.commit()
        secret = await svc.active_secret(conn, c.vault, row, cred_ver)

    await ctx.progress("verifying")
    client, http = svc.build_adapter(c, row, secret)
    try:
        if row["provider"] == "github":
            result = await gh.verify(client, auth_method=row["auth_method"], config=row["config"], secret=secret)
        else:
            result = await vc.verify(client, auth_method=row["auth_method"], config=row["config"])
    finally:
        await http.aclose()
    await ctx.check_cancel()

    outcome = result.outcome
    stages = json.dumps([s.as_dict() for s in result.stages])
    failure = result.first_failure
    err = json.dumps({"code": failure.key, "message": failure.message}) if failure else None
    async with c.pool.connection() as conn:
        await conn.execute("UPDATE connection_checks SET outcome=%s, stages=%s::jsonb, finished_at=now() WHERE id=%s",
                           ({"connected": "ok", "connected_with_warnings": "warnings", "failed": "failed"}[outcome], stages, check["id"]))
        current = await (await conn.execute("SELECT status, config FROM connections WHERE id=%s FOR UPDATE", (cid,))).fetchone()
        if current["status"] == "disconnected":  # disconnected while we were calling the provider: change nothing
            await conn.commit()
            raise PermanentJobError("connection_gone", "Connection was disconnected during verification")
        if rotating:
            if outcome == "failed":
                # Preserve the working credential; only the pending replacement is discarded (R-016).
                await conn.execute("UPDATE credential_versions SET status='revoked' WHERE connection_id=%s AND version=%s",
                                   (cid, cred_ver))
            else:
                await conn.execute("UPDATE credential_versions SET status='revoked' WHERE connection_id=%s AND status='active'", (cid,))
                await conn.execute("UPDATE credential_versions SET status='active' WHERE connection_id=%s AND version=%s", (cid, cred_ver))
                await conn.execute(
                    "UPDATE connections SET active_credential_version=%s, status=%s, last_error=NULL, last_verified_at=now(),"
                    " effective_scopes=%s::jsonb, credential_expires_at=%s, config=config || %s::jsonb WHERE id=%s",
                    (cred_ver, outcome, json.dumps(result.effective_scopes), result.expires_at, json.dumps(result.config_updates), cid))
        else:
            if cred_ver is not None and outcome != "failed":
                await conn.execute("UPDATE credential_versions SET status='active' WHERE connection_id=%s AND version=%s", (cid, cred_ver))
            await conn.execute(
                "UPDATE connections SET status=%s, last_error=%s::jsonb, last_verified_at=now(), effective_scopes=%s::jsonb,"
                " credential_expires_at=%s, config=config || %s::jsonb,"
                " active_credential_version=CASE WHEN %s THEN %s ELSE active_credential_version END WHERE id=%s",
                (outcome, err, json.dumps(result.effective_scopes), result.expires_at, json.dumps(result.config_updates),
                 cred_ver is not None and outcome != "failed", cred_ver, cid))
        await audit(conn, organization_id=row["organization_id"], project_id=row["project_id"], actor=ctx.job["requested_by"],
                    action="connection.rotate_verify" if rotating else "connection.verify",
                    result="failure" if outcome == "failed" else "success", target_type="connection", target_id=cid,
                    details={"outcome": outcome, "provider": row["provider"]})
        await conn.commit()
    return {"outcome": outcome, "check_id": str(check["id"])}
