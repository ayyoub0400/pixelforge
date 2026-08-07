"""The chaos endpoint: gated off by default, effective when switched on."""

from __future__ import annotations

import time
from typing import Any

from conftest import upload_fixture

from api.chaos import ChaosController
from shared.models import ChaosRequest


def test_chaos_route_is_absent_when_the_flag_is_false(client: Any) -> None:
    response = client.post("/admin/chaos", json={"error_rate": 1.0})

    assert response.status_code == 404


def test_chaos_route_exists_when_enabled(chaos_client: Any) -> None:
    response = chaos_client.post("/admin/chaos", json={})

    assert response.status_code == 200
    assert response.json() == {"fail_readiness": False, "latency_ms": 0, "error_rate": 0.0}


def test_forced_readiness_failure(chaos_client: Any) -> None:
    assert chaos_client.get("/readyz").status_code == 200

    chaos_client.post("/admin/chaos", json={"fail_readiness": True})

    response = chaos_client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"] == {"chaos": "readiness failure forced"}
    # Liveness is untouched, so the pod leaves the load balancer without being
    # restarted - which is the behaviour the demo is meant to show.
    assert chaos_client.get("/healthz").status_code == 200


def test_error_rate_fails_api_requests(chaos_client: Any) -> None:
    chaos_client.post("/admin/chaos", json={"error_rate": 1.0})

    response = upload_fixture(chaos_client, "landscape.jpg")

    assert response.status_code == 500
    assert response.json()["code"] == "chaos_injected_error"


def test_error_rate_leaves_operational_endpoints_alone(chaos_client: Any) -> None:
    """Health, readiness and metrics must stay honest while chaos is on."""
    chaos_client.post("/admin/chaos", json={"error_rate": 1.0})

    assert chaos_client.get("/healthz").status_code == 200
    assert chaos_client.get("/readyz").status_code == 200
    assert chaos_client.get("/metrics").status_code == 200


def test_injected_latency_is_applied(chaos_client: Any) -> None:
    chaos_client.post("/admin/chaos", json={"latency_ms": 300})

    started = time.perf_counter()
    response = chaos_client.get("/api/v1/jobs/00000000-0000-4000-8000-000000000000")
    elapsed = time.perf_counter() - started

    assert response.status_code == 404
    assert elapsed >= 0.3


def test_settings_merge_rather_than_replace(chaos_client: Any) -> None:
    chaos_client.post("/admin/chaos", json={"latency_ms": 25})

    body = chaos_client.post("/admin/chaos", json={"error_rate": 0.5}).json()

    assert body == {"fail_readiness": False, "latency_ms": 25, "error_rate": 0.5}


def test_injected_failures_are_counted_like_real_ones(chaos_client: Any) -> None:
    from conftest import metric_value

    chaos_client.post("/admin/chaos", json={"error_rate": 1.0})
    labels = {"method": "POST", "endpoint": "/api/v1/jobs", "status": "500"}
    before = metric_value("pixelforge_http_requests_total", labels)

    upload_fixture(chaos_client, "landscape.jpg")

    assert metric_value("pixelforge_http_requests_total", labels) == before + 1


def test_invalid_settings_are_rejected(chaos_client: Any) -> None:
    assert chaos_client.post("/admin/chaos", json={"error_rate": 2.0}).status_code == 422
    assert chaos_client.post("/admin/chaos", json={"latency_ms": -1}).status_code == 422
    assert chaos_client.post("/admin/chaos", json={"latency_ms": 90_000}).status_code == 422
    assert chaos_client.post("/admin/chaos", json={"unknown": 1}).status_code == 422


def test_controller_samples_the_error_rate() -> None:
    samples = iter([0.1, 0.9, 0.49])
    controller = ChaosController(rng=lambda: next(samples))
    controller.apply(ChaosRequest(error_rate=0.5))

    assert controller.should_fail_request() is True
    assert controller.should_fail_request() is False
    assert controller.should_fail_request() is True


def test_controller_short_circuits_the_extremes() -> None:
    def explode() -> float:
        raise AssertionError("rng must not be sampled at 0.0 or 1.0")

    controller = ChaosController(rng=explode)

    assert controller.should_fail_request() is False

    controller.apply(ChaosRequest(error_rate=1.0))
    assert controller.should_fail_request() is True


def test_controller_reset_restores_defaults() -> None:
    controller = ChaosController()
    controller.apply(ChaosRequest(fail_readiness=True, latency_ms=100, error_rate=0.5))

    state = controller.reset()

    assert state.fail_readiness is False
    assert state.latency_ms == 0
    assert state.error_rate == 0.0
