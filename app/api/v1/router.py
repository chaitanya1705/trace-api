from fastapi import APIRouter

from app.api.v1 import identity

api_router = APIRouter()
api_router.include_router(identity.router)
