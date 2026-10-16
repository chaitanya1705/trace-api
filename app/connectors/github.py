"""GitHub read-plane adapter (P1). Two auth modes:

* github_app  - preferred. Installation id in connection.config; short-lived installation tokens minted per call from
                the *read* app's private key (env). The remediation app (P3) is a different identity and is never
                reachable from this module.
* github_pat  - trial mode only: fine-grained token stored in the vault. Tied to a person, may expire.

Host is fixed to api.github.com (GitHub Enterprise Server is visibly gated until tested; no arbitrary URL fetch).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from app.connectors.base import ProviderError, Stage, VerifyResult, raise_for_status

API = "https://api.github.com"
API_VERSION = "2022-11-28"
EXPECTED_READ = {"metadata": "read", "contents": "read", "pull_requests": "read"}
_RANK = {"read": 1, "write": 2, "admin": 3}


def _norm_pem(pem: str) -> str:
    return pem.replace("\\n", "\n").strip()


def app_jwt(app_id: str, private_key_pem: str, now: float | None = None) -> str:
    t = int(now or time.time())
    return jwt.encode({"iat": t - 60, "exp": t + 540, "iss": str(app_id)}, _norm_pem(private_key_pem), algorithm="RS256")


def compare_permissions(granted: dict[str, str]) -> tuple[list[str], list[str]]:
    """(missing, excess). Excess = anything beyond the read set, or a higher level than needed - shown, not hidden."""
    missing = [k for k, lvl in EXPECTED_READ.items() if _RANK.get(granted.get(k, ""), 0) < _RANK[lvl]]
    excess = [f"{k}:{v}" for k, v in sorted(granted.items())
              if k not in EXPECTED_READ or _RANK.get(v, 0) > _RANK[EXPECTED_READ[k]]]
    return missing, excess


@dataclass
class GitHubClient:
    http: httpx.AsyncClient
    app_id: str | None = None
    private_key: str | None = None

    def _hdr(self, token: str) -> dict[str, str]:
        return {"authorization": f"Bearer {token}", "accept": "application/vnd.github+json", "x-github-api-version": API_VERSION}

    async def _get(self, path: str, token: str, what: str, params: dict | None = None) -> httpx.Response:
        try:
            r = await self.http.get(API + path, headers=self._hdr(token), params=params)
        except httpx.HTTPError:
            raise ProviderError("provider_unavailable", f"{what}: network error", retryable=True) from None
        raise_for_status(r, what)
        return r

    # -- app mode -------------------------------------------------------------------------------------------------
    def _need_app(self) -> tuple[str, str]:
        if not (self.app_id and self.private_key):
            raise ProviderError("not_configured", "GitHub App is not configured on this TRACE deployment")
        return self.app_id, self.private_key

    async def installation(self, installation_id: int | str) -> dict[str, Any]:
        a, k = self._need_app()
        return (await self._get(f"/app/installations/{installation_id}", app_jwt(a, k), "Find installation")).json()

    async def installation_token(self, installation_id: int | str) -> tuple[str, str | None]:
        a, k = self._need_app()
        try:
            r = await self.http.post(f"{API}/app/installations/{installation_id}/access_tokens", headers=self._hdr(app_jwt(a, k)))
        except httpx.HTTPError:
            raise ProviderError("provider_unavailable", "Mint installation token: network error", retryable=True) from None
        raise_for_status(r, "Mint installation token")
        body = r.json()
        return body["token"], body.get("expires_at")

    # -- shared ---------------------------------------------------------------------------------------------------
    async def list_installation_repos(self, token: str, *, max_pages: int = 5) -> tuple[list[dict], int, bool]:
        """(repos, total_count, truncated). Bounded: never walks an unbounded org."""
        out: list[dict] = []
        total = 0
        for page in range(1, max_pages + 1):
            body = (await self._get("/installation/repositories", token, "List repositories",
                                    {"per_page": 100, "page": page})).json()
            total = body.get("total_count", total)
            out += body.get("repositories", [])
            if len(out) >= total or not body.get("repositories"):
                break
        return out, total, len(out) < total

    async def list_user_repos(self, token: str, *, max_pages: int = 5) -> tuple[list[dict], bool]:
        out: list[dict] = []
        for page in range(1, max_pages + 1):
            r = await self._get("/user/repos", token, "List repositories",
                                {"per_page": 100, "page": page, "sort": "pushed"})
            batch = r.json()
            out += batch
            if len(batch) < 100:
                return out, False
        return out, True


def repo_view(r: dict) -> dict[str, Any]:
    return {
        "provider_repository_id": str(r["id"]), "full_name": r["full_name"], "default_branch": r.get("default_branch") or "main",
        "private": bool(r.get("private")), "archived": bool(r.get("archived")), "language": r.get("language"),
        "pushed_at": r.get("pushed_at"),
    }


async def verify(client: GitHubClient, *, auth_method: str, config: dict, secret: str | None) -> VerifyResult:
    """Stage-by-stage real checks. Stops at the first failure so the reason is actionable."""
    st: list[Stage] = []
    eff: dict[str, Any] = {}
    try:
        if auth_method == "github_app":
            iid = config.get("installation_id")
            if not iid:
                return VerifyResult([Stage("installation", "Find the installation", "fail",
                                           "No installation id yet. Install the TRACE GitHub App first.")])
            inst = await client.installation(iid)
            suspended = inst.get("suspended_at")
            acct = (inst.get("account") or {}).get("login", "?")
            st.append(Stage("installation", "Find the installation", "fail" if suspended else "ok",
                            f"Suspended since {suspended}." if suspended else f"Active on {acct}.",
                            f"GET /app/installations/{iid}"))
            if suspended:
                return VerifyResult(st)
            perms = inst.get("permissions") or {}
            missing, excess = compare_permissions(perms)
            eff["permissions"] = perms
            eff["repository_selection"] = inst.get("repository_selection")
            if missing:
                st.append(Stage("permissions", "Compare permissions", "fail",
                                "Missing: " + ", ".join(f"{m}:{EXPECTED_READ[m]}" for m in missing), "installation.permissions"))
                return VerifyResult(st, eff)
            st.append(Stage("permissions", "Compare permissions", "warn" if excess else "ok",
                            ("Broader than TRACE needs: " + ", ".join(excess)) if excess
                            else "metadata, contents and pull_requests: read. Nothing more.", "installation.permissions"))
            token, _ = await client.installation_token(iid)
            repos, total, truncated = await client.list_installation_repos(token)
            archived = sum(1 for r in repos if r.get("archived"))
            st.append(Stage("repositories", "List repositories", "warn" if truncated else "ok",
                            f"{total} repositories visible ({archived} archived)."
                            + (" Showing the first 500 only." if truncated else ""), "GET /installation/repositories"))
            eff["repository_count"] = total
            return VerifyResult(st, eff, {"account": acct, "repository_selection": inst.get("repository_selection")})

        if auth_method == "github_pat":
            if not secret:
                return VerifyResult([Stage("token", "Check the token", "fail", "No token stored.")])
            r = await client._get("/user", secret, "Check the token")
            login = r.json().get("login", "?")
            classic = r.headers.get("x-oauth-scopes")
            exp = r.headers.get("github-authentication-token-expiration")
            st.append(Stage("token", "Check the token", "warn",
                            f"Signed in as {login}. Trial mode: tied to one person"
                            + ("" if exp else " and expiry is unknown") + ".", "GET /user"))
            if classic is not None:
                eff["classic_scopes"] = [s.strip() for s in classic.split(",") if s.strip()]
                if "repo" in eff["classic_scopes"]:
                    st.append(Stage("scopes", "Check effective access", "warn",
                                    "Classic 'repo' scope can write. TRACE only reads, but the token could.", "x-oauth-scopes"))
            repos, truncated = await client.list_user_repos(secret)
            st.append(Stage("repositories", "List repositories", "warn" if truncated else "ok",
                            f"{len(repos)}{'+' if truncated else ''} repositories visible.", "GET /user/repos"))
            from datetime import datetime
            exp_dt = None
            if exp:
                try:
                    exp_dt = datetime.strptime(exp.rsplit(" ", 1)[0], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    exp_dt = None
            return VerifyResult(st, eff, {"login": login}, expires_at=exp_dt)
        return VerifyResult([Stage("method", "Auth method", "fail", f"Unsupported auth method: {auth_method}")])
    except ProviderError as e:
        st.append(Stage(e.code, "Provider check", "fail", e.message))
        return VerifyResult(st, eff)
