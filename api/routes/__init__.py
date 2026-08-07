"""HTTP routers for the API service."""

from api.routes.admin import router as admin_router
from api.routes.health import router as health_router
from api.routes.jobs import router as jobs_router

__all__ = ["admin_router", "health_router", "jobs_router"]
