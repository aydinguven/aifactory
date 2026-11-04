"""Admin API (/api/admin), one sub-router per administrative area."""

from fastapi import APIRouter

from app.routes.admin import (
    catalog,
    downloads,
    images,
    integrations,
    kubernetes,
    network,
    nodes,
    skills,
    system,
    users,
    workspaces,
)

admin_router = APIRouter(prefix="/api/admin", tags=["Admin"])
for _module in (
    system,
    images,
    catalog,
    network,
    nodes,
    integrations,
    skills,
    kubernetes,
    users,
    workspaces,
    downloads,
):
    admin_router.include_router(_module.router)

__all__ = ["admin_router"]
