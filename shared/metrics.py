"""Prometheus metrics.

Metric names are part of the published contract: dashboards, alerts and the
worker's autoscaling rule are all built against the exact names below. Renaming
one is a breaking change and must go through the CONTRACT section of the
README.

Each service has its own registry, so an API scrape never reports worker series
and vice versa. The one metric both services can emit is
``pixelforge_sqs_errors_total``: the API sends messages and the worker consumes
them, and a send failure is just as worth alerting on.

Standard Python runtime collectors (``process_*``, ``python_gc_*``) are
registered on both registries so the usual RSS and GC dashboards keep working.
"""

from __future__ import annotations

import contextlib
from typing import Final

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    GC_COLLECTOR,
    PLATFORM_COLLECTOR,
    PROCESS_COLLECTOR,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

__all__ = [
    "API_REGISTRY",
    "HTTP_REQUESTS_TOTAL",
    "HTTP_REQUEST_DURATION",
    "JOBS_INFLIGHT",
    "JOBS_PROCESSED_TOTAL",
    "JOB_DURATION_SECONDS",
    "JOB_STAGE_DURATION_SECONDS",
    "METRICS_CONTENT_TYPE",
    "SQS_ERRORS_TOTAL",
    "UPLOADS_TOTAL",
    "UPLOAD_SIZE_BYTES",
    "WORKER_REGISTRY",
    "Stage",
    "UploadResult",
    "render_metrics",
]

METRICS_CONTENT_TYPE: Final[str] = CONTENT_TYPE_LATEST

#: Served by the API on ``GET /metrics`` (port 8000).
API_REGISTRY: Final[CollectorRegistry] = CollectorRegistry(auto_describe=True)

#: Served by the worker on port 9090.
WORKER_REGISTRY: Final[CollectorRegistry] = CollectorRegistry(auto_describe=True)


def _register_runtime_collectors(registry: CollectorRegistry) -> None:
    """Attach the interpreter/process collectors to a registry."""
    for collector in (PROCESS_COLLECTOR, PLATFORM_COLLECTOR, GC_COLLECTOR):
        # Already registered is fine: both registries want the same collectors.
        with contextlib.suppress(ValueError):
            registry.register(collector)


_register_runtime_collectors(API_REGISTRY)
_register_runtime_collectors(WORKER_REGISTRY)

# --------------------------------------------------------------------------
# API metrics
# --------------------------------------------------------------------------

HTTP_REQUESTS_TOTAL: Final[Counter] = Counter(
    "pixelforge_http_requests_total",
    "Total HTTP requests handled by the API.",
    labelnames=("method", "endpoint", "status"),
    registry=API_REGISTRY,
)

HTTP_REQUEST_DURATION: Final[Histogram] = Histogram(
    "pixelforge_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    labelnames=("endpoint",),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=API_REGISTRY,
)

UPLOADS_TOTAL: Final[Counter] = Counter(
    "pixelforge_uploads_total",
    "Upload attempts by outcome.",
    labelnames=("result",),
    registry=API_REGISTRY,
)

UPLOAD_SIZE_BYTES: Final[Histogram] = Histogram(
    "pixelforge_upload_size_bytes",
    "Size of accepted uploads in bytes.",
    buckets=(
        10_000,
        50_000,
        100_000,
        250_000,
        500_000,
        1_000_000,
        2_500_000,
        5_000_000,
        10_000_000,
        10_485_760,
    ),
    registry=API_REGISTRY,
)

# --------------------------------------------------------------------------
# Worker metrics
# --------------------------------------------------------------------------

JOBS_PROCESSED_TOTAL: Final[Counter] = Counter(
    "pixelforge_jobs_processed_total",
    "Jobs that reached a terminal state.",
    labelnames=("status",),
    registry=WORKER_REGISTRY,
)

JOB_DURATION_SECONDS: Final[Histogram] = Histogram(
    "pixelforge_job_duration_seconds",
    "End-to-end job processing time in the worker, in seconds.",
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
    registry=WORKER_REGISTRY,
)

JOBS_INFLIGHT: Final[Gauge] = Gauge(
    "pixelforge_jobs_inflight",
    "Jobs currently being processed by this worker instance.",
    registry=WORKER_REGISTRY,
)

JOB_STAGE_DURATION_SECONDS: Final[Histogram] = Histogram(
    "pixelforge_job_stage_duration_seconds",
    "Per-stage processing time in seconds.",
    labelnames=("stage",),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
    registry=WORKER_REGISTRY,
)

# Emitted by whichever service made the failing call, so it is registered on
# both: the worker receives and deletes, the API sends.
SQS_ERRORS_TOTAL: Final[Counter] = Counter(
    "pixelforge_sqs_errors_total",
    "SQS API call failures by operation.",
    labelnames=("operation",),
    registry=WORKER_REGISTRY,
)
API_REGISTRY.register(SQS_ERRORS_TOTAL)


class UploadResult:
    """Allowed values for the ``result`` label on ``pixelforge_uploads_total``."""

    ACCEPTED: Final[str] = "accepted"
    REJECTED_TOO_LARGE: Final[str] = "rejected_too_large"
    REJECTED_CONTENT_TYPE: Final[str] = "rejected_content_type"
    REJECTED_INVALID_IMAGE: Final[str] = "rejected_invalid_image"
    ERROR: Final[str] = "error"


class Stage:
    """Allowed values for the ``stage`` label on the stage histogram."""

    DOWNLOAD: Final[str] = "download"
    PROCESS: Final[str] = "process"
    UPLOAD: Final[str] = "upload"


def render_metrics(registry: CollectorRegistry = API_REGISTRY) -> tuple[bytes, str]:
    """Render a registry in Prometheus exposition format.

    Args:
        registry: Which registry to render. Defaults to the API's.

    Returns:
        A ``(payload, content_type)`` tuple ready to be returned from an HTTP
        handler.
    """
    return generate_latest(registry), METRICS_CONTENT_TYPE
