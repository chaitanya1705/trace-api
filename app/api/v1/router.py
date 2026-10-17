from fastapi import APIRouter

from app.api.v1 import architecture, connect, connections, demo, drafts, graph, identity, investigations, log_source, logs, projects, scope, tickets, webhooks

api_router = APIRouter()
for r in (identity, projects, connections, connect, scope, drafts, tickets, webhooks, demo, graph, architecture, logs, log_source, investigations):
    api_router.include_router(r.router)
