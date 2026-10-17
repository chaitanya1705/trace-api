from fastapi import APIRouter

from app.api.v1 import architecture, bot, connect, connections, demo, drafts, graph, graph_explore, identity, investigations, log_source, logs, projects, scope, tickets, webhooks

api_router = APIRouter()
for r in (identity, projects, connections, connect, scope, drafts, tickets, webhooks, demo, graph, graph_explore, architecture, logs, log_source, investigations, bot):
    api_router.include_router(r.router)
