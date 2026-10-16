"""Development-only sign-in. Mounted solely when AUTH_MODE=local and TRACE_ENV != production."""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, EmailStr

from app.auth.deps import get_container
from app.auth.local import issue_token

router = APIRouter(prefix="/dev", tags=["dev-only"])


class DevLogin(BaseModel):
    email: EmailStr
    name: str | None = None


@router.post("/token")
async def dev_token(body: DevLogin, request: Request):
    c = await get_container(request)
    return {"access_token": issue_token(c.settings.local_auth_secret, body.email, body.name), "token_type": "bearer"}
