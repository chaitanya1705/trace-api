from __future__ import annotations

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_SKEY = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
ProjectRole = Literal["admin", "approver", "investigator", "viewer"]
ReadinessState = Literal["ok", "warning", "todo", "skipped", "failed"]


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:32] or "env"


class EnvironmentIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    key: str | None = None

    @field_validator("key")
    @classmethod
    def _k(cls, v):
        if v is not None and not _KEY.match(v):
            raise ValueError("key must be lowercase letters, digits, - or _ (max 32)")
        return v


class ServiceIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    key: str | None = None

    @field_validator("key")
    @classmethod
    def _k(cls, v):
        if v is not None and not _SKEY.match(v):
            raise ValueError("key must be lowercase letters, digits, - _ . (max 64)")
        return v


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(None, max_length=500)
    environments: list[EnvironmentIn] = Field(default_factory=lambda: [EnvironmentIn(name="production")], min_length=1, max_length=8)
    services: list[ServiceIn] = Field(default_factory=list, max_length=100)
    owner_user_id: UUID | None = None  # defaults to the creator


class ProjectPatch(BaseModel):
    version: int
    name: str | None = Field(None, min_length=1, max_length=80)
    description: str | None = Field(None, max_length=500)
    owner_user_id: UUID | None = None


class EnvironmentOut(BaseModel):
    id: UUID
    name: str
    key: str


class ServiceOut(BaseModel):
    id: UUID
    name: str
    key: str


class ReadinessItem(BaseModel):
    key: str
    label: str
    state: ReadinessState
    detail: str | None = None


class ConnectionBrief(BaseModel):
    id: UUID
    provider: str
    kind: str
    status: str
    last_verified_at: datetime | None = None


class ProjectSummary(BaseModel):
    id: UUID
    name: str
    description: str | None
    owner_user_id: UUID
    environments: list[EnvironmentOut]
    connections: list[ConnectionBrief]
    readiness: list[ReadinessItem]
    updated_at: datetime
    version: int


class MemberIn(BaseModel):
    user_id: UUID
    role: ProjectRole
    environment_ids: list[UUID] | None = None


class MembersPut(BaseModel):
    members: list[MemberIn] = Field(max_length=200)


class EnvironmentsPut(BaseModel):
    environments: list[EnvironmentIn] = Field(min_length=1, max_length=8)


class ServicesPut(BaseModel):
    services: list[ServiceIn] = Field(max_length=100)


class ProjectDetail(ProjectSummary):
    services: list[ServiceOut]
    capabilities: list[str]


class MemberOut(BaseModel):
    user_id: UUID
    role: str
    environment_ids: list[UUID] | None
    email: str
    display_name: str | None
    member_status: str
