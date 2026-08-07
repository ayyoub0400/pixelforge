"""Thin, typed wrappers around the three AWS services pixelforge uses.

Everything that talks to AWS goes through this module. That buys two things:

* **testability** — the API and worker depend on small interfaces that moto (or
  a stub) can stand in for, instead of reaching for ``boto3`` inline;
* **uniform resilience** — retries, error classification and SQS error metrics
  are applied in exactly one place.

Credentials are never handled here. Clients are built with the default boto3
provider chain so that IRSA, instance roles, ``AWS_PROFILE`` and LocalStack's
dummy credentials all work without a code change. Nothing in this file reads a
credentials file or accepts an access key as configuration.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Iterable, Mapping, Sequence

import boto3
import structlog
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from shared.config import Config
from shared.metrics import SQS_ERRORS_TOTAL
from shared.models import JobRecord, JobStatus, ThumbnailOutput
from shared.retry import DEFAULT_POLICY, RetryPolicy, call_with_retry
from shared.timeutil import utc_now_iso

__all__ = [
    "build_client",
    "build_dynamodb_resource",
    "S3Store",
    "JobQueue",
    "JobTable",
    "AwsClients",
    "ReadinessProbe",
    "READINESS_SENTINEL_JOB_ID",
]

_LOG = structlog.get_logger(__name__)

#: Item key used by the DynamoDB readiness probe. A ``GetItem`` for a key that
#: does not exist proves reachability using only ``dynamodb:GetItem``, which
#: both services already require.
READINESS_SENTINEL_JOB_ID: Final[str] = "__readiness_probe__"

#: Error codes that prove the dependency answered even though this principal is
#: not allowed to make that particular call. Readiness means "reachable", so a
#: 403 is a pass: the optional probe permissions stay optional.
_REACHABLE_DENIED_CODES: Final[tuple[str, ...]] = (
    "AccessDenied",
    "AccessDeniedException",
    "Forbidden",
    "403",
    "AuthorizationError",
    "UnauthorizedOperation",
    "MissingAuthenticationToken",
    "InvalidClientTokenId",
)

#: SQS long polling holds the connection for up to ``WaitTimeSeconds``; the
#: socket read timeout must comfortably exceed it or every poll aborts.
_SQS_READ_TIMEOUT: Final[int] = 40
_DEFAULT_READ_TIMEOUT: Final[int] = 30
_CONNECT_TIMEOUT: Final[int] = 5


def build_boto_config(*, read_timeout: int = _DEFAULT_READ_TIMEOUT) -> BotoConfig:
    """Build the botocore client config used for every service.

    botocore's ``standard`` retry mode is the first line of defence; the
    explicit ladder in :mod:`shared.retry` sits on top of it for the cases
    botocore gives up on.
    """
    return BotoConfig(
        retries={"max_attempts": 3, "mode": "standard"},
        connect_timeout=_CONNECT_TIMEOUT,
        read_timeout=read_timeout,
        user_agent_extra="pixelforge/1.0",
    )


def build_client(service: str, config: Config, *, read_timeout: int | None = None) -> Any:
    """Create a boto3 client for ``service``.

    Args:
        service: AWS service name, e.g. ``"s3"``.
        config: Process configuration supplying region and optional endpoint.
        read_timeout: Socket read timeout override, in seconds.

    Returns:
        A configured boto3 client using the default credential chain.
    """
    return boto3.client(
        service,
        region_name=config.aws_region,
        endpoint_url=config.aws_endpoint_url,
        config=build_boto_config(
            read_timeout=read_timeout if read_timeout is not None else _DEFAULT_READ_TIMEOUT
        ),
    )


def build_dynamodb_resource(config: Config) -> Any:
    """Create a boto3 DynamoDB resource using the default credential chain."""
    return boto3.resource(
        "dynamodb",
        region_name=config.aws_region,
        endpoint_url=config.aws_endpoint_url,
        config=build_boto_config(),
    )


# ---------------------------------------------------------------------------
# DynamoDB value marshalling
# ---------------------------------------------------------------------------


def _to_dynamo(value: Any) -> Any:
    """Convert a Python value into something DynamoDB accepts.

    DynamoDB rejects ``float``; the resource API expects ``Decimal``. Going
    through ``str`` keeps the shortest repr that round-trips.
    """
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: _to_dynamo(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        return [_to_dynamo(item) for item in value]
    return value


def _from_dynamo(value: Any) -> Any:
    """Convert a DynamoDB item back into plain Python types."""
    if isinstance(value, Decimal):
        as_int = int(value)
        return as_int if value == as_int else float(value)
    if isinstance(value, dict):
        return {key: _from_dynamo(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_from_dynamo(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------


class S3Store:
    """Object storage operations scoped to a single bucket."""

    def __init__(self, client: Any, bucket: str, *, policy: RetryPolicy = DEFAULT_POLICY) -> None:
        self._client = client
        self._bucket = bucket
        self._policy = policy

    @property
    def bucket(self) -> str:
        """Name of the bucket every operation is scoped to."""
        return self._bucket

    @property
    def client(self) -> Any:
        """Underlying boto3 client, exposed for tests and readiness probes."""
        return self._client

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        """Upload ``data`` to ``key``.

        Requires ``s3:PutObject``.
        """
        call_with_retry(
            self._client.put_object,
            operation="s3.put_object",
            policy=self._policy,
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )

    def get_bytes(self, key: str) -> bytes:
        """Download ``key`` into memory. Requires ``s3:GetObject``."""
        response = call_with_retry(
            self._client.get_object,
            operation="s3.get_object",
            policy=self._policy,
            Bucket=self._bucket,
            Key=key,
        )
        return response["Body"].read()

    def download_to(self, key: str, destination: Path) -> int:
        """Stream ``key`` to ``destination`` on local disk.

        Args:
            key: Source object key.
            destination: Path under the job's temporary workspace.

        Returns:
            Number of bytes written.
        """
        payload = self.get_bytes(key)
        destination.write_bytes(payload)
        return len(payload)

    def check(self) -> None:
        """Readiness probe.

        Uses ``HeadBucket`` (``s3:ListBucket``). A ``403`` counts as reachable,
        so granting that permission stays optional.

        Raises:
            TransientDependencyError: The bucket could not be reached.
            ClientError: The bucket is missing or otherwise misconfigured.
        """
        call_with_retry(
            self._client.head_bucket,
            operation="s3.head_bucket",
            policy=RetryPolicy(attempts=2, base_delay=0.1, max_delay=0.5),
            swallow_codes=_REACHABLE_DENIED_CODES,
            Bucket=self._bucket,
        )


# ---------------------------------------------------------------------------
# SQS
# ---------------------------------------------------------------------------


class JobQueue:
    """SQS operations for the job queue, with error metrics attached."""

    def __init__(self, client: Any, queue_url: str, *, policy: RetryPolicy = DEFAULT_POLICY) -> None:
        self._client = client
        self._queue_url = queue_url
        self._policy = policy

    @property
    def queue_url(self) -> str:
        """URL of the queue every operation targets."""
        return self._queue_url

    @property
    def client(self) -> Any:
        """Underlying boto3 client, exposed for tests."""
        return self._client

    def _call(self, operation: str, func: Any, **kwargs: Any) -> Any:
        """Invoke an SQS call, counting failures under ``operation``."""
        try:
            return call_with_retry(func, operation=operation, policy=self._policy, **kwargs)
        except Exception as exc:
            SQS_ERRORS_TOTAL.labels(operation=operation.split(".", 1)[-1]).inc()
            _LOG.warning("sqs_call_failed", operation=operation, error=str(exc))
            raise

    def send_job(self, body: str, *, attributes: Mapping[str, str] | None = None) -> str:
        """Enqueue a job. Requires ``sqs:SendMessage``.

        Args:
            body: JSON-encoded :class:`~shared.models.JobMessage`.
            attributes: String message attributes, used for trace propagation.

        Returns:
            The SQS message id.
        """
        message_attributes = {
            name: {"DataType": "String", "StringValue": value}
            for name, value in (attributes or {}).items()
            if value
        }
        response = self._call(
            "sqs.send_message",
            self._client.send_message,
            QueueUrl=self._queue_url,
            MessageBody=body,
            MessageAttributes=message_attributes,
        )
        return str(response.get("MessageId", ""))

    def receive(
        self,
        *,
        max_messages: int = 1,
        wait_seconds: int = 20,
        visibility_timeout: int | None = None,
    ) -> list[dict[str, Any]]:
        """Long-poll for messages. Requires ``sqs:ReceiveMessage``.

        Args:
            max_messages: Batch size, 1-10.
            wait_seconds: Long-poll duration; 20 is the SQS maximum.
            visibility_timeout: Per-request override of the queue default.

        Returns:
            A possibly empty list of raw SQS message dicts.
        """
        kwargs: dict[str, Any] = {
            "QueueUrl": self._queue_url,
            "MaxNumberOfMessages": max_messages,
            "WaitTimeSeconds": wait_seconds,
            "MessageAttributeNames": ["All"],
            "AttributeNames": ["ApproximateReceiveCount", "SentTimestamp"],
        }
        if visibility_timeout is not None:
            kwargs["VisibilityTimeout"] = visibility_timeout
        response = self._call("sqs.receive_message", self._client.receive_message, **kwargs)
        return list(response.get("Messages", []))

    def delete(self, receipt_handle: str) -> None:
        """Delete a processed message. Requires ``sqs:DeleteMessage``."""
        self._call(
            "sqs.delete_message",
            self._client.delete_message,
            QueueUrl=self._queue_url,
            ReceiptHandle=receipt_handle,
        )

    def change_visibility(self, receipt_handle: str, timeout_seconds: int) -> None:
        """Extend the invisibility window of an in-flight message.

        Requires ``sqs:ChangeMessageVisibility``.
        """
        self._call(
            "sqs.change_message_visibility",
            self._client.change_message_visibility,
            QueueUrl=self._queue_url,
            ReceiptHandle=receipt_handle,
            VisibilityTimeout=timeout_seconds,
        )

    def check(self) -> None:
        """Readiness probe using ``GetQueueAttributes``.

        ``sqs:GetQueueAttributes`` is optional: a ``403`` still proves the
        queue endpoint is reachable and counts as ready.
        """
        call_with_retry(
            self._client.get_queue_attributes,
            operation="sqs.get_queue_attributes",
            policy=RetryPolicy(attempts=2, base_delay=0.1, max_delay=0.5),
            swallow_codes=_REACHABLE_DENIED_CODES,
            QueueUrl=self._queue_url,
            AttributeNames=["QueueArn"],
        )


# ---------------------------------------------------------------------------
# DynamoDB
# ---------------------------------------------------------------------------


class JobTable:
    """Job records in DynamoDB.

    Key design: ``job_id`` (string) is the partition key, there is no sort key
    and no secondary index. Lookups are always by id, so a single-item GetItem
    serves the read path and the table needs no scans.
    """

    def __init__(self, table: Any, *, policy: RetryPolicy = DEFAULT_POLICY) -> None:
        self._table = table
        self._policy = policy

    @property
    def name(self) -> str:
        """Table name."""
        return str(self._table.name)

    @property
    def table(self) -> Any:
        """Underlying boto3 Table resource, exposed for tests."""
        return self._table

    def put_job(self, record: JobRecord) -> None:
        """Write a new job record. Requires ``dynamodb:PutItem`` (API only)."""
        call_with_retry(
            self._table.put_item,
            operation="dynamodb.put_item",
            policy=self._policy,
            Item=_to_dynamo(record.to_item()),
        )

    def get_job(self, job_id: str, *, consistent: bool = True) -> JobRecord | None:
        """Fetch a job by id. Requires ``dynamodb:GetItem``.

        Args:
            job_id: Partition key value.
            consistent: Strongly consistent read. The worker needs this so it
                never sees a stale ``PENDING`` for a job it just completed.

        Returns:
            The record, or ``None`` when no item exists.
        """
        response = call_with_retry(
            self._table.get_item,
            operation="dynamodb.get_item",
            policy=self._policy,
            Key={"job_id": job_id},
            ConsistentRead=consistent,
        )
        item = response.get("Item")
        if not item:
            return None
        return JobRecord.from_item(_from_dynamo(item))

    def mark_processing(self, job_id: str) -> bool:
        """Transition a job to ``PROCESSING``.

        The update is conditional on the job existing and not already being
        ``COMPLETE``, which makes a duplicate SQS delivery a no-op instead of a
        second render.

        Returns:
            ``True`` when this caller won the transition, ``False`` when the
            job was already complete or has disappeared.
        """
        try:
            call_with_retry(
                self._table.update_item,
                operation="dynamodb.update_item",
                policy=self._policy,
                Key={"job_id": job_id},
                UpdateExpression="SET #s = :processing, started_at = :now, updated_at = :now",
                ConditionExpression="attribute_exists(job_id) AND #s <> :complete",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":processing": JobStatus.PROCESSING.value,
                    ":complete": JobStatus.COMPLETE.value,
                    ":now": utc_now_iso(),
                },
            )
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def mark_complete(
        self,
        job_id: str,
        *,
        outputs: Mapping[str, ThumbnailOutput],
        exif: Mapping[str, Any],
        source_width: int,
        source_height: int,
        source_format: str,
        processing_ms: int,
    ) -> None:
        """Transition a job to ``COMPLETE`` with its outputs.

        Requires ``dynamodb:UpdateItem``. The message is deleted from SQS only
        after this call returns, so a crash here results in redelivery rather
        than a lost job.
        """
        now = utc_now_iso()
        serialised_outputs = {
            key: value.model_dump(mode="json") for key, value in outputs.items()
        }
        call_with_retry(
            self._table.update_item,
            operation="dynamodb.update_item",
            policy=self._policy,
            Key={"job_id": job_id},
            UpdateExpression=(
                "SET #s = :status, outputs = :outputs, exif = :exif, "
                "source_width = :width, source_height = :height, "
                "source_format = :format, processing_ms = :ms, "
                "completed_at = :now, updated_at = :now REMOVE #e"
            ),
            ExpressionAttributeNames={"#s": "status", "#e": "error"},
            ExpressionAttributeValues=_to_dynamo(
                {
                    ":status": JobStatus.COMPLETE.value,
                    ":outputs": serialised_outputs,
                    ":exif": dict(exif),
                    ":width": source_width,
                    ":height": source_height,
                    ":format": source_format,
                    ":ms": processing_ms,
                    ":now": now,
                }
            ),
        )

    def mark_failed(self, job_id: str, reason: str) -> None:
        """Transition a job to ``FAILED`` with a human-readable reason.

        Requires ``dynamodb:UpdateItem``. The reason is truncated so a verbose
        decoder error cannot bloat the item.
        """
        now = utc_now_iso()
        call_with_retry(
            self._table.update_item,
            operation="dynamodb.update_item",
            policy=self._policy,
            Key={"job_id": job_id},
            UpdateExpression=(
                "SET #s = :status, #e = :reason, completed_at = :now, updated_at = :now"
            ),
            ExpressionAttributeNames={"#s": "status", "#e": "error"},
            ExpressionAttributeValues={
                ":status": JobStatus.FAILED.value,
                ":reason": reason[:512],
                ":now": now,
            },
        )

    def check(self) -> None:
        """Readiness probe.

        Issues a ``GetItem`` for a sentinel key, which needs only the
        ``dynamodb:GetItem`` permission both services already hold. A missing
        item is a successful probe.
        """
        call_with_retry(
            self._table.get_item,
            operation="dynamodb.get_item",
            policy=RetryPolicy(attempts=2, base_delay=0.1, max_delay=0.5),
            swallow_codes=_REACHABLE_DENIED_CODES,
            Key={"job_id": READINESS_SENTINEL_JOB_ID},
        )


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


class AwsClients:
    """The three dependency wrappers a service needs, built together."""

    def __init__(self, store: S3Store, queue: JobQueue, table: JobTable) -> None:
        self.store = store
        self.queue = queue
        self.table = table

    @classmethod
    def build(cls, config: Config) -> "AwsClients":
        """Construct every wrapper from process configuration."""
        store = S3Store(build_client("s3", config), config.s3_bucket)
        queue = JobQueue(
            build_client("sqs", config, read_timeout=_SQS_READ_TIMEOUT),
            config.sqs_queue_url,
        )
        table = JobTable(build_dynamodb_resource(config).Table(config.dynamodb_table))
        return cls(store, queue, table)


class ReadinessProbe:
    """Runs the three dependency checks and reports per-dependency results."""

    def __init__(self, clients: AwsClients) -> None:
        self._clients = clients

    def run(self) -> tuple[bool, dict[str, str]]:
        """Probe S3, SQS and DynamoDB.

        Returns:
            ``(ready, checks)`` where ``checks`` maps dependency name to
            ``"ok"`` or ``"error: ..."``. A single failure makes ``ready``
            false, matching the ``503`` contract of ``/readyz``.
        """
        probes: Sequence[tuple[str, Any]] = (
            ("s3", self._clients.store.check),
            ("sqs", self._clients.queue.check),
            ("dynamodb", self._clients.table.check),
        )
        checks: dict[str, str] = {}
        ready = True
        for name, probe in probes:
            try:
                probe()
                checks[name] = "ok"
            except Exception as exc:  # readiness must never raise
                ready = False
                checks[name] = f"error: {_summarise(exc)}"
                _LOG.warning("readiness_check_failed", dependency=name, error=str(exc))
        return ready, checks


def _summarise(exc: BaseException) -> str:
    """Compress an exception into a short, log-safe phrase."""
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code", type(exc).__name__))
    return type(exc).__name__


def wait_for_dependencies(
    clients: AwsClients,
    *,
    attempts: int = 10,
    base_delay: float = 0.5,
    max_delay: float = 15.0,
    sleep: Any = None,
) -> None:
    """Block until every dependency answers, with exponential backoff.

    Called once at worker startup so that a pod scheduled before its
    dependencies are reachable waits instead of crash-looping.

    Args:
        clients: Wrappers to probe.
        attempts: Maximum number of probe rounds.
        base_delay: Delay before the second round.
        max_delay: Ceiling on the backoff.
        sleep: Injected sleep function (tests pass a no-op).

    Raises:
        TransientDependencyError: Dependencies were still unreachable after
            ``attempts`` rounds.
    """
    import time

    from shared.errors import TransientDependencyError

    sleeper = sleep or time.sleep
    probe = ReadinessProbe(clients)
    failures: Iterable[str] = ()

    for attempt in range(1, attempts + 1):
        ready, checks = probe.run()
        if ready:
            _LOG.info("dependencies_ready", attempt=attempt, checks=checks)
            return
        failures = [f"{name}={result}" for name, result in checks.items() if result != "ok"]
        if attempt < attempts:
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            _LOG.warning(
                "dependencies_unavailable",
                attempt=attempt,
                max_attempts=attempts,
                delay_seconds=round(delay, 2),
                failures=list(failures),
            )
            sleeper(delay)

    raise TransientDependencyError(
        "dependencies unavailable after startup retries: " + ", ".join(failures)
    )
