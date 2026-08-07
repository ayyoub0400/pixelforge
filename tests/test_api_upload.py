"""Upload validation and the write sequence behind ``POST /api/v1/jobs``."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from conftest import TEST_BUCKET, metric_value, upload_fixture

from shared.aws import AwsClients
from shared.models import JobMessage, JobStatus


def test_upload_returns_202_with_a_job_id(client: Any) -> None:
    response = upload_fixture(client, "landscape.jpg")

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "PENDING"
    # A well-formed UUID, not an incrementing integer that leaks volume.
    uuid.UUID(body["job_id"])
    assert set(body) == {"job_id", "status"}


def test_upload_stores_the_original_in_s3(client: Any, raw_clients: dict[str, Any]) -> None:
    job_id = upload_fixture(client, "landscape.jpg").json()["job_id"]

    key = f"uploads/{job_id}/original.jpg"
    head = raw_clients["s3"].head_object(Bucket=TEST_BUCKET, Key=key)
    assert head["ContentLength"] > 0
    assert head["ContentType"] == "image/jpeg"


def test_upload_extension_comes_from_the_detected_format_not_the_filename(
    client: Any, fixture_bytes: Callable[[str], bytes], raw_clients: dict[str, Any]
) -> None:
    """A crafted filename must not influence the S3 key."""
    payload = fixture_bytes("portrait.png")
    response = client.post(
        "/api/v1/jobs",
        files={"file": ("../../etc/passwd.jpg", payload, "image/png")},
    )

    job_id = response.json()["job_id"]
    listing = raw_clients["s3"].list_objects_v2(Bucket=TEST_BUCKET, Prefix=f"uploads/{job_id}/")
    keys = [item["Key"] for item in listing.get("Contents", [])]
    assert keys == [f"uploads/{job_id}/original.png"]


def test_upload_writes_a_pending_dynamodb_record(client: Any, clients: AwsClients) -> None:
    job_id = upload_fixture(client, "landscape.jpg").json()["job_id"]

    record = clients.table.get_job(job_id)
    assert record is not None
    assert record.status is JobStatus.PENDING
    assert record.filename == "landscape.jpg"
    assert record.size_bytes > 0
    assert record.content_type == "image/jpeg"
    assert record.input_key == f"uploads/{job_id}/original.jpg"
    assert record.created_at.endswith("Z")
    assert record.outputs is None


def test_upload_enqueues_a_message_matching_the_documented_schema(
    client: Any, clients: AwsClients
) -> None:
    job_id = upload_fixture(client, "landscape.jpg").json()["job_id"]

    messages = clients.queue.receive(max_messages=1, wait_seconds=0)
    assert len(messages) == 1

    body = json.loads(messages[0]["Body"])
    message = JobMessage.model_validate(body)
    assert message.job_id == job_id
    assert message.bucket == TEST_BUCKET
    assert message.input_key == f"uploads/{job_id}/original.jpg"
    assert message.schema_version == 1
    assert message.content_type == "image/jpeg"

    attributes = messages[0].get("MessageAttributes", {})
    assert attributes["job_id"]["StringValue"] == job_id


def test_filename_is_sanitised_before_being_stored(
    client: Any, clients: AwsClients, fixture_bytes: Callable[[str], bytes]
) -> None:
    response = client.post(
        "/api/v1/jobs",
        files={"file": ("evil\x00name/../x.jpg", fixture_bytes("landscape.jpg"), "image/jpeg")},
    )

    record = clients.table.get_job(response.json()["job_id"])
    assert record is not None
    assert "\x00" not in record.filename
    assert "/" not in record.filename
    assert ".." not in record.filename


def test_oversized_upload_is_rejected(
    client: Any, fixture_bytes: Callable[[str], bytes], clients: AwsClients
) -> None:
    """MAX_UPLOAD_BYTES is 1 MiB in the test config."""
    oversized = fixture_bytes("landscape.jpg") + b"\x00" * (1_048_576 * 2)

    before = metric_value("pixelforge_uploads_total", {"result": "rejected_too_large"})
    response = client.post("/api/v1/jobs", files={"file": ("huge.jpg", oversized, "image/jpeg")})

    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"
    assert metric_value("pixelforge_uploads_total", {"result": "rejected_too_large"}) == before + 1
    assert clients.queue.receive(max_messages=1, wait_seconds=0) == []


def test_wrong_content_type_is_rejected(client: Any) -> None:
    before = metric_value("pixelforge_uploads_total", {"result": "rejected_content_type"})

    response = upload_fixture(client, "not_an_image.txt", content_type="text/plain")

    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_media_type"
    assert (
        metric_value("pixelforge_uploads_total", {"result": "rejected_content_type"}) == before + 1
    )


def test_corrupt_image_is_rejected(client: Any, clients: AwsClients) -> None:
    """Declared image/jpeg, but the bytes do not decode."""
    before = metric_value("pixelforge_uploads_total", {"result": "rejected_invalid_image"})

    response = upload_fixture(client, "corrupt.jpg")

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_image"
    assert (
        metric_value("pixelforge_uploads_total", {"result": "rejected_invalid_image"}) == before + 1
    )
    # Nothing was stored and nothing was queued.
    assert clients.queue.receive(max_messages=1, wait_seconds=0) == []


def test_text_disguised_as_an_image_is_rejected(client: Any) -> None:
    response = upload_fixture(client, "not_an_image.txt", content_type="image/jpeg")

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_image"


def test_content_type_parameters_are_tolerated(
    client: Any, fixture_bytes: Callable[[str], bytes]
) -> None:
    response = client.post(
        "/api/v1/jobs",
        files={
            "file": ("landscape.jpg", fixture_bytes("landscape.jpg"), "image/jpeg; charset=binary")
        },
    )

    assert response.status_code == 202


def test_missing_file_field_is_a_validation_error(client: Any) -> None:
    response = client.post("/api/v1/jobs", data={"not_a_file": "x"})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_accepted_upload_records_size_and_count_metrics(client: Any) -> None:
    before_count = metric_value("pixelforge_uploads_total", {"result": "accepted"})
    before_observations = metric_value("pixelforge_upload_size_bytes_count")

    upload_fixture(client, "landscape.jpg")

    assert metric_value("pixelforge_uploads_total", {"result": "accepted"}) == before_count + 1
    assert metric_value("pixelforge_upload_size_bytes_count") == before_observations + 1


@pytest.mark.parametrize("name", ["landscape.jpg", "portrait.png", "tiny.png"])
def test_every_supported_fixture_is_accepted(client: Any, name: str) -> None:
    content_type = "image/jpeg" if name.endswith(".jpg") else "image/png"

    assert upload_fixture(client, name, content_type=content_type).status_code == 202
