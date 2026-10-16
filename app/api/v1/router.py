from fastapi import APIRouter

from app.api.v1 import connections, drafts, identity, projects, scope, webhooks

api_router = APIRouter()
for r in (identity, projects, connections, scope, drafts, webhooks):
    api_router.include_router(r.router)
