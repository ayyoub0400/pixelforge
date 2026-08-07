"""Graceful shutdown for both services."""

from __future__ import annotations

import signal
import threading
from typing import Any

import pytest
from conftest import upload_fixture

import worker.main as worker_main
from api import main as api_main
from shared.aws import AwsClients
from shared.config import Config
from shared.models import JobStatus
from worker.consumer import Worker

# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def test_stop_before_start_means_no_polling(
    worker: Worker, clients: AwsClients, monkeypatch: pytest.MonkeyPatch
) -> None:
    polls = []
    monkeypatch.setattr(clients.queue, "receive", lambda **kwargs: polls.append(kwargs) or [])

    worker.request_stop("SIGTERM")
    worker.run()

    assert worker.stopping is True
    assert polls == []


def test_run_loop_exits_promptly_after_request_stop(worker: Worker) -> None:
    finished = threading.Event()

    def loop() -> None:
        worker.run()
        finished.set()

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    worker.request_stop("SIGTERM")

    assert finished.wait(timeout=5.0), "worker did not stop within the grace period"
    thread.join(timeout=1.0)


def test_in_flight_job_is_finished_before_exiting(
    client: Any, clients: AwsClients, worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGTERM arriving mid-render must not abandon the job."""
    job_id = upload_fixture(client, "landscape.jpg").json()["job_id"]

    original_render = worker._render

    def render_then_signal(job: Any) -> Any:
        # Simulates SIGTERM landing while this job is being rendered.
        worker.request_stop("SIGTERM")
        return original_render(job)

    monkeypatch.setattr(worker, "_render", render_then_signal)
    worker.run_once()

    record = clients.table.get_job(job_id)
    assert record is not None
    assert record.status is JobStatus.COMPLETE, "the in-flight job must still complete"
    assert worker.stopping is True


def test_request_stop_is_idempotent(worker: Worker) -> None:
    worker.request_stop("SIGTERM")
    worker.request_stop("SIGINT")

    assert worker.stopping is True


def test_signal_handler_asks_the_worker_to_stop(
    worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGTERM sets the flag; it must not kill the process outright."""
    forced: list[int] = []
    monkeypatch.setattr(worker_main, "_force_exit", lambda grace: forced.append(grace))

    previous_term = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)
    try:
        worker_main.install_signal_handlers(worker, grace_seconds=30)
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)

        handler(signal.SIGTERM, None)  # type: ignore[operator]

        assert worker.stopping is True
        # The watchdog is armed but has not fired: the job gets its grace.
        assert forced == []
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


def test_watchdog_exits_when_the_grace_period_is_exhausted(
    worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job that hangs past the grace period must not block the shutdown."""
    forced: list[int] = []
    monkeypatch.setattr(worker_main, "_force_exit", lambda grace: forced.append(grace))

    previous_term = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)
    try:
        worker_main.install_signal_handlers(worker, grace_seconds=0.05)  # type: ignore[arg-type]
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)  # type: ignore[operator]

        deadline = threading.Event()
        deadline.wait(0.5)

        assert forced == [0.05]
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def test_api_configures_a_graceful_drain(
    monkeypatch: pytest.MonkeyPatch, config: Config, clients: AwsClients
) -> None:
    """uvicorn must be told to drain in-flight requests for the grace period."""
    captured: dict[str, Any] = {}

    def fake_run(app: Any, **kwargs: Any) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(api_main.uvicorn, "run", fake_run)
    monkeypatch.setattr(api_main, "load_config", lambda: config)
    monkeypatch.setattr(
        api_main, "AwsClients", type("_", (), {"build": staticmethod(lambda _c: clients)})
    )

    api_main.run()

    assert captured["timeout_graceful_shutdown"] == config.shutdown_grace_seconds
    assert captured["port"] == 8000
    assert captured["host"] == "0.0.0.0"


def test_api_lifespan_runs_startup_and_shutdown(config: Config, clients: AwsClients) -> None:
    from fastapi.testclient import TestClient

    app = api_main.create_app(config=config, clients=clients)

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert app.state.service is not None

    # The lifespan shutdown ran without raising; state survives for inspection.
    assert app.state.config is config


def test_missing_configuration_exits_with_ex_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pod that cannot be configured must say so and exit, not limp on."""
    for name in ("AWS_REGION", "S3_BUCKET", "SQS_QUEUE_URL", "DYNAMODB_TABLE"):
        monkeypatch.delenv(name, raising=False)

    assert api_main.main() == 78
    assert "invalid configuration" in capsys.readouterr().err


def test_worker_missing_configuration_exits_with_ex_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("AWS_REGION", "S3_BUCKET", "SQS_QUEUE_URL", "DYNAMODB_TABLE"):
        monkeypatch.delenv(name, raising=False)

    assert worker_main.main() == 78
    assert "invalid configuration" in capsys.readouterr().err


def test_worker_run_serves_metrics_then_consumes(
    monkeypatch: pytest.MonkeyPatch, config: Config, clients: AwsClients
) -> None:
    """Metrics must be scrapeable before the first message is touched."""
    served: list[int] = []
    monkeypatch.setattr(worker_main, "load_config", lambda: config)
    monkeypatch.setattr(
        worker_main, "start_http_server", lambda port, **_kwargs: served.append(port)
    )

    def build(cfg: Config) -> Worker:
        instance = Worker(cfg, clients, poll_wait_seconds=0, sleep=lambda _s: None)
        instance.request_stop("test")  # exit after zero iterations
        return instance

    monkeypatch.setattr(worker_main, "build_worker", build)

    previous_term = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)
    try:
        assert worker_main.run() == 0
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)

    assert served == [worker_main.METRICS_PORT] == [9090]


def test_worker_run_exits_non_zero_when_dependencies_never_appear(
    monkeypatch: pytest.MonkeyPatch, config: Config, broken_clients: AwsClients
) -> None:
    monkeypatch.setattr(worker_main, "load_config", lambda: config)
    monkeypatch.setattr(worker_main, "start_http_server", lambda _port, **_kwargs: None)
    monkeypatch.setattr(
        worker_main,
        "AwsClients",
        type("_", (), {"build": staticmethod(lambda _c: broken_clients)}),
    )
    monkeypatch.setattr(
        worker_main,
        "wait_for_dependencies",
        lambda clients_: (_ for _ in ()).throw(
            __import__("shared.errors", fromlist=["x"]).TransientDependencyError("down")
        ),
    )

    assert worker_main.run() == 1


def test_worker_startup_waits_for_dependencies(config: Config, broken_clients: AwsClients) -> None:
    from shared.aws import wait_for_dependencies
    from shared.errors import TransientDependencyError

    slept: list[float] = []

    with pytest.raises(TransientDependencyError):
        wait_for_dependencies(broken_clients, attempts=3, base_delay=0.01, sleep=slept.append)

    # It backed off between attempts instead of hammering or crash-looping.
    assert len(slept) == 2


def test_worker_startup_returns_once_dependencies_answer(clients: AwsClients) -> None:
    from shared.aws import wait_for_dependencies

    slept: list[float] = []
    wait_for_dependencies(clients, attempts=3, base_delay=0.01, sleep=slept.append)

    assert slept == []


def test_visibility_heartbeat_extends_in_flight_messages(clients: AwsClients, client: Any) -> None:
    """Long jobs must not be redelivered while they are still being processed."""
    from worker.heartbeat import VisibilityHeartbeat

    upload_fixture(client, "landscape.jpg")
    message = clients.queue.receive(max_messages=1, wait_seconds=0)[0]
    extensions: list[tuple[str, int]] = []

    class RecordingQueue:
        def change_visibility(self, handle: str, timeout: int) -> None:
            extensions.append((handle, timeout))

    heartbeat = VisibilityHeartbeat(
        RecordingQueue(),  # type: ignore[arg-type]
        message["ReceiptHandle"],
        job_id="test-job",
        interval_seconds=1,
        extension_seconds=90,
    )
    with heartbeat:
        threading.Event().wait(1.3)

    assert extensions, "the heartbeat should have extended the visibility timeout"
    assert extensions[0][1] == 90
    assert heartbeat.beats >= 1


def test_heartbeat_failures_do_not_propagate(clients: AwsClients) -> None:
    from worker.heartbeat import VisibilityHeartbeat

    class BrokenQueue:
        def change_visibility(self, handle: str, timeout: int) -> None:
            raise RuntimeError("sqs is down")

    heartbeat = VisibilityHeartbeat(
        BrokenQueue(),  # type: ignore[arg-type]
        "handle",
        job_id="test-job",
        interval_seconds=1,
        extension_seconds=90,
    )
    with heartbeat:
        threading.Event().wait(1.2)

    assert heartbeat.beats == 0
