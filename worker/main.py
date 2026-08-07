"""Entrypoint for the worker service.

Run with ``python -m worker.main``. The process is PID 1 in its container and
installs its own SIGTERM handler, so a rolling update stops the worker picking
up new messages, lets the in-flight job finish, and exits cleanly — SQS never
has to redeliver work that was already half done.

There is no ingress port. Prometheus metrics are served on ``9090``.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from types import FrameType
from typing import Final

import structlog
from prometheus_client import start_http_server

from shared.aws import AwsClients, wait_for_dependencies
from shared.config import Config, load_config
from shared.errors import ConfigError, TransientDependencyError
from shared.logging_setup import configure_logging
from shared.tracing import configure_tracing
from worker.consumer import SERVICE_NAME, Worker

__all__ = ["run", "main", "install_signal_handlers", "METRICS_PORT"]

_LOG = structlog.get_logger(__name__)

#: Prometheus scrape port for the worker. Part of the published contract.
METRICS_PORT: Final[int] = 9090

#: Exit code used when the grace period elapses with a job still in flight.
_EXIT_GRACE_EXCEEDED: Final[int] = 1

#: sysexits.h EX_CONFIG: the process was started with bad configuration.
_EXIT_CONFIG: Final[int] = 78


def install_signal_handlers(worker: Worker, grace_seconds: int) -> None:
    """Route SIGTERM/SIGINT into a graceful stop with a hard deadline.

    The handler does almost nothing — it sets a flag the poll loop checks — so
    it is safe to run at any point in the loop. A watchdog timer guarantees the
    process exits even if the in-flight job hangs, because a pod that ignores
    SIGTERM is only ever going to be SIGKILLed a moment later.

    Args:
        worker: The worker to stop.
        grace_seconds: How long the in-flight job may take to finish.
    """

    def handle(signum: int, _frame: FrameType | None) -> None:
        name = signal.Signals(signum).name
        _LOG.info("shutdown_signal_received", signal=name, grace_seconds=grace_seconds)
        worker.request_stop(reason=name)
        watchdog = threading.Timer(grace_seconds, _force_exit, args=(grace_seconds,))
        watchdog.daemon = True
        watchdog.start()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)


def _force_exit(grace_seconds: int) -> None:
    """Terminate immediately once the grace period is spent."""
    _LOG.error("shutdown_grace_exceeded", grace_seconds=grace_seconds)
    # os._exit skips atexit handlers on purpose: the point of this path is that
    # normal unwinding has already failed to finish in time.
    os._exit(_EXIT_GRACE_EXCEEDED)


def build_worker(config: Config) -> Worker:
    """Construct a worker and block until its dependencies are reachable."""
    clients = AwsClients.build(config)
    wait_for_dependencies(clients)
    return Worker(config, clients)


def run() -> int:
    """Configure the process, serve metrics, and consume until SIGTERM.

    Returns:
        A process exit code.
    """
    config = load_config()
    configure_logging(SERVICE_NAME, config.log_level)
    configure_tracing(SERVICE_NAME, config.otel_exporter_otlp_endpoint)

    _LOG.info("service_starting", metrics_port=METRICS_PORT, **config.redacted())
    start_http_server(METRICS_PORT)

    try:
        worker = build_worker(config)
    except TransientDependencyError as exc:
        _LOG.error("startup_dependencies_unavailable", error=str(exc))
        return _EXIT_GRACE_EXCEEDED

    install_signal_handlers(worker, config.shutdown_grace_seconds)
    worker.run()
    return 0


def main() -> int:
    """Console entrypoint."""
    try:
        return run()
    except ConfigError as exc:
        print(f"FATAL: invalid configuration: {exc}", file=sys.stderr, flush=True)
        return _EXIT_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())
