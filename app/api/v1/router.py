from fastapi import APIRouter

from app.api.v1 import identity, projects

api_router = APIRouter()
api_router.include_router(identity.router)
api_router.include_router(projects.router)
