"""Supabase JWT verification (A11): signature, issuer, audience and expiry are checked server-side.

Identity only. Authorization (membership, project grants) is looked up per command in Postgres, so a revoked
member loses access immediately even while their JWT is still valid (R-001 acceptance).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import jwt
from jwt import PyJWKClient

from app.errors import DependencyUnavailable, Unauthenticated


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    email: str | None
    display_name: str | None = None


class TokenVerifier(Protocol):
    async def verify(self, token: str) -> Principal: ...


class SupabaseTokenVerifier:
    ALGORITHMS = ["RS256", "ES256"]  # asymmetric signing keys only; no shared-secret HS256 path

    def __init__(self, *, jwks_url: str, issuer: str | None, audience: str):
        self._jwks = PyJWKClient(jwks_url, cache_keys=True, lifespan=600, timeout=5)
        self._issuer = issuer
        self._audience = audience

    async def verify(self, token: str) -> Principal:
        try:
            # PyJWKClient does blocking HTTP on a cache miss, so keep it off the event loop.
            key = await asyncio.to_thread(self._jwks.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=self.ALGORITHMS,
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "sub"], "verify_iss": self._issuer is not None},
            )
        except jwt.PyJWKClientConnectionError:
            raise DependencyUnavailable("Identity provider keys are unavailable") from None
        except jwt.ExpiredSignatureError:
            raise Unauthenticated("Session expired", code="session_expired") from None
        except jwt.PyJWTError:
            raise Unauthenticated("Invalid credentials") from None
        try:
            uid = UUID(str(claims["sub"]))
        except ValueError:
            raise Unauthenticated("Invalid credentials") from None
        meta = claims.get("user_metadata") or {}
        return Principal(uid, claims.get("email"), meta.get("full_name") or meta.get("name"))
