"""Read-only source retrieval from GitHub: resolve a ref to an exact commit, then download that commit's archive.

Uses the same read credentials as the rest of the GitHub connector. Never clones, never executes anything, never writes.
"""
from __future__ import annotations

import re

import httpx

from app.connectors.base import ProviderError, raise_for_status
from app.connectors.github import API, GitHubClient
from app.graph.archive import MAX_ARCHIVE_BYTES

_SHA = re.compile(r"^[0-9a-f]{40}$")


async def resolve_commit(client: GitHubClient, token: str, full_name: str, ref: str) -> str:
    """Exact 40-char commit SHA for a branch, tag or commit. Raises ProviderError if it does not exist."""
    r = await client._get(f"/repos/{full_name}/commits/{ref}", token, f"Resolve {ref}")
    sha = (r.json() or {}).get("sha", "")
    if not _SHA.match(sha):
        raise ProviderError("unexpected_response", "GitHub did not return a commit SHA")
    return sha


async def download_archive(client: GitHubClient, token: str, full_name: str, sha: str, dest_path: str) -> int:
    """Stream the commit's .tar.gz to `dest_path` with a hard size cap. Returns bytes written.

    GitHub answers with a redirect to a signed download URL; the credential is sent only to api.github.com and is NOT
    forwarded to the redirect target.
    """
    if not _SHA.match(sha):
        raise ValueError("an exact commit sha is required")
    try:
        first = await client.http.get(f"{API}/repos/{full_name}/tarball/{sha}", headers=client._hdr(token))
    except httpx.HTTPError:
        raise ProviderError("provider_unavailable", "Download source: network error", retryable=True) from None
    if first.status_code in (301, 302, 303, 307, 308):
        url = first.headers.get("location", "")
        if not url.startswith("https://"):
            raise ProviderError("unexpected_response", "GitHub returned an unusable download location")
        headers: dict[str, str] = {}
    else:
        raise_for_status(first, "Download source")
        raise ProviderError("unexpected_response", "GitHub did not redirect to the archive")
    written = 0
    try:
        async with client.http.stream("GET", url, headers=headers) as resp:
            raise_for_status(resp, "Download source")
            with open(dest_path, "wb") as f:
                async for chunk in resp.aiter_bytes(1 << 16):
                    written += len(chunk)
                    if written > MAX_ARCHIVE_BYTES:
                        raise ProviderError("archive_too_large", "Repository archive exceeds the size limit")
                    f.write(chunk)
    except httpx.HTTPError:
        raise ProviderError("provider_unavailable", "Download source: network error", retryable=True) from None
    return written
