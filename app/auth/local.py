"""Local development identity (no Supabase). NEVER usable in production: config validation rejects it and the
token endpoint is not mounted. Same Principal/authorization path as production; only the token issuer differs.
"""
from __future__ import annotations

import time
import uuid

import jwt

from app.auth.jwt import Principal
from app.errors import Unauthenticated

ISSUER = "trace-local"
AUDIENCE = "trace-local"
_NS = uuid.UUID("6f1f3a52-5b54-4d0e-9a7e-1c2f3d4b5a69")


def user_id_for(email: str) -> uuid.UUID:
    """Stable id per email so repeated dev logins and the bootstrap CLI agree."""
    return uuid.uuid5(_NS, email.strip().lower())


def issue_token(secret: str, email: str, name: str | None = None, ttl_seconds: int = 8 * 3600) -> str:
    now = int(time.time())
    claims = {"sub": str(user_id_for(email)), "email": email.strip().lower(), "iss": ISSUER, "aud": AUDIENCE,
              "iat": now, "exp": now + ttl_seconds, "user_metadata": {"full_name": name} if name else {}}
    return jwt.encode(claims, secret, algorithm="HS256")


class LocalTokenVerifier:
    def __init__(self, secret: str):
        self._secret = secret

    async def verify(self, token: str) -> Principal:
        try:
            c = jwt.decode(token, self._secret, algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER,
                           options={"require": ["exp", "sub"]})
        except jwt.ExpiredSignatureError:
            raise Unauthenticated("Session expired", code="session_expired") from None
        except jwt.PyJWTError:
            raise Unauthenticated("Invalid credentials") from None
        return Principal(uuid.UUID(c["sub"]), c.get("email"), (c.get("user_metadata") or {}).get("full_name"))
