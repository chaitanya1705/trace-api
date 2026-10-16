"""Vercel adapter (hosting + runtime). Fixed host api.vercel.com; read-only calls only.

Auth modes: vercel_token (scoped access token; Vercel tokens cannot be made read-only, so this always warns) and
vercel_integration (needs a provisioned Vercel integration; reported unavailable until configured).

Git commit SHAs on deployments live in the loosely typed `meta` object (githubCommitSha for GitHub-linked projects).
They are used as the Deployment->Revision mapping when present; absent => ambiguous_revision gap, never guessed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.connectors.base import ProviderError, Stage, VerifyResult, raise_for_status

API = "https://api.vercel.com"
SHA_KEYS = ("githubCommitSha", "gitlabCommitSha", "bitbucketCommitSha")


def deployment_sha(meta: dict | None) -> str | None:
    for k in SHA_KEYS:
        v = (meta or {}).get(k)
        if isinstance(v, str) and len(v) >= 7:
            return v
    return None


@dataclass
class VercelClient:
    http: httpx.AsyncClient
    token: str
    team_id: str | None = None

    async def _get(self, path: str, what: str, params: dict | None = None) -> httpx.Response:
        p = dict(params or {})
        if self.team_id:
            p["teamId"] = self.team_id
        try:
            r = await self.http.get(API + path, headers={"authorization": f"Bearer {self.token}"}, params=p)
        except httpx.HTTPError:
            raise ProviderError("provider_unavailable", f"{what}: network error", retryable=True) from None
        raise_for_status(r, what)
        return r

    async def user(self) -> dict:
        return (await self._get("/v2/user", "Sign in")).json().get("user", {})

    async def projects(self, *, max_pages: int = 5) -> tuple[list[dict], bool]:
        out: list[dict] = []
        until: Any = None
        for _ in range(max_pages):
            body = (await self._get("/v9/projects", "List projects", {"limit": 100, **({"until": until} if until else {})})).json()
            out += body.get("projects", [])
            until = (body.get("pagination") or {}).get("next")
            if not until:
                return out, False
        return out, True

    async def deployments(self, *, project_id: str | None = None, target: str | None = None, limit: int = 20,
                          since: int | None = None, until: int | None = None) -> tuple[list[dict], int | None]:
        params: dict[str, Any] = {"limit": limit}
        for k, v in (("projectId", project_id), ("target", target), ("since", since), ("until", until)):
            if v is not None:
                params[k] = v
        body = (await self._get("/v7/deployments", "Read deployments", params)).json()
        return body.get("deployments", []), (body.get("pagination") or {}).get("next")


def project_view(p: dict) -> dict[str, Any]:
    link = p.get("link") or {}
    return {
        "provider_resource_id": p["id"], "name": p["name"], "resource_type": "vercel_project",
        "framework": p.get("framework"),
        "git_repository": f"{link.get('org')}/{link.get('repo')}" if link.get("repo") else None,
        "git_provider": link.get("type"),
    }


async def verify(client: VercelClient, *, auth_method: str, config: dict) -> VerifyResult:
    st: list[Stage] = []
    eff: dict[str, Any] = {}
    if auth_method == "vercel_integration":
        return VerifyResult([Stage("method", "Auth method", "fail",
                                   "The TRACE Vercel integration is not provisioned on this deployment. Use an access token.")])
    try:
        user = await client.user()
        scope = "team" if client.team_id else "personal account"
        st.append(Stage("signin", "Sign in", "warn",
                        f"Token accepted for {user.get('username') or user.get('email') or 'user'} ({scope}). "
                        "Vercel tokens cannot be read-only: this one could also deploy and change settings. "
                        "TRACE only sends read requests.", "GET /v2/user"))
        projects, truncated = await client.projects()
        st.append(Stage("projects", "List projects", "warn" if truncated else "ok",
                        f"{len(projects)}{'+' if truncated else ''} projects visible.", "GET /v9/projects"))
        eff["project_count"] = len(projects)
        deps, _ = await client.deployments(limit=1)
        if deps:
            d = deps[0]
            has_sha = deployment_sha(d.get("meta")) is not None
            st.append(Stage("deployments", "Read deployments", "ok" if has_sha else "warn",
                            f"Latest deployment {d.get('uid')} is {d.get('readyState') or d.get('state')}."
                            + ("" if has_sha else " It carries no git commit SHA, so revision mapping would be ambiguous."),
                            "GET /v7/deployments"))
        else:
            st.append(Stage("deployments", "Read deployments", "warn", "No deployments visible to this token.", "GET /v7/deployments"))
        st.append(Stage("logs", "Runtime logs", "skip",
                        "Not checked yet. Runtime logs are collected through a Log Drain configured in the next phase; "
                        "until then TRACE sees no Vercel logs and reports that as a gap, not as healthy."))
        return VerifyResult(st, eff, {"username": user.get("username"), "scope": scope})
    except ProviderError as e:
        st.append(Stage(e.code, "Provider check", "fail", e.message))
        return VerifyResult(st, eff)
