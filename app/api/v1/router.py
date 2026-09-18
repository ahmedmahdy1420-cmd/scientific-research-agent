"""v1 API router assembly."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import agent, auth, documents, evaluation, health, research, search, users

api_router = APIRouter()

# Ops probes first; they are unauthenticated by design.
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(agent.router)
api_router.include_router(documents.router)
api_router.include_router(search.router)
api_router.include_router(research.router)
api_router.include_router(evaluation.router)

__all__ = ["api_router"]
