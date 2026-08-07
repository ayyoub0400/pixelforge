"""Data contracts shared by the API, the worker and the test-suite.

These models are the single source of truth for three wire formats that other
teams build against:

* the DynamoDB item (see :class:`JobRecord`),
* the SQS message body (see :class:`JobMessage`),
* the HTTP responses of the API.

Changing a field name here changes a published contract; keep the README's
CONTRACT section in step.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "MESSAGE_SCHEMA_VERSION",
    "ChaosRequest",
    "ChaosState",
    "ErrorResponse",
    "JobAcceptedResponse",
    "JobMessage",
    "JobRecord",
    "JobStatus",
    "ReadinessResponse",
    "ThumbnailOutput",
]

#: Bumped whenever :class:`JobMessage` gains a breaking change. The worker
#: refuses versions it does not understand rather than guessing.
MESSAGE_SCHEMA_VERSION: Final[int] = 1


class JobStatus(StrEnum):
    """Lifecycle of a job. Only ``COMPLETE`` and ``FAILED`` are terminal."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        """Whether no further transition is expected from this state."""
        return self in (JobStatus.COMPLETE, JobStatus.FAILED)


class ThumbnailOutput(BaseModel):
    """One generated thumbnail, as stored under ``outputs`` in DynamoDB."""

    model_config = ConfigDict(extra="allow")

    size: int = Field(description="Requested bounding-box edge in pixels.")
    key: str = Field(description="S3 object key of the rendered thumbnail.")
    width: int = Field(description="Actual width after aspect-ratio preserving fit.")
    height: int = Field(description="Actual height after aspect-ratio preserving fit.")
    bytes: int = Field(description="Size of the rendered JPEG in bytes.")


class JobRecord(BaseModel):
    """The DynamoDB item for a job.

    ``job_id`` is the partition key; there is no sort key and no secondary
    index. Attributes that are not yet known are omitted from the item rather
    than stored as null.
    """

    model_config = ConfigDict(extra="allow", use_enum_values=False)

    job_id: str
    status: JobStatus
    created_at: str
    filename: str
    size_bytes: int
    content_type: str
    input_key: str

    updated_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None

    outputs: dict[str, ThumbnailOutput] | None = None
    exif: dict[str, Any] | None = None
    source_width: int | None = None
    source_height: int | None = None
    source_format: str | None = None
    processing_ms: int | None = None

    error: str | None = None
    trace_id: str | None = None

    def to_item(self) -> dict[str, Any]:
        """Render as a DynamoDB-ready dict with ``None`` values dropped."""
        item = self.model_dump(mode="json", exclude_none=True)
        item["status"] = self.status.value
        return item

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> JobRecord:
        """Rebuild from a DynamoDB item."""
        return cls.model_validate(item)

    def public_view(self) -> dict[str, Any]:
        """Render the record for the ``GET /api/v1/jobs/{job_id}`` response."""
        return self.model_dump(mode="json", exclude_none=True)


class JobMessage(BaseModel):
    """The SQS message body written by the API and read by the worker.

    Trace context travels in SQS *message attributes*, not in this body, so
    that the body stays a stable data contract independent of telemetry.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: int = MESSAGE_SCHEMA_VERSION
    job_id: str
    bucket: str
    input_key: str
    content_type: str
    filename: str
    submitted_at: str

    def to_json(self) -> str:
        """Serialise for :meth:`shared.aws.JobQueue.send_job`."""
        return self.model_dump_json(exclude_none=True)


class JobAcceptedResponse(BaseModel):
    """``202`` body returned by ``POST /api/v1/jobs``."""

    job_id: str
    status: JobStatus


class ChaosRequest(BaseModel):
    """Body of ``POST /admin/chaos``. Every field is an optional delta."""

    model_config = ConfigDict(extra="forbid")

    fail_readiness: bool | None = Field(
        default=None, description="When true, /readyz reports 503 without probing AWS."
    )
    latency_ms: int | None = Field(
        default=None, ge=0, le=60_000, description="Artificial delay added to /api/v1 requests."
    )
    error_rate: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Fraction of /api/v1 requests failed with 500."
    )


class ChaosState(BaseModel):
    """Current chaos settings, echoed back by ``POST /admin/chaos``."""

    fail_readiness: bool = False
    latency_ms: int = 0
    error_rate: float = 0.0


class ReadinessResponse(BaseModel):
    """Body of ``GET /readyz`` for both the healthy and unhealthy cases."""

    status: str
    checks: dict[str, str]


class ErrorResponse(BaseModel):
    """Uniform error body for every non-2xx API response."""

    detail: str
    code: str
