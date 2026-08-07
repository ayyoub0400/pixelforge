"""Liveness, readiness and the metrics endpoint."""

from __future__ import annotations

from typing import Any

from conftest import metric_value
from fastapi.testclient import TestClient

from api.main import create_app
from shared.aws import AwsClients
from shared.config import Config
from shared.metrics import METRICS_CONTENT_TYPE


def test_healthz_is_200(client: Any) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "api"}


def test_healthz_ignores_broken_dependencies(config: Config, broken_clients: AwsClients) -> None:
    """Liveness must never depend on AWS, or one outage restarts the fleet."""
    with TestClient(create_app(config=config, clients=broken_clients)) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503


def test_readyz_reports_every_dependency(client: Any) -> None:
    response = client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"] == {"s3": "ok", "sqs": "ok", "dynamodb": "ok"}


def test_readyz_names_the_failing_dependency(config: Config, broken_clients: AwsClients) -> None:
    with TestClient(create_app(config=config, clients=broken_clients)) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert any(result != "ok" for result in body["checks"].values())


def test_metrics_endpoint_exposes_the_api_contract_names(client: Any) -> None:
    client.get("/healthz")

    response = client.get("/metrics")

    assert response.status_code == 200
    assert METRICS_CONTENT_TYPE.split(";")[0] in response.headers["content-type"]
    body = response.text
    for name in (
        "pixelforge_http_requests_total",
        "pixelforge_http_request_duration_seconds",
        "pixelforge_uploads_total",
        "pixelforge_upload_size_bytes",
        # Emitted by both services: the API sends, the worker consumes.
        "pixelforge_sqs_errors_total",
    ):
        assert name in body, f"{name} missing from the API exposition"


def test_api_does_not_expose_worker_metrics(client: Any) -> None:
    """Separate registries, so a dashboard cannot pick up an empty API series."""
    body = client.get("/metrics").text

    for name in (
        "pixelforge_jobs_processed_total",
        "pixelforge_job_duration_seconds",
        "pixelforge_jobs_inflight",
        "pixelforge_job_stage_duration_seconds",
    ):
        assert name not in body, f"{name} should only be exposed by the worker"


def test_worker_registry_exposes_the_worker_contract_names() -> None:
    from shared.metrics import WORKER_REGISTRY, render_metrics

    body = render_metrics(WORKER_REGISTRY)[0].decode()

    for name in (
        "pixelforge_jobs_processed_total",
        "pixelforge_job_duration_seconds",
        "pixelforge_jobs_inflight",
        "pixelforge_job_stage_duration_seconds",
        "pixelforge_sqs_errors_total",
    ):
        assert name in body, f"{name} missing from the worker exposition"
    assert "pixelforge_http_requests_total" not in body


def test_request_metrics_are_recorded(client: Any) -> None:
    labels = {"method": "GET", "endpoint": "/healthz", "status": "200"}
    before = metric_value("pixelforge_http_requests_total", labels)
    before_duration = metric_value(
        "pixelforge_http_request_duration_seconds_count", {"endpoint": "/healthz"}
    )

    client.get("/healthz")

    assert metric_value("pixelforge_http_requests_total", labels) == before + 1
    assert (
        metric_value("pixelforge_http_request_duration_seconds_count", {"endpoint": "/healthz"})
        == before_duration + 1
    )


def test_unmatched_paths_share_one_time_series(client: Any) -> None:
    """A scanner hitting random paths must not explode metric cardinality."""
    before = metric_value(
        "pixelforge_http_requests_total",
        {"method": "GET", "endpoint": "unmatched", "status": "404"},
    )

    client.get("/nope/one")
    client.get("/nope/two")

    assert (
        metric_value(
            "pixelforge_http_requests_total",
            {"method": "GET", "endpoint": "unmatched", "status": "404"},
        )
        == before + 2
    )
