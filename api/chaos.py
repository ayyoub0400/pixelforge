"""Demo controls for failure injection.

This is the one piece of deliberately global mutable state in the codebase. It
exists so that a rollback, an alert, or a readiness-driven traffic shift can be
demonstrated on a live deployment without pushing a broken build.

The whole surface is gated behind ``ENABLE_CHAOS_ENDPOINT``. When that flag is
false — the default, and the expected setting in production — the route is
never registered and the controller stays at its inert defaults.

State is per-process. With more than one API replica, a chaos setting applies
only to the pod that received the ``POST``; that is usually what you want for a
demo (one sick pod, N healthy ones) but it is worth knowing before wondering
why only some requests are slow.
"""

from __future__ import annotations

import asyncio
import random
import threading
from collections.abc import Callable

import structlog
from starlette.requests import Request

from shared.models import ChaosRequest, ChaosState

__all__ = ["ChaosController", "ChaosInjectedError", "apply_chaos"]

_LOG = structlog.get_logger(__name__)


class ChaosController:
    """Thread-safe holder for the injected-failure settings."""

    def __init__(self, rng: Callable[[], float] | None = None) -> None:
        self._lock = threading.Lock()
        self._state = ChaosState()
        self._rng = rng or random.random

    def snapshot(self) -> ChaosState:
        """Return a copy of the current settings."""
        with self._lock:
            return self._state.model_copy()

    def apply(self, request: ChaosRequest) -> ChaosState:
        """Merge a partial update into the current settings.

        Fields left unset in ``request`` keep their current value, so a caller
        can nudge one knob without restating the others.

        Args:
            request: The validated request body.

        Returns:
            The settings now in force.
        """
        with self._lock:
            updates = request.model_dump(exclude_none=True)
            self._state = self._state.model_copy(update=updates)
            return self._state.model_copy()

    def reset(self) -> ChaosState:
        """Restore inert defaults. Used by tests and by ``make chaos-reset``."""
        with self._lock:
            self._state = ChaosState()
            return self._state.model_copy()

    @property
    def fail_readiness(self) -> bool:
        """Whether ``/readyz`` should report 503 without probing AWS."""
        with self._lock:
            return self._state.fail_readiness

    @property
    def latency_seconds(self) -> float:
        """Artificial delay to add to a request, in seconds."""
        with self._lock:
            return self._state.latency_ms / 1000.0

    def should_fail_request(self) -> bool:
        """Sample the configured error rate for a single request."""
        with self._lock:
            rate = self._state.error_rate
        if rate <= 0.0:
            return False
        if rate >= 1.0:
            return True
        return self._rng() < rate


class ChaosInjectedError(Exception):
    """Raised to fail a request on purpose. Handled as a ``500``."""


async def apply_chaos(request: Request) -> None:
    """Router dependency that applies the configured latency and error rate.

    Attached to the ``/api/v1`` router rather than installed as middleware for
    two reasons: it applies to exactly the job API (health, readiness and
    metrics stay honest while chaos is switched on), and because the request
    has already been routed, the resulting ``500`` is attributed to the right
    endpoint in ``pixelforge_http_requests_total`` instead of collapsing into
    the ``unmatched`` series. Making the dashboards move correctly is the whole
    point of the feature.

    Raises:
        ChaosInjectedError: The configured error rate fired for this request.
    """
    controller: ChaosController = request.app.state.chaos

    latency = controller.latency_seconds
    if latency > 0:
        await asyncio.sleep(latency)

    if controller.should_fail_request():
        _LOG.warning("chaos_injected_error", path=request.url.path)
        raise ChaosInjectedError("injected failure")
