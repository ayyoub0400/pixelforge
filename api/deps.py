"""Accessors for the objects created once during application startup.

Everything the request path needs lives on ``app.state``, which keeps the app
factory the single place where dependencies are wired and lets tests inject
moto-backed clients without patching module globals.
"""

from __future__ import annotations

from starlette.requests import Request

from api.chaos import ChaosController
from api.service import JobService
from shared.aws import ReadinessProbe
from shared.config import Config

__all__ = ["get_config", "get_service", "get_chaos", "get_probe"]


def get_config(request: Request) -> Config:
    """Return the process configuration."""
    return request.app.state.config


def get_service(request: Request) -> JobService:
    """Return the job service."""
    return request.app.state.service


def get_chaos(request: Request) -> ChaosController:
    """Return the chaos controller."""
    return request.app.state.chaos


def get_probe(request: Request) -> ReadinessProbe:
    """Return the readiness probe."""
    return request.app.state.probe
