"""Upload and status logic, independent of HTTP.

Keeping this layer free of FastAPI types means the interesting behaviour —
validation ordering, the S3/DynamoDB/SQS write sequence, what happens when the
enqueue fails — is testable without spinning up a client.

All methods here are synchronous and blocking; the routes run them in a
threadpool so a slow S3 PUT never stalls the event loop.
"""

from __future__ import annotations

import re
import uuid
from typing import Final

import structlog

from shared.aws import AwsClients
from shared.config import Config
from shared.errors import ImageProcessingError
from shared.images import ALLOWED_CONTENT_TYPES, probe_image
from shared.metrics import UPLOAD_SIZE_BYTES, UPLOADS_TOTAL, UploadResult
from shared.models import JobMessage, JobRecord, JobStatus
from shared.timeutil import utc_now_iso
from shared.tracing import current_span_ids, inject_trace_context

__all__ = ["JobService", "UploadRejected", "EnqueueFailed", "sanitise_filename"]

_LOG = structlog.get_logger(__name__)

#: Control characters and path separators are stripped from the stored
#: filename; the S3 key never derives from it in any case.
_UNSAFE_FILENAME_CHARS: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f/\\]+")
_MAX_FILENAME_CHARS: Final[int] = 255
_UUID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


class UploadRejected(Exception):
    """The client sent something we will not accept.

    Carries everything the HTTP layer needs to answer, so the routes contain no
    validation policy of their own.
    """

    def __init__(self, *, detail: str, status_code: int, code: str, metric_result: str) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code
        self.code = code
        self.metric_result = metric_result

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.detail


class EnqueueFailed(Exception):
    """The job was stored but could not be queued for processing."""


def sanitise_filename(filename: str | None) -> str:
    """Reduce a client-supplied filename to something safe to store and log."""
    if not filename:
        return "upload"
    cleaned = _UNSAFE_FILENAME_CHARS.sub("", filename).strip()
    cleaned = cleaned.replace("..", "")
    return cleaned[:_MAX_FILENAME_CHARS] or "upload"


def is_valid_job_id(job_id: str) -> bool:
    """Whether ``job_id`` is a syntactically valid job identifier.

    Rejecting malformed ids before touching DynamoDB turns a scan of the
    keyspace into a cheap 404 and keeps the read path off the hot path of an
    attacker's making.
    """
    return bool(_UUID_PATTERN.match(job_id))


class JobService:
    """Creates jobs and reads them back."""

    def __init__(self, config: Config, clients: AwsClients) -> None:
        self._config = config
        self._clients = clients

    def validate_upload(self, *, content_type: str | None, size_bytes: int) -> None:
        """Check the cheap properties of an upload before decoding it.

        Args:
            content_type: The declared ``Content-Type`` of the uploaded part.
            size_bytes: Number of bytes received.

        Raises:
            UploadRejected: The type is unsupported or the payload is too large.
        """
        if size_bytes > self._config.max_upload_bytes:
            raise UploadRejected(
                detail=(
                    f"file exceeds the {self._config.max_upload_bytes} byte limit "
                    f"({size_bytes} bytes received)"
                ),
                status_code=413,
                code="payload_too_large",
                metric_result=UploadResult.REJECTED_TOO_LARGE,
            )
        normalised = (content_type or "").split(";")[0].strip().lower()
        if normalised not in ALLOWED_CONTENT_TYPES:
            raise UploadRejected(
                detail=(
                    f"unsupported content type {normalised or 'unknown'!r}; "
                    f"expected one of {sorted(ALLOWED_CONTENT_TYPES)}"
                ),
                status_code=415,
                code="unsupported_media_type",
                metric_result=UploadResult.REJECTED_CONTENT_TYPE,
            )

    def create_job(
        self, *, data: bytes, filename: str | None, content_type: str | None
    ) -> JobRecord:
        """Store an upload, record it, and enqueue it for processing.

        The write order is deliberate: object first, then the DynamoDB record,
        then the queue message. A failure at any point leaves either no trace
        or a record the client can observe — never a queued job with no object
        behind it.

        Args:
            data: The complete uploaded payload.
            filename: Client-supplied name, used only for display.
            content_type: Declared content type of the upload.

        Returns:
            The persisted :class:`~shared.models.JobRecord` in ``PENDING``.

        Raises:
            UploadRejected: The upload failed validation.
            EnqueueFailed: The record was written but SQS would not accept it.
            TransientDependencyError: S3 or DynamoDB were unreachable.
        """
        size_bytes = len(data)
        self.validate_upload(content_type=content_type, size_bytes=size_bytes)

        try:
            probe = probe_image(data)
        except ImageProcessingError as exc:
            raise UploadRejected(
                detail=str(exc),
                status_code=400,
                code="invalid_image",
                metric_result=UploadResult.REJECTED_INVALID_IMAGE,
            ) from exc

        job_id = str(uuid.uuid4())
        safe_name = sanitise_filename(filename)
        input_key = f"uploads/{job_id}/original{probe.extension}"
        trace_id, _ = current_span_ids()

        record = JobRecord(
            job_id=job_id,
            status=JobStatus.PENDING,
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
            filename=safe_name,
            size_bytes=size_bytes,
            content_type=(content_type or "").split(";")[0].strip().lower(),
            input_key=input_key,
            source_width=probe.width,
            source_height=probe.height,
            source_format=probe.format,
            trace_id=trace_id,
        )

        self._clients.store.put_bytes(input_key, data, record.content_type)
        self._clients.table.put_job(record)

        message = JobMessage(
            job_id=job_id,
            bucket=self._config.s3_bucket,
            input_key=input_key,
            content_type=record.content_type,
            filename=safe_name,
            submitted_at=record.created_at,
        )
        attributes = dict(inject_trace_context())
        attributes["job_id"] = job_id

        try:
            message_id = self._clients.queue.send_job(message.to_json(), attributes=attributes)
        except Exception as exc:
            # The object and the record exist but nothing will ever pick the
            # job up. Mark it FAILED so a poller gets a definitive answer
            # instead of a job stuck in PENDING forever.
            self._mark_unqueueable(record, exc)
            raise EnqueueFailed(str(exc)) from exc

        UPLOADS_TOTAL.labels(result=UploadResult.ACCEPTED).inc()
        UPLOAD_SIZE_BYTES.observe(size_bytes)
        _LOG.info(
            "upload_accepted",
            job_id=job_id,
            filename=safe_name,
            size_bytes=size_bytes,
            content_type=record.content_type,
            input_key=input_key,
            source_format=probe.format,
            sqs_message_id=message_id,
        )
        return record

    def get_job(self, job_id: str) -> JobRecord | None:
        """Read a job record by id.

        Returns:
            The record, or ``None`` when the id is malformed or unknown.
        """
        if not is_valid_job_id(job_id):
            return None
        return self._clients.table.get_job(job_id)

    def _mark_unqueueable(self, record: JobRecord, exc: Exception) -> None:
        """Best-effort transition to FAILED after an enqueue failure.

        Uses ``PutItem`` because the API's IAM policy deliberately excludes
        ``UpdateItem``; overwriting the item it just wrote is safe.
        """
        try:
            failed = record.model_copy(
                update={
                    "status": JobStatus.FAILED,
                    "error": f"could not enqueue job: {type(exc).__name__}",
                    "updated_at": utc_now_iso(),
                    "completed_at": utc_now_iso(),
                }
            )
            self._clients.table.put_job(failed)
        except Exception as cleanup_exc:
            _LOG.error(
                "job_failed_marking_failed",
                job_id=record.job_id,
                error=str(cleanup_exc),
            )
