"""Retry classification and backoff around AWS calls."""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ParamValidationError,
)

from shared.errors import TransientDependencyError
from shared.retry import DEFAULT_POLICY, RetryPolicy, call_with_retry, is_transient


def _client_error(code: str, status: int = 400) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "SomeOperation",
    )


@pytest.mark.parametrize(
    "code",
    ["ThrottlingException", "ProvisionedThroughputExceededException", "SlowDown", "InternalError"],
)
def test_throttling_and_5xx_are_transient(code: str) -> None:
    assert is_transient(_client_error(code)) is True


def test_server_errors_are_transient_regardless_of_code() -> None:
    assert is_transient(_client_error("SomethingNew", status=503)) is True


def test_client_errors_are_not_transient() -> None:
    assert is_transient(_client_error("AccessDenied", status=403)) is False
    assert is_transient(_client_error("NoSuchKey", status=404)) is False
    assert is_transient(_client_error("ValidationException", status=400)) is False
    assert is_transient(_client_error("ConditionalCheckFailedException", status=400)) is False


def test_connectivity_errors_are_transient() -> None:
    assert is_transient(EndpointConnectionError(endpoint_url="http://x")) is True
    assert is_transient(ConnectTimeoutError(endpoint_url="http://x")) is True


def test_unrelated_exceptions_are_not_transient() -> None:
    assert is_transient(ValueError("nope")) is False
    assert is_transient(ParamValidationError(report="bad params")) is False


def test_success_on_the_first_attempt_does_not_sleep() -> None:
    slept: list[float] = []

    result = call_with_retry(lambda: "ok", operation="test.call", sleep=slept.append)

    assert result == "ok"
    assert slept == []


def test_transient_failures_are_retried_then_succeed() -> None:
    attempts = {"count": 0}
    slept: list[float] = []

    def flaky() -> str:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise _client_error("ThrottlingException", status=400)
        return "ok"

    result = call_with_retry(
        flaky,
        operation="test.call",
        policy=RetryPolicy(attempts=5, base_delay=0.01, max_delay=0.1),
        sleep=slept.append,
    )

    assert result == "ok"
    assert attempts["count"] == 3
    assert len(slept) == 2


def test_permanent_failures_are_raised_immediately() -> None:
    attempts = {"count": 0}

    def denied() -> None:
        attempts["count"] += 1
        raise _client_error("AccessDenied", status=403)

    with pytest.raises(ClientError):
        call_with_retry(denied, operation="test.call", sleep=lambda _s: None)

    assert attempts["count"] == 1, "a permanent error must not be retried"


def test_exhausted_retries_raise_transient_dependency_error() -> None:
    def always_throttled() -> None:
        raise _client_error("ThrottlingException")

    with pytest.raises(TransientDependencyError) as excinfo:
        call_with_retry(
            always_throttled,
            operation="dynamodb.get_item",
            policy=RetryPolicy(attempts=3, base_delay=0.001, max_delay=0.01),
            sleep=lambda _s: None,
        )

    assert excinfo.value.operation == "dynamodb.get_item"
    assert "3 attempts" in str(excinfo.value)


def test_swallowed_codes_are_treated_as_success() -> None:
    """Readiness probes accept a 403 as proof the dependency answered."""

    def denied() -> None:
        raise _client_error("AccessDenied", status=403)

    assert (
        call_with_retry(
            denied,
            operation="s3.head_bucket",
            swallow_codes=("AccessDenied",),
            sleep=lambda _s: None,
        )
        is None
    )


def test_backoff_grows_and_is_capped() -> None:
    policy = RetryPolicy(attempts=8, base_delay=1.0, max_delay=4.0)

    # Full jitter: each delay is bounded by the exponential ceiling.
    for attempt, ceiling in [(1, 1.0), (2, 2.0), (3, 4.0), (6, 4.0)]:
        for _ in range(20):
            assert 0.0 <= policy.delay_for(attempt) <= ceiling


def test_default_policy_is_bounded() -> None:
    """A per-request retry ladder must not add unbounded latency."""
    worst_case = sum(DEFAULT_POLICY.max_delay for _ in range(DEFAULT_POLICY.attempts - 1))

    assert DEFAULT_POLICY.attempts <= 5
    assert worst_case <= 20.0


def test_kwargs_are_forwarded() -> None:
    def call(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"args": args, "kwargs": kwargs}

    result = call_with_retry(call, "positional", operation="test.call", Bucket="b", Key="k")

    assert result == {"args": ("positional",), "kwargs": {"Bucket": "b", "Key": "k"}}
