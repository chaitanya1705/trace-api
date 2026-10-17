"""TRACE Bot: the single chat window. It reads the evidence graph (Neo4j) as its primary source; Postgres is used only to
decide which projects the caller may read. Conversation history is kept by the client and sent with each turn."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from psycopg import AsyncConnection
from pydantic import BaseModel, Field

from app.api.v1.deps_graph import neo4j_driver
from app.auth.deps import OrgContext, get_conn, org_context
from app.bot.agent import chat
from app.envelope import wrap
from app.errors import DependencyUnavailable, NotFound

router = APIRouter(tags=["bot"])


class Turn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=4000)


class Focus(BaseModel):
    kind: str = Field(pattern="^(investigation|ticket|service)$")
    id: str = Field(max_length=120, pattern=r"^[A-Za-z0-9_:.\-]+$")


class ChatIn(BaseModel):
    focus: Focus | None = None
    message: str = Field(min_length=2, max_length=1000)
    project_id: UUID | None = None
    history: list[Turn] = Field(default_factory=list, max_length=20)


@router.post("/bot/chat")
async def bot_chat(body: ChatIn, request: Request, org: OrgContext = Depends(org_context), conn: AsyncConnection = Depends(get_conn),
                   driver=Depends(neo4j_driver)):
    s = request.app.state.container.settings
    if not s.openai_api_key:
        raise DependencyUnavailable("The language model is not configured")
    rows = await (await conn.execute(
        "SELECT p.id, p.name FROM projects p WHERE p.organization_id = %s AND p.status = 'active' "
        "AND EXISTS (SELECT 1 FROM project_memberships m WHERE m.project_id = p.id AND m.user_id = %s)",
        (org.organization_id, org.principal.user_id))).fetchall()
    names = {str(r["id"]): r["name"] for r in rows}
    if body.project_id and str(body.project_id) not in names:
        raise NotFound("Project not found")
    pids = [str(body.project_id)] if body.project_id else list(names)
    label = names[str(body.project_id)] if body.project_id else "all projects"
    focus = body.focus.model_dump() if body.focus else None
    if focus and focus["kind"] == "investigation" and body.project_id and not focus["id"].startswith("run_"):
        from app.graph import identity as I  # the web knows the control-plane run id; the graph key is derived from it

        focus["id"] = I.entity_id("InvestigationRun", str(body.project_id), focus["id"])
    out = await asyncio.to_thread(chat, driver[0], driver[1], project_ids=pids, scope_label=label, message=body.message,
                                  history=[t.model_dump() for t in body.history], focus=focus, api_key=s.openai_api_key, model=s.trace_model)
    return wrap(request, {**out, "scope": label, "as_of": datetime.now(UTC).isoformat()})
