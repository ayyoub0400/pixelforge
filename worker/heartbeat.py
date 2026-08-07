"""Keep an in-flight SQS message invisible while it is being processed.

SQS makes a received message invisible for the queue's visibility timeout. If
processing outlives that window the message is redelivered and a second worker
starts the same job — wasted work at best, and with a low ``maxReceiveCount``
a healthy job can be pushed to the DLQ while it is still being rendered.

The heartbeat extends the window on a timer for as long as the job is running,
and stops the moment the job finishes so a *crashed* worker still releases its
message promptly.
"""

from __future__ import annotations

import threading
from types import TracebackType
from typing import Final

import structlog

from shared.aws import JobQueue

__all__ = ["HEARTBEAT_INTERVAL_SECONDS", "VISIBILITY_EXTENSION_SECONDS", "VisibilityHeartbeat"]

_LOG = structlog.get_logger(__name__)

#: How often the visibility timeout is pushed out.
HEARTBEAT_INTERVAL_SECONDS: Final[int] = 15

#: How far into the future each heartbeat moves the deadline. Comfortably more
#: than the interval so one failed call does not lose the message.
VISIBILITY_EXTENSION_SECONDS: Final[int] = 60


class VisibilityHeartbeat:
    """Context manager that extends a message's visibility timeout.

    Example:
        >>> with VisibilityHeartbeat(queue, handle, job_id=job_id):  # doctest: +SKIP
        ...     render(job)
    """

    def __init__(
        self,
        queue: JobQueue,
        receipt_handle: str,
        *,
        job_id: str,
        interval_seconds: int = HEARTBEAT_INTERVAL_SECONDS,
        extension_seconds: int = VISIBILITY_EXTENSION_SECONDS,
    ) -> None:
        self._queue = queue
        self._receipt_handle = receipt_handle
        self._job_id = job_id
        self._interval = max(1, interval_seconds)
        self._extension = max(self._interval * 2, extension_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.beats = 0

    def start(self) -> None:
        """Begin extending the visibility timeout in a background thread."""
        if self._thread is not None:  # pragma: no cover - defensive
            return
        self._thread = threading.Thread(
            target=self._loop, name=f"heartbeat-{self._job_id[:8]}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop heartbeating and wait briefly for the thread to unwind."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=self._interval + 1)

    def _loop(self) -> None:
        """Extend the deadline every ``interval`` seconds until stopped."""
        while not self._stop.wait(self._interval):
            try:
                self._queue.change_visibility(self._receipt_handle, self._extension)
                self.beats += 1
                _LOG.debug(
                    "visibility_extended",
                    job_id=self._job_id,
                    extension_seconds=self._extension,
                )
            except Exception as exc:
                # A failed extension is not fatal: the job may still finish
                # before the current deadline, and idempotency makes a
                # redelivery harmless.
                _LOG.warning("visibility_extension_failed", job_id=self._job_id, error=str(exc))

    def __enter__(self) -> VisibilityHeartbeat:
        self.start()
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        self.stop()
