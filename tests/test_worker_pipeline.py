"""The worker pipeline: happy path, idempotency, poison messages, outages."""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from typing import Any

import pytest
from botocore.exceptions import ClientError
from conftest import TEST_BUCKET, metric_value, upload_fixture
from PIL import Image

from shared.aws import AwsClients
from shared.config import Config
from shared.errors import TransientDependencyError
from shared.models import JobMessage, JobStatus
from worker.consumer import Outcome, Worker


def _submit(client: Any, name: str = "landscape.jpg", content_type: str = "image/jpeg") -> str:
    """Upload a fixture through the API and return the job id."""
    response = upload_fixture(client, name, content_type=content_type)
    assert response.status_code == 202
    return str(response.json()["job_id"])


def _next_message(clients: AwsClients) -> dict[str, Any]:
    messages = clients.queue.receive(max_messages=1, wait_seconds=0)
    assert messages, "expected a queued job"
    return messages[0]


def _queue_depth(clients: AwsClients) -> int:
    attributes = clients.queue.client.get_queue_attributes(
        QueueUrl=clients.queue.queue_url,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    return int(attributes["ApproximateNumberOfMessages"]) + int(
        attributes["ApproximateNumberOfMessagesNotVisible"]
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_end_to_end_upload_to_complete(
    client: Any, clients: AwsClients, worker: Worker, raw_clients: dict[str, Any]
) -> None:
    job_id = _submit(client)

    assert worker.run_once() == 1

    record = clients.table.get_job(job_id)
    assert record is not None
    assert record.status is JobStatus.COMPLETE
    assert record.completed_at is not None
    assert record.processing_ms is not None and record.processing_ms >= 0
    assert record.source_width == 1600 and record.source_height == 900

    assert sorted(record.outputs or {}) == ["150", "400", "800"]
    for size, output in (record.outputs or {}).items():
        assert output.key == f"outputs/{job_id}/thumb_{size}.jpg"
        stored = raw_clients["s3"].get_object(Bucket=TEST_BUCKET, Key=output.key)
        assert stored["ContentType"] == "image/jpeg"
        with Image.open(io.BytesIO(stored["Body"].read())) as rendered:
            assert rendered.size == (output.width, output.height)
            assert max(rendered.size) == int(size)

    # The message is gone only because DynamoDB was updated first.
    assert _queue_depth(clients) == 0


def test_completed_job_is_visible_through_the_api(client: Any, worker: Worker) -> None:
    job_id = _submit(client)
    worker.run_once()

    body = client.get(f"/api/v1/jobs/{job_id}").json()

    assert body["status"] == "COMPLETE"
    assert body["outputs"]["400"]["width"] == 400
    assert body["exif"]["Make"] == "PixelForge"


def test_gps_is_not_persisted(client: Any, clients: AwsClients, worker: Worker) -> None:
    """The landscape fixture carries GPS EXIF; none of it may be stored."""
    job_id = _submit(client)
    worker.run_once()

    record = clients.table.get_job(job_id)
    assert record is not None and record.exif
    assert not [key for key in record.exif if key.lower().startswith("gps")]


def test_happy_path_records_metrics(client: Any, worker: Worker) -> None:
    before_complete = metric_value("pixelforge_jobs_processed_total", {"status": "complete"})
    before_duration = metric_value("pixelforge_job_duration_seconds_count")
    before_stages = {
        stage: metric_value("pixelforge_job_stage_duration_seconds_count", {"stage": stage})
        for stage in ("download", "process", "upload")
    }

    _submit(client)
    worker.run_once()

    assert (
        metric_value("pixelforge_jobs_processed_total", {"status": "complete"})
        == before_complete + 1
    )
    assert metric_value("pixelforge_job_duration_seconds_count") == before_duration + 1
    for stage, before in before_stages.items():
        assert (
            metric_value("pixelforge_job_stage_duration_seconds_count", {"stage": stage})
            == before + 1
        )
    # The gauge is decremented in a finally block, so it returns to zero.
    assert metric_value("pixelforge_jobs_inflight") == 0


def test_empty_poll_is_a_no_op(worker: Worker) -> None:
    assert worker.run_once() == 0


def test_thumbnail_sizes_follow_configuration(
    client: Any, clients: AwsClients, config: Config
) -> None:
    custom = Config(
        aws_region=config.aws_region,
        s3_bucket=config.s3_bucket,
        sqs_queue_url=config.sqs_queue_url,
        dynamodb_table=config.dynamodb_table,
        thumbnail_sizes=(64, 512),
    )
    job_id = _submit(client)

    Worker(custom, clients, poll_wait_seconds=0, sleep=lambda _s: None).run_once()

    record = clients.table.get_job(job_id)
    assert record is not None
    assert sorted(record.outputs or {}) == ["512", "64"]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_processing_the_same_message_twice_produces_one_result(
    client: Any, clients: AwsClients, worker: Worker
) -> None:
    job_id = _submit(client)
    message = _next_message(clients)

    first = worker.handle_message(message)
    record_after_first = clients.table.get_job(job_id)

    # SQS is at-least-once: the very same message can arrive again.
    second = worker.handle_message(message)
    record_after_second = clients.table.get_job(job_id)

    assert first is Outcome.COMPLETED
    assert second is Outcome.SKIPPED_DUPLICATE
    assert record_after_first is not None and record_after_second is not None
    assert record_after_second.completed_at == record_after_first.completed_at
    assert record_after_second.processing_ms == record_after_first.processing_ms
    assert record_after_second.outputs == record_after_first.outputs


def test_duplicate_delivery_does_not_double_count(
    client: Any, clients: AwsClients, worker: Worker
) -> None:
    _submit(client)
    message = _next_message(clients)
    worker.handle_message(message)

    before = metric_value("pixelforge_jobs_processed_total", {"status": "complete"})
    worker.handle_message(message)

    assert metric_value("pixelforge_jobs_processed_total", {"status": "complete"}) == before


def test_conditional_transition_blocks_a_concurrent_worker(
    client: Any, clients: AwsClients
) -> None:
    """Two workers racing on one message: only one may take the job."""
    job_id = _submit(client)

    assert clients.table.mark_processing(job_id) is True
    clients.table.mark_complete(
        job_id,
        outputs={},
        exif={},
        source_width=1,
        source_height=1,
        source_format="JPEG",
        processing_ms=1,
    )

    assert clients.table.mark_processing(job_id) is False


# ---------------------------------------------------------------------------
# Poison messages
# ---------------------------------------------------------------------------


def test_corrupt_image_marks_the_job_failed_without_raising(
    client: Any,
    clients: AwsClients,
    worker: Worker,
    raw_clients: dict[str, Any],
    fixture_bytes: Callable[[str], bytes],
) -> None:
    """A file that decodes at upload but not at render: the poison case."""
    job_id = _submit(client, "truncated.jpg")
    # The upload gate only parses the header, so this reaches the worker.
    raw_clients["s3"].put_object(
        Bucket=TEST_BUCKET,
        Key=f"uploads/{job_id}/original.jpg",
        Body=fixture_bytes("truncated.jpg"),
    )
    before = metric_value("pixelforge_jobs_processed_total", {"status": "failed"})

    outcome = worker.handle_message(_next_message(clients))

    assert outcome is Outcome.FAILED
    record = clients.table.get_job(job_id)
    assert record is not None
    assert record.status is JobStatus.FAILED
    assert record.error and "could not be processed" in record.error
    assert metric_value("pixelforge_jobs_processed_total", {"status": "failed"}) == before + 1
    # Deleted, so it never reaches the DLQ and never blocks the queue.
    assert _queue_depth(clients) == 0


def test_a_poison_message_does_not_stop_the_next_job(
    client: Any,
    clients: AwsClients,
    worker: Worker,
    raw_clients: dict[str, Any],
    fixture_bytes: Callable[[str], bytes],
) -> None:
    poison_id = _submit(client, "truncated.jpg")
    raw_clients["s3"].put_object(
        Bucket=TEST_BUCKET,
        Key=f"uploads/{poison_id}/original.jpg",
        Body=fixture_bytes("corrupt.jpg"),
    )
    good_id = _submit(client)

    worker.run_once()
    worker.run_once()

    assert (clients.table.get_job(poison_id) or {}).status is JobStatus.FAILED  # type: ignore[union-attr]
    assert (clients.table.get_job(good_id) or {}).status is JobStatus.COMPLETE  # type: ignore[union-attr]


def test_missing_object_is_left_for_redelivery(
    client: Any, clients: AwsClients, worker: Worker, raw_clients: dict[str, Any]
) -> None:
    job_id = _submit(client)
    raw_clients["s3"].delete_object(Bucket=TEST_BUCKET, Key=f"uploads/{job_id}/original.jpg")

    outcome = worker.handle_message(_next_message(clients))

    assert outcome is Outcome.RETRY
    record = clients.table.get_job(job_id)
    assert record is not None and record.status is JobStatus.PROCESSING


def test_malformed_message_body_is_dropped(clients: AwsClients, worker: Worker) -> None:
    clients.queue.send_job("this is not json")
    before = metric_value("pixelforge_jobs_processed_total", {"status": "failed"})

    outcome = worker.handle_message(_next_message(clients))

    assert outcome is Outcome.MALFORMED
    assert metric_value("pixelforge_jobs_processed_total", {"status": "failed"}) == before + 1
    assert _queue_depth(clients) == 0


def test_message_for_an_unknown_job_is_dropped(clients: AwsClients, worker: Worker) -> None:
    clients.queue.send_job(
        JobMessage(
            job_id="11111111-2222-4333-8444-555555555555",
            bucket=TEST_BUCKET,
            input_key="uploads/nope/original.jpg",
            content_type="image/jpeg",
            filename="nope.jpg",
            submitted_at="2026-01-01T00:00:00.000Z",
        ).to_json()
    )

    assert worker.handle_message(_next_message(clients)) is Outcome.FAILED
    assert _queue_depth(clients) == 0


def test_message_from_another_environment_fails_the_job(
    client: Any, clients: AwsClients, worker: Worker
) -> None:
    job_id = _submit(client)
    message = _next_message(clients)
    body = json.loads(message["Body"])
    body["bucket"] = "someone-elses-bucket"
    message = {**message, "Body": json.dumps(body)}

    outcome = worker.handle_message(message)

    assert outcome is Outcome.FAILED
    record = clients.table.get_job(job_id)
    assert record is not None and record.error and "unexpected bucket" in record.error


def test_newer_schema_version_is_left_for_the_dlq(
    client: Any, clients: AwsClients, worker: Worker
) -> None:
    _submit(client)
    message = _next_message(clients)
    body = json.loads(message["Body"])
    body["schema_version"] = 99
    message = {**message, "Body": json.dumps(body)}

    outcome = worker.handle_message(message)

    assert outcome is Outcome.RETRY
    # Not deleted: it stays in flight and the redrive policy parks it.
    assert _queue_depth(clients) == 1


# ---------------------------------------------------------------------------
# Infrastructure failures
# ---------------------------------------------------------------------------


def test_dynamodb_failure_leaves_the_message_on_the_queue(
    client: Any, clients: AwsClients, worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The final update failing must not silently drop the job."""
    job_id = _submit(client)

    def explode(*_args: object, **_kwargs: object) -> None:
        raise TransientDependencyError("dynamodb is down", operation="dynamodb.update_item")

    monkeypatch.setattr(clients.table, "mark_complete", explode)

    outcome = worker.handle_message(_next_message(clients))

    assert outcome is Outcome.RETRY
    record = clients.table.get_job(job_id)
    assert record is not None and record.status is JobStatus.PROCESSING
    assert _queue_depth(clients) == 1


def test_failure_to_record_a_failure_also_leaves_the_message(
    client: Any,
    clients: AwsClients,
    worker: Worker,
    raw_clients: dict[str, Any],
    fixture_bytes: Callable[[str], bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = _submit(client, "truncated.jpg")
    raw_clients["s3"].put_object(
        Bucket=TEST_BUCKET,
        Key=f"uploads/{job_id}/original.jpg",
        Body=fixture_bytes("truncated.jpg"),
    )

    def explode(*_args: object, **_kwargs: object) -> None:
        raise TransientDependencyError("dynamodb is down", operation="dynamodb.update_item")

    monkeypatch.setattr(clients.table, "mark_failed", explode)

    assert worker.handle_message(_next_message(clients)) is Outcome.RETRY
    assert _queue_depth(clients) == 1


def test_receive_failure_does_not_break_the_loop(
    worker: Worker, monkeypatch: pytest.MonkeyPatch, clients: AwsClients
) -> None:
    def explode(**_kwargs: object) -> None:
        raise TransientDependencyError("sqs is down", operation="sqs.receive_message")

    monkeypatch.setattr(clients.queue, "receive", explode)

    assert worker.run_once() == 0


def test_sqs_errors_are_counted(clients: AwsClients) -> None:
    before = metric_value("pixelforge_sqs_errors_total", {"operation": "delete_message"})

    with pytest.raises(ClientError):
        clients.queue.delete("not-a-real-receipt-handle")

    assert (
        metric_value("pixelforge_sqs_errors_total", {"operation": "delete_message"}) == before + 1
    )
