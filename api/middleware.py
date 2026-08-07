"""ASGI middleware: request metrics, structured access logs, chaos injection.

Two middlewares are installed. Observability is the outermost so that a chaos
injected 500 is counted and logged exactly like a real one — the point of the
chaos endpoint is to make dashboards move.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Final

import structlog
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match, Route

from api.chaos import ChaosController
from shared.metrics import HTTP_REQUEST_DURATION, HTTP_REQUESTS_TOTAL
from shared.tracing import current_span_ids

__all__ = ["ObservabilityMiddleware", "ChaosMiddleware", "resolve_endpoint"]

_LOG = structlog.get_logger("api.access")

#: Label used for requests that matched no route. Bounding this keeps a scanner
#: hammering random paths from exploding metric cardinality.
_UNMATCHED: Final[str] = "unmatched"

#: Chaos latency and error injection apply only to the job API. Health,
#: readiness and metrics endpoints stay honest so that the *effect* of chaos
#: remains observable while it is switched on.
_CHAOS_PATH_PREFIX: Final[str] = "/api/v1"


def resolve_endpoint(app: FastAPI, request: Request) -> str:
    """Return the route *template* for a request, e.g. ``/api/v1/jobs/{job_id}``.

    Using the template rather than the concrete path keeps one time series per
    endpoint instead of one per job id.
    """
    for route in app.router.routes:
        if not isinstance(route, Route):
            continue
        match, _ = route.matches(request.scope)
        if match is not Match.NONE:
            return route.path
    return _UNMATCHED


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Record ``pixelforge_http_*`` metrics and emit one access log per request."""

    def __init__(self, app: FastAPI, fastapi_app: FastAPI) -> None:
        super().__init__(app)
        self._fastapi_app = fastapi_app

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        endpoint = resolve_endpoint(self._fastapi_app, request)
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception:
            # Starlette's error middleware turns this into a 500; we still want
            # the metric and the log line to reflect what happened.
            _LOG.exception("request_failed", method=request.method, endpoint=endpoint)
            raise
        finally:
            elapsed = time.perf_counter() - started
            HTTP_REQUEST_DURATION.labels(endpoint=endpoint).observe(elapsed)
            HTTP_REQUESTS_TOTAL.labels(
                method=request.method, endpoint=endpoint, status=str(status_code)
            ).inc()
            trace_id, _ = current_span_ids()
            _LOG.info(
                "http_request",
                method=request.method,
                endpoint=endpoint,
                path=request.url.path,
                status=status_code,
                duration_ms=round(elapsed * 1000, 2),
                trace_id=trace_id,
            )


class ChaosMiddleware(BaseHTTPMiddleware):
    """Apply the injected latency and error rate to ``/api/v1`` requests."""

    def __init__(self, app: FastAPI, controller: ChaosController) -> None:
        super().__init__(app)
        self._controller = controller

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not request.url.path.startswith(_CHAOS_PATH_PREFIX):
            return await call_next(request)

        latency = self._controller.latency_seconds
        if latency > 0:
            await asyncio.sleep(latency)

        if self._controller.should_fail_request():
            _LOG.warning("chaos_injected_error", path=request.url.path)
            return JSONResponse(
                status_code=500,
                content={
                    "detail": "chaos: injected failure",
                    "code": "chaos_injected_error",
                },
            )

        return await call_next(request)
