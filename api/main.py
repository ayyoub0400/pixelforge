"""Application factory and entrypoint for the API service.

Run with ``python -m api.main``. The Python process is PID 1 in the container
and uvicorn installs its own SIGTERM handler, so a ``kubectl delete pod`` stops
new connections being accepted and drains in-flight requests for up to
``SHUTDOWN_GRACE_SECONDS`` before exiting.
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator, Final

import structlog
import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from api.chaos import ChaosController
from api.middleware import ChaosMiddleware, ObservabilityMiddleware
from api.routes import admin_router, health_router, jobs_router
from api.service import EnqueueFailed, JobService, UploadRejected
from shared.aws import AwsClients, ReadinessProbe
from shared.config import Config, load_config
from shared.errors import ConfigError, TransientDependencyError
from shared.logging_setup import configure_logging
from shared.tracing import configure_tracing

__all__ = ["create_app", "run", "main"]

_LOG = structlog.get_logger(__name__)

#: The API listens on a fixed port; it is part of the published contract and
#: therefore not configurable. Bind on all interfaces because the process is
#: alone in its network namespace.
API_PORT: Final[int] = 8000
API_HOST: Final[str] = "0.0.0.0"  # noqa: S104 - container-local binding

SERVICE_NAME: Final[str] = "api"


def create_app(
    config: Config | None = None,
    clients: AwsClients | None = None,
    chaos: ChaosController | None = None,
) -> FastAPI:
    """Build the FastAPI application.

    Args:
        config: Process configuration. Loaded from the environment when
            omitted, which fails fast if a required variable is missing.
        clients: Pre-built AWS wrappers. Tests pass moto-backed ones; in
            production they are constructed during startup.
        chaos: Pre-built chaos controller, for tests that want to drive it
            directly.

    Returns:
        A configured application with routes, middleware and state wired up.
    """
    resolved_config = config or load_config()
    resolved_chaos = chaos or ChaosController()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Create dependencies on startup and log the drain on shutdown."""
        app.state.config = resolved_config
        app.state.chaos = resolved_chaos
        app.state.clients = clients or AwsClients.build(resolved_config)
        app.state.service = JobService(resolved_config, app.state.clients)
        app.state.probe = ReadinessProbe(app.state.clients)

        _LOG.info(
            "service_starting",
            port=API_PORT,
            chaos_enabled=resolved_config.enable_chaos_endpoint,
            **resolved_config.redacted(),
        )
        try:
            yield
        finally:
            # uvicorn has already stopped accepting connections and waited for
            # in-flight requests by the time this runs.
            _LOG.info(
                "service_stopped",
                grace_seconds=resolved_config.shutdown_grace_seconds,
            )

    app = FastAPI(
        title="pixelforge API",
        version="1.0.0",
        summary="Asynchronous image processing: upload, poll, collect thumbnails.",
        lifespan=lifespan,
    )

    # State is also set here so that tests which never enter the lifespan (for
    # example a direct call to a route function) still find their dependencies.
    app.state.config = resolved_config
    app.state.chaos = resolved_chaos

    app.include_router(jobs_router)
    app.include_router(health_router)
    if resolved_config.enable_chaos_endpoint:
        app.include_router(admin_router)
        _LOG.warning("chaos_endpoint_enabled", path="/admin/chaos")

    _register_exception_handlers(app)

    # Added innermost-first: chaos runs inside observability so injected
    # failures show up in the metrics and access logs like any other response.
    app.add_middleware(ChaosMiddleware, controller=resolved_chaos)
    app.add_middleware(ObservabilityMiddleware, fastapi_app=app)

    return app


def _register_exception_handlers(app: FastAPI) -> None:
    """Map domain exceptions onto the uniform ``{detail, code}`` error body."""

    @app.exception_handler(UploadRejected)
    async def _handle_upload_rejected(_request: Request, exc: UploadRejected) -> JSONResponse:
        _LOG.info("upload_rejected", code=exc.code, detail=exc.detail)
        return JSONResponse(
            status_code=exc.status_code, content={"detail": exc.detail, "code": exc.code}
        )

    @app.exception_handler(EnqueueFailed)
    async def _handle_enqueue_failed(_request: Request, exc: EnqueueFailed) -> JSONResponse:
        _LOG.error("enqueue_failed", error=str(exc))
        return JSONResponse(
            status_code=503,
            content={
                "detail": "job could not be queued for processing; retry",
                "code": "enqueue_failed",
            },
        )

    @app.exception_handler(TransientDependencyError)
    async def _handle_transient(
        _request: Request, exc: TransientDependencyError
    ) -> JSONResponse:
        _LOG.error("dependency_unavailable", operation=exc.operation, error=str(exc))
        return JSONResponse(
            status_code=503,
            content={
                "detail": "a required dependency is unavailable; retry",
                "code": "dependency_unavailable",
            },
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": _first_validation_message(exc), "code": "validation_error"},
        )


def _first_validation_message(exc: RequestValidationError) -> str:
    """Flatten pydantic's error list into one human-readable sentence."""
    errors = exc.errors()
    if not errors:  # pragma: no cover - defensive
        return "request validation failed"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
    message = first.get("msg", "invalid value")
    return f"{location}: {message}" if location else message


def run() -> None:
    """Load configuration, configure telemetry, and serve until SIGTERM."""
    config = load_config()
    configure_logging(SERVICE_NAME, config.log_level)
    configure_tracing(SERVICE_NAME, config.otel_exporter_otlp_endpoint)

    app = create_app(config)

    uvicorn.run(
        app,
        host=API_HOST,
        port=API_PORT,
        # Our own middleware emits a structured access log; uvicorn's would be
        # a second, differently shaped line for the same request.
        access_log=False,
        log_config=None,
        timeout_graceful_shutdown=config.shutdown_grace_seconds,
    )


def main() -> int:
    """Console entrypoint. Returns a process exit code."""
    try:
        run()
    except ConfigError as exc:
        # Logging may not be configured yet, so write plainly to stderr: a pod
        # that cannot be configured must say why in its very first line.
        print(f"FATAL: invalid configuration: {exc}", file=sys.stderr, flush=True)
        return 78  # EX_CONFIG
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
