"""Exponential-backoff retry helpers for AWS calls.

botocore already retries a subset of errors internally; this module adds a
second, explicit layer around the thin wrappers in :mod:`shared.aws` so that a
dependency being briefly unavailable at *startup* or mid-flight surfaces as a
:class:`~shared.errors.TransientDependencyError` after a bounded number of
attempts instead of crash-looping the pod on the first connection refusal.

Only errors that can plausibly succeed on a retry are retried. Permanent
errors (``AccessDenied``, ``ValidationException``, ``NoSuchKey``,
``ConditionalCheckFailedException``) are re-raised immediately so that a
misconfiguration is visible in seconds rather than after a backoff ladder.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, Final, Iterable, TypeVar

import structlog
from botocore.exceptions import BotoCoreError, ClientError

from shared.errors import TransientDependencyError

__all__ = ["RetryPolicy", "DEFAULT_POLICY", "STARTUP_POLICY", "call_with_retry", "is_transient"]

T = TypeVar("T")

_LOG = structlog.get_logger(__name__)

#: Error codes that mean "the service said try again later".
TRANSIENT_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "InternalError",
        "InternalFailure",
        "InternalServerError",
        "ProvisionedThroughputExceededException",
        "RequestLimitExceeded",
        "RequestThrottled",
        "RequestThrottledException",
        "RequestTimeout",
        "RequestTimeoutException",
        "ServiceUnavailable",
        "SlowDown",
        "ThrottledException",
        "Throttling",
        "ThrottlingException",
        "TooManyRequestsException",
        "TransactionInProgressException",
        "503",
        "500",
    }
)

#: botocore exception class names that always mean "connectivity problem".
_TRANSIENT_BOTOCORE_ERRORS: Final[frozenset[str]] = frozenset(
    {
        "ConnectionClosedError",
        "ConnectTimeoutError",
        "EndpointConnectionError",
        "IncompleteReadError",
        "ReadTimeoutError",
        "ResponseStreamingError",
    }
)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded exponential backoff with full jitter.

    Attributes:
        attempts: Total number of tries, including the first.
        base_delay: Delay before the second attempt, in seconds.
        max_delay: Ceiling applied to every computed delay.
    """

    attempts: int = 4
    base_delay: float = 0.2
    max_delay: float = 5.0

    def delay_for(self, attempt: int) -> float:
        """Return the sleep duration before ``attempt`` (1-based) is retried."""
        exponential = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        return random.uniform(0.0, exponential)


#: Used for per-request calls, where a slow retry ladder costs latency.
DEFAULT_POLICY: Final[RetryPolicy] = RetryPolicy(attempts=4, base_delay=0.2, max_delay=5.0)

#: Used while waiting for dependencies at boot, where patience is free and a
#: crash-loop is expensive.
STARTUP_POLICY: Final[RetryPolicy] = RetryPolicy(attempts=8, base_delay=0.5, max_delay=15.0)


def is_transient(exc: BaseException) -> bool:
    """Report whether ``exc`` is worth retrying.

    Args:
        exc: The exception raised by a boto3 call.

    Returns:
        ``True`` for throttling, 5xx responses and connectivity errors;
        ``False`` for anything the caller sent wrong.
    """
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        code = str(error.get("Code", ""))
        status = int(
            exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0
        )
        return code in TRANSIENT_ERROR_CODES or status >= 500 or status == 429
    if isinstance(exc, BotoCoreError):
        return type(exc).__name__ in _TRANSIENT_BOTOCORE_ERRORS
    return False


def call_with_retry(
    func: Callable[..., T],
    *args: object,
    operation: str,
    policy: RetryPolicy = DEFAULT_POLICY,
    sleep: Callable[[float], None] = time.sleep,
    swallow_codes: Iterable[str] = (),
    **kwargs: object,
) -> T:
    """Invoke ``func`` with exponential backoff on transient AWS failures.

    Args:
        func: The boto3 call to make.
        *args: Positional arguments forwarded to ``func``.
        operation: Short label used in logs and metrics, e.g. ``"s3.get_object"``.
        policy: Backoff policy to apply.
        sleep: Injected for tests so they never actually wait.
        swallow_codes: ``ClientError`` codes to treat as success, returning
            ``None``. Used by readiness probes that accept ``AccessDenied`` as
            proof the dependency is reachable.
        **kwargs: Keyword arguments forwarded to ``func``.

    Returns:
        Whatever ``func`` returns.

    Raises:
        TransientDependencyError: Every attempt failed with a retryable error.
        Exception: Any non-retryable error is re-raised unchanged.
    """
    swallowed = frozenset(swallow_codes)
    last_exc: BaseException | None = None

    for attempt in range(1, policy.attempts + 1):
        try:
            return func(*args, **kwargs)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in swallowed:
                return None  # type: ignore[return-value]
            if not is_transient(exc):
                raise
            last_exc = exc
        except BotoCoreError as exc:
            if not is_transient(exc):
                raise
            last_exc = exc

        if attempt < policy.attempts:
            delay = policy.delay_for(attempt)
            _LOG.warning(
                "aws_call_retrying",
                operation=operation,
                attempt=attempt,
                max_attempts=policy.attempts,
                delay_seconds=round(delay, 3),
                error=str(last_exc),
            )
            sleep(delay)

    raise TransientDependencyError(
        f"{operation} failed after {policy.attempts} attempts: {last_exc}",
        operation=operation,
    ) from last_exc
