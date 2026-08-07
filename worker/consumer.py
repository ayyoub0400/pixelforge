"""The SQS consume loop and the job pipeline it drives.

The rules this module exists to enforce:

* **At-least-once delivery is safe.** A job that is already ``COMPLETE`` is
  skipped, and the transition to ``PROCESSING`` is a conditional write, so a
  duplicate delivery produces one result, not two.
* **A bad upload never crashes the worker.** Anything that fails to decode
  marks the job ``FAILED`` and the message is deleted; the pod keeps serving.
* **A dependency outage never loses a job.** Infrastructure failures leave the
  message on the queue so SQS redelivers it, and the redrive policy eventually
  parks it on the DLQ.
* **The message is deleted last.** Only after DynamoDB says ``COMPLETE``.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final, Iterator, Mapping

import structlog
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from shared.aws import AwsClients
from shared.config import Config
from shared.errors import ImageProcessingError, TransientDependencyError
from shared.images import render_thumbnails
from shared.logging_setup import bind_job_context, clear_job_context, redact_exif
from shared.metrics import (
    JOB_DURATION_SECONDS,
    JOB_STAGE_DURATION_SECONDS,
    JOBS_INFLIGHT,
    JOBS_PROCESSED_TOTAL,
    Stage,
)
from shared.models import MESSAGE_SCHEMA_VERSION, JobMessage, JobStatus, ThumbnailOutput
from shared.tempfiles import temp_workspace
from shared.tracing import extract_trace_context, span
from worker.heartbeat import (
    HEARTBEAT_INTERVAL_SECONDS,
    VISIBILITY_EXTENSION_SECONDS,
    VisibilityHeartbeat,
)

__all__ = ["Worker", "Outcome", "SERVICE_NAME"]

_LOG = structlog.get_logger(__name__)

SERVICE_NAME: Final[str] = "worker"

#: SQS long-poll duration. 20s is the maximum and minimises empty receives.
POLL_WAIT_SECONDS: Final[int] = 20

#: Pause after an unexpected receive failure so a persistent error does not
#: turn into a hot loop against the SQS API.
RECEIVE_BACKOFF_SECONDS: Final[float] = 2.0

#: Content type written for every rendered thumbnail.
_THUMBNAIL_CONTENT_TYPE: Final[str] = "image/jpeg"


class Outcome(str, Enum):
    """What happened to a single message. Returned to make tests explicit."""

    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    RETRY = "retry"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class _ProcessedOutputs:
    """Everything a successful render produced."""

    outputs: dict[str, ThumbnailOutput]
    exif: dict[str, Any]
    width: int
    height: int
    image_format: str


@contextlib.contextmanager
def _stage(name: str) -> Iterator[None]:
    """Time one pipeline stage into ``pixelforge_job_stage_duration_seconds``."""
    started = time.perf_counter()
    try:
        yield
    finally:
        JOB_STAGE_DURATION_SECONDS.labels(stage=name).observe(time.perf_counter() - started)


class Worker:
    """Consumes job messages and renders thumbnails."""

    def __init__(
        self,
        config: Config,
        clients: AwsClients,
        *,
        poll_wait_seconds: int = POLL_WAIT_SECONDS,
        heartbeat_interval_seconds: int = HEARTBEAT_INTERVAL_SECONDS,
        visibility_extension_seconds: int = VISIBILITY_EXTENSION_SECONDS,
        sleep: Any = time.sleep,
    ) -> None:
        self._config = config
        self._clients = clients
        self._poll_wait_seconds = poll_wait_seconds
        self._heartbeat_interval = heartbeat_interval_seconds
        self._visibility_extension = visibility_extension_seconds
        self._sleep = sleep
        self._stopping = False
        self._stop_reason: str | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def stopping(self) -> bool:
        """Whether a shutdown has been requested."""
        return self._stopping

    def request_stop(self, reason: str = "requested") -> None:
        """Ask the loop to finish the current job and exit.

        Safe to call from a signal handler: it only sets a flag.
        """
        if not self._stopping:
            self._stopping = True
            self._stop_reason = reason

    def run(self) -> None:
        """Poll until :meth:`request_stop` is called.

        Never raises. A failure inside one iteration is logged and the loop
        continues, because a worker that exits on a transient SQS error just
        becomes a CrashLoopBackOff.
        """
        _LOG.info(
            "worker_started",
            queue=self._config.sqs_queue_url.rsplit("/", 1)[-1],
            bucket=self._config.s3_bucket,
            table=self._config.dynamodb_table,
            thumbnail_sizes=list(self._config.thumbnail_sizes),
            poll_wait_seconds=self._poll_wait_seconds,
        )
        while not self._stopping:
            try:
                self.run_once()
            except Exception as exc:  # pragma: no cover - belt and braces
                _LOG.exception("poll_iteration_failed", error=str(exc))
                self._sleep(RECEIVE_BACKOFF_SECONDS)
        _LOG.info("worker_stopped", reason=self._stop_reason or "requested")

    def run_once(self) -> int:
        """Perform one receive-and-process cycle.

        Returns:
            The number of messages handled, which is ``0`` on an empty poll.
        """
        try:
            messages = self._clients.queue.receive(
                max_messages=1, wait_seconds=self._poll_wait_seconds
            )
        except (TransientDependencyError, ClientError, BotoCoreError) as exc:
            _LOG.error("sqs_receive_failed", error=str(exc))
            self._sleep(RECEIVE_BACKOFF_SECONDS)
            return 0

        for message in messages:
            self.handle_message(message)
        return len(messages)

    # -- message handling --------------------------------------------------

    def handle_message(self, message: Mapping[str, Any]) -> Outcome:
        """Process one raw SQS message.

        Never raises: every failure mode is converted into an :class:`Outcome`
        so the poll loop cannot be taken down by one bad job.
        """
        receipt_handle = str(message.get("ReceiptHandle", ""))
        carrier = _trace_carrier(message)

        try:
            job = JobMessage.model_validate_json(str(message.get("Body", "")))
        except (ValidationError, ValueError) as exc:
            # Nothing here identifies a job, so there is no record to fail.
            # Delete it: leaving it would only cycle it to the DLQ, and it is
            # already fully described in this log line.
            _LOG.error(
                "message_malformed",
                error=str(exc),
                message_id=message.get("MessageId"),
            )
            JOBS_PROCESSED_TOTAL.labels(status="failed").inc()
            self._delete_quietly(receipt_handle)
            return Outcome.MALFORMED

        if job.schema_version > MESSAGE_SCHEMA_VERSION:
            # A newer producer is running. Leave the message for a worker that
            # understands it; the redrive policy parks it if none exists.
            _LOG.error(
                "message_schema_unsupported",
                job_id=job.job_id,
                schema_version=job.schema_version,
                supported=MESSAGE_SCHEMA_VERSION,
            )
            return Outcome.RETRY

        bind_job_context(job_id=job.job_id)
        try:
            with span(
                "worker.process_job",
                context=extract_trace_context(carrier),
                attributes={
                    "job.id": job.job_id,
                    "messaging.system": "aws_sqs",
                    "messaging.operation": "process",
                },
            ):
                return self._process(job, receipt_handle, message)
        finally:
            clear_job_context()

    def _process(
        self, job: JobMessage, receipt_handle: str, message: Mapping[str, Any]
    ) -> Outcome:
        """Run the pipeline for one job."""
        receive_count = _receive_count(message)

        try:
            record = self._clients.table.get_job(job.job_id)
        except (TransientDependencyError, ClientError, BotoCoreError) as exc:
            _LOG.error("job_lookup_failed", error=str(exc))
            return Outcome.RETRY

        if record is None:
            _LOG.error("job_record_missing", input_key=job.input_key)
            JOBS_PROCESSED_TOTAL.labels(status="failed").inc()
            self._delete_quietly(receipt_handle)
            return Outcome.FAILED

        if record.status is JobStatus.COMPLETE:
            _LOG.info("job_skipped_already_complete", receive_count=receive_count)
            self._delete_quietly(receipt_handle)
            return Outcome.SKIPPED_DUPLICATE

        if job.bucket != self._config.s3_bucket:
            # The message came from a differently configured producer. Refusing
            # is safer than reading from a bucket this worker was not scoped to.
            return self._fail_job(
                job.job_id,
                receipt_handle,
                f"message references unexpected bucket {job.bucket!r}",
            )

        try:
            won = self._clients.table.mark_processing(job.job_id)
        except (TransientDependencyError, ClientError, BotoCoreError) as exc:
            _LOG.error("job_mark_processing_failed", error=str(exc))
            return Outcome.RETRY

        if not won:
            _LOG.info("job_skipped_lost_race", receive_count=receive_count)
            self._delete_quietly(receipt_handle)
            return Outcome.SKIPPED_DUPLICATE

        _LOG.info(
            "job_processing_started",
            input_key=job.input_key,
            receive_count=receive_count,
            previous_status=record.status.value,
        )

        JOBS_INFLIGHT.inc()
        started = time.perf_counter()
        try:
            with VisibilityHeartbeat(
                self._clients.queue,
                receipt_handle,
                job_id=job.job_id,
                interval_seconds=self._heartbeat_interval,
                extension_seconds=self._visibility_extension,
            ):
                processed = self._render(job)

            elapsed = time.perf_counter() - started
            self._clients.table.mark_complete(
                job.job_id,
                outputs=processed.outputs,
                exif=processed.exif,
                source_width=processed.width,
                source_height=processed.height,
                source_format=processed.image_format,
                processing_ms=int(elapsed * 1000),
            )
            # Only now is it safe to drop the message: DynamoDB is the record
            # of truth and it says the job is done.
            self._clients.queue.delete(receipt_handle)

            JOBS_PROCESSED_TOTAL.labels(status="complete").inc()
            JOB_DURATION_SECONDS.observe(elapsed)
            _LOG.info(
                "job_completed",
                duration_ms=round(elapsed * 1000, 2),
                thumbnails=sorted(processed.outputs),
                source_width=processed.width,
                source_height=processed.height,
                exif_tags=len(redact_exif(processed.exif)),
            )
            return Outcome.COMPLETED

        except ImageProcessingError as exc:
            JOB_DURATION_SECONDS.observe(time.perf_counter() - started)
            return self._fail_job(job.job_id, receipt_handle, str(exc))

        except (TransientDependencyError, ClientError, BotoCoreError) as exc:
            # Leave the message on the queue. SQS will redeliver, and the
            # redrive policy sends it to the DLQ if this keeps happening.
            _LOG.error(
                "job_infrastructure_failure",
                error=str(exc),
                error_type=type(exc).__name__,
                receive_count=receive_count,
            )
            return Outcome.RETRY

        except Exception as exc:  # pragma: no cover - unexpected bug
            _LOG.exception("job_unexpected_error", error=str(exc))
            return Outcome.RETRY

        finally:
            JOBS_INFLIGHT.dec()

    def _render(self, job: JobMessage) -> _ProcessedOutputs:
        """Download, render and upload, timing each stage.

        The workspace is a private directory under ``/tmp`` that is removed
        even if a stage raises, so the container filesystem stays clean and
        every replica is interchangeable.
        """
        with temp_workspace() as workspace:
            original = workspace / "original"

            with _stage(Stage.DOWNLOAD):
                downloaded = self._clients.store.download_to(job.input_key, original)

            with _stage(Stage.PROCESS):
                thumbnails, exif, width, height, image_format = render_thumbnails(
                    original, self._config.thumbnail_sizes
                )

            with _stage(Stage.UPLOAD):
                outputs: dict[str, ThumbnailOutput] = {}
                for thumbnail in thumbnails:
                    key = f"outputs/{job.job_id}/thumb_{thumbnail.size}.jpg"
                    self._clients.store.put_bytes(
                        key, thumbnail.data, _THUMBNAIL_CONTENT_TYPE
                    )
                    outputs[str(thumbnail.size)] = ThumbnailOutput(
                        size=thumbnail.size,
                        key=key,
                        width=thumbnail.width,
                        height=thumbnail.height,
                        bytes=thumbnail.bytes_len,
                    )

        _LOG.debug(
            "render_finished",
            downloaded_bytes=downloaded,
            thumbnail_count=len(outputs),
        )
        return _ProcessedOutputs(
            outputs=outputs,
            exif=exif,
            width=width,
            height=height,
            image_format=image_format,
        )

    # -- failure paths -----------------------------------------------------

    def _fail_job(self, job_id: str, receipt_handle: str, reason: str) -> Outcome:
        """Mark a job ``FAILED`` and remove its message.

        If DynamoDB is unavailable the message is intentionally left on the
        queue: better to reprocess a doomed job than to lose the record of it.
        """
        try:
            self._clients.table.mark_failed(job_id, reason)
        except (TransientDependencyError, ClientError, BotoCoreError) as exc:
            _LOG.error("job_mark_failed_failed", error=str(exc), reason=reason)
            return Outcome.RETRY

        JOBS_PROCESSED_TOTAL.labels(status="failed").inc()
        self._delete_quietly(receipt_handle)
        _LOG.warning("job_failed", reason=reason)
        return Outcome.FAILED

    def _delete_quietly(self, receipt_handle: str) -> None:
        """Delete a message, tolerating a failure to do so.

        The caller has already reached a terminal state; a failed delete only
        means one redundant redelivery, which idempotency absorbs.
        """
        if not receipt_handle:
            return
        try:
            self._clients.queue.delete(receipt_handle)
        except Exception as exc:
            _LOG.warning("message_delete_failed", error=str(exc))


def _trace_carrier(message: Mapping[str, Any]) -> dict[str, str]:
    """Pull the W3C trace headers out of the SQS message attributes."""
    attributes = message.get("MessageAttributes") or {}
    return {
        name: str(value.get("StringValue"))
        for name, value in attributes.items()
        if isinstance(value, Mapping) and value.get("StringValue")
    }


def _receive_count(message: Mapping[str, Any]) -> int:
    """How many times SQS has delivered this message, for log context."""
    raw = (message.get("Attributes") or {}).get("ApproximateReceiveCount", "1")
    try:
        return int(raw)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return 1
