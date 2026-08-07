"""Request metrics and structured access logging.

Chaos injection lives in :mod:`api.chaos` as a router dependency rather than
here, so that an injected failure is attributed to the endpoint it hit instead
of to a catch-all series.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Final

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from shared.metrics import HTTP_REQUEST_DURATION, HTTP_REQUESTS_TOTAL
from shared.tracing import current_span_ids

__all__ = ["ObservabilityMiddleware", "resolve_endpoint"]

_LOG = structlog.get_logger("api.access")

#: Label used for requests that matched no route. Bounding this keeps a scanner
#: hammering random paths from exploding metric cardinality.
_UNMATCHED: Final[str] = "unmatched"


def resolve_endpoint(request: Request) -> str:
    """Return the route *template* for a request, e.g. ``/api/v1/jobs/{job_id}``.

    Using the template rather than the concrete path keeps one time series per
    endpoint instead of one per job id, which is the difference between a
    dashboard and an out-of-memory Prometheus.

    Starlette records the matched route on the scope during routing, so this
    must be called *after* the request has been dispatched. Anything that
    matched no route - a scanner probing random paths - collapses onto a single
    ``unmatched`` series.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else _UNMATCHED


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Record ``pixelforge_http_*`` metrics and emit one access log per request."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception:
            # Starlette's error middleware turns this into a 500; we still want
            # the metric and the log line to reflect what happened.
            _LOG.exception("request_failed", method=request.method, path=request.url.path)
            raise
        finally:
            # Resolved here rather than up front: the matched route is only on
            # the scope once routing has run.
            endpoint = resolve_endpoint(request)
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
