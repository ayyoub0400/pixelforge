"""Prometheus metrics.

Metric names are part of the published contract: dashboards, alerts and the
worker's autoscaling rule are all built against the exact names below. Renaming
one is a breaking change and must go through the CONTRACT section of the
README.

Both services import this module; each only observes the metrics it owns, so an
API scrape never reports worker series and vice versa.
"""

from __future__ import annotations

from typing import Final

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

__all__ = [
    "HTTP_REQUESTS_TOTAL",
    "HTTP_REQUEST_DURATION",
    "UPLOADS_TOTAL",
    "UPLOAD_SIZE_BYTES",
    "JOBS_PROCESSED_TOTAL",
    "JOB_DURATION_SECONDS",
    "JOBS_INFLIGHT",
    "JOB_STAGE_DURATION_SECONDS",
    "SQS_ERRORS_TOTAL",
    "render_metrics",
    "METRICS_CONTENT_TYPE",
    "UploadResult",
    "Stage",
]

METRICS_CONTENT_TYPE: Final[str] = CONTENT_TYPE_LATEST

# --------------------------------------------------------------------------
# API metrics
# --------------------------------------------------------------------------

HTTP_REQUESTS_TOTAL: Final[Counter] = Counter(
    "pixelforge_http_requests_total",
    "Total HTTP requests handled by the API.",
    labelnames=("method", "endpoint", "status"),
)

HTTP_REQUEST_DURATION: Final[Histogram] = Histogram(
    "pixelforge_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    labelnames=("endpoint",),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

UPLOADS_TOTAL: Final[Counter] = Counter(
    "pixelforge_uploads_total",
    "Upload attempts by outcome.",
    labelnames=("result",),
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
)

# --------------------------------------------------------------------------
# Worker metrics
# --------------------------------------------------------------------------

JOBS_PROCESSED_TOTAL: Final[Counter] = Counter(
    "pixelforge_jobs_processed_total",
    "Jobs that reached a terminal state.",
    labelnames=("status",),
)

JOB_DURATION_SECONDS: Final[Histogram] = Histogram(
    "pixelforge_job_duration_seconds",
    "End-to-end job processing time in the worker, in seconds.",
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
)

JOBS_INFLIGHT: Final[Gauge] = Gauge(
    "pixelforge_jobs_inflight",
    "Jobs currently being processed by this worker instance.",
)

JOB_STAGE_DURATION_SECONDS: Final[Histogram] = Histogram(
    "pixelforge_job_stage_duration_seconds",
    "Per-stage processing time in seconds.",
    labelnames=("stage",),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

SQS_ERRORS_TOTAL: Final[Counter] = Counter(
    "pixelforge_sqs_errors_total",
    "SQS API call failures by operation.",
    labelnames=("operation",),
)


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


def render_metrics() -> tuple[bytes, str]:
    """Render the default registry in Prometheus exposition format.

    Returns:
        A ``(payload, content_type)`` tuple ready to be returned from an HTTP
        handler.
    """
    return generate_latest(REGISTRY), METRICS_CONTENT_TYPE
