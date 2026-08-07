"""The job status endpoint."""

from __future__ import annotations

import uuid
from typing import Any

from conftest import upload_fixture

from shared.aws import AwsClients
from shared.models import JobStatus, ThumbnailOutput


def test_unknown_job_is_404(client: Any) -> None:
    response = client.get(f"/api/v1/jobs/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["code"] == "job_not_found"


def test_malformed_job_id_is_404_not_500(client: Any) -> None:
    """A non-UUID id is answered without touching DynamoDB."""
    response = client.get("/api/v1/jobs/not-a-uuid")

    assert response.status_code == 404
    assert response.json()["code"] == "job_not_found"


def test_pending_job_reports_submission_details(client: Any) -> None:
    job_id = upload_fixture(client, "landscape.jpg").json()["job_id"]

    body = client.get(f"/api/v1/jobs/{job_id}").json()

    assert body["job_id"] == job_id
    assert body["status"] == "PENDING"
    assert body["filename"] == "landscape.jpg"
    assert body["input_key"] == f"uploads/{job_id}/original.jpg"
    assert body["size_bytes"] > 0
    # Nothing has been rendered yet, so these keys are absent rather than null.
    assert "outputs" not in body
    assert "exif" not in body


def test_completed_job_reports_outputs_and_exif(client: Any, clients: AwsClients) -> None:
    job_id = upload_fixture(client, "landscape.jpg").json()["job_id"]
    clients.table.mark_processing(job_id)
    clients.table.mark_complete(
        job_id,
        outputs={
            "150": ThumbnailOutput(
                size=150, key=f"outputs/{job_id}/thumb_150.jpg", width=150, height=84, bytes=1234
            )
        },
        exif={"Make": "PixelForge", "FNumber": 2.8},
        source_width=1600,
        source_height=900,
        source_format="JPEG",
        processing_ms=412,
    )

    body = client.get(f"/api/v1/jobs/{job_id}").json()

    assert body["status"] == "COMPLETE"
    assert body["outputs"]["150"]["key"] == f"outputs/{job_id}/thumb_150.jpg"
    assert body["outputs"]["150"]["width"] == 150
    assert body["exif"]["Make"] == "PixelForge"
    # Floats survive the Decimal round-trip through DynamoDB.
    assert body["exif"]["FNumber"] == 2.8
    assert body["source_width"] == 1600
    assert body["processing_ms"] == 412
    assert body["completed_at"].endswith("Z")


def test_failed_job_reports_a_reason(client: Any, clients: AwsClients) -> None:
    job_id = upload_fixture(client, "landscape.jpg").json()["job_id"]
    clients.table.mark_failed(job_id, "image could not be processed: truncated")

    body = client.get(f"/api/v1/jobs/{job_id}").json()

    assert body["status"] == JobStatus.FAILED.value
    assert "truncated" in body["error"]


def test_failure_reason_is_truncated(client: Any, clients: AwsClients) -> None:
    job_id = upload_fixture(client, "landscape.jpg").json()["job_id"]
    clients.table.mark_failed(job_id, "x" * 5000)

    body = client.get(f"/api/v1/jobs/{job_id}").json()

    assert len(body["error"]) == 512


def test_status_endpoint_is_labelled_by_route_template(client: Any) -> None:
    """One time series per endpoint, not one per job id."""
    from conftest import metric_value

    job_id = upload_fixture(client, "landscape.jpg").json()["job_id"]
    before = metric_value(
        "pixelforge_http_requests_total",
        {"method": "GET", "endpoint": "/api/v1/jobs/{job_id}", "status": "200"},
    )

    client.get(f"/api/v1/jobs/{job_id}")

    assert (
        metric_value(
            "pixelforge_http_requests_total",
            {"method": "GET", "endpoint": "/api/v1/jobs/{job_id}", "status": "200"},
        )
        == before + 1
    )
