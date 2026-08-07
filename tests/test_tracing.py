"""W3C trace context has to survive the queue hop."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import upload_fixture
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from shared.aws import AwsClients
from shared.tracing import (
    TRACEPARENT_HEADER,
    configure_tracing,
    current_span_ids,
    extract_trace_context,
    inject_trace_context,
)
from worker.consumer import Worker


@pytest.fixture
def tracer() -> Iterator[Any]:
    """A real SDK tracer, without touching the global provider."""
    provider = TracerProvider()
    yield provider.get_tracer("tests")


def test_no_active_span_injects_nothing() -> None:
    assert inject_trace_context() == {}


def test_active_span_is_serialised_as_traceparent(tracer: Any) -> None:
    with tracer.start_as_current_span("outer"):
        carrier = inject_trace_context()
        trace_id, _ = current_span_ids()

    assert TRACEPARENT_HEADER in carrier
    assert carrier[TRACEPARENT_HEADER].startswith("00-")
    assert trace_id is not None and trace_id in carrier[TRACEPARENT_HEADER]


def test_carrier_round_trips(tracer: Any) -> None:
    with tracer.start_as_current_span("outer"):
        carrier = inject_trace_context()
        expected_trace_id, _ = current_span_ids()

    context = extract_trace_context(carrier)
    assert context is not None

    span_context = trace.get_current_span(context).get_span_context()
    assert format(span_context.trace_id, "032x") == expected_trace_id


def test_extract_tolerates_missing_or_broken_headers() -> None:
    """A malformed header must start a fresh trace, not blow up the worker."""
    assert extract_trace_context(None) is None
    assert extract_trace_context({}) is None

    context = extract_trace_context({TRACEPARENT_HEADER: "not-a-traceparent"})

    # Either no context at all, or a context whose span context is invalid;
    # both mean "no parent to continue".
    if context is not None:
        assert not trace.get_current_span(context).get_span_context().is_valid


def test_api_puts_the_traceparent_on_the_sqs_message(
    client: Any, clients: AwsClients, tracer: Any
) -> None:
    """The API side of the contract: trace context leaves with the message."""
    with tracer.start_as_current_span("client-request"):
        expected_trace_id, _ = current_span_ids()
        upload_fixture(client, "landscape.jpg")

    message = clients.queue.receive(max_messages=1, wait_seconds=0)[0]
    attributes = message["MessageAttributes"]

    assert TRACEPARENT_HEADER in attributes
    assert attributes[TRACEPARENT_HEADER]["DataType"] == "String"
    assert expected_trace_id in attributes[TRACEPARENT_HEADER]["StringValue"]


def test_worker_continues_the_trace_started_by_the_api(
    client: Any, clients: AwsClients, worker: Worker, tracer: Any
) -> None:
    """The worker side: the same trace id continues after the queue hop."""
    with tracer.start_as_current_span("client-request"):
        job_id = upload_fixture(client, "landscape.jpg").json()["job_id"]
        expected_trace_id, _ = current_span_ids()

    message = clients.queue.receive(max_messages=1, wait_seconds=0)[0]
    carrier = {name: value["StringValue"] for name, value in message["MessageAttributes"].items()}
    context = extract_trace_context(carrier)

    assert context is not None
    assert (
        format(trace.get_current_span(context).get_span_context().trace_id, "032x")
        == expected_trace_id
    )

    # And the worker still processes it normally.
    assert worker.handle_message(message).value == "completed"
    record = clients.table.get_job(job_id)
    assert record is not None and record.trace_id == expected_trace_id


def test_trace_id_is_recorded_on_the_job(client: Any, clients: AwsClients, tracer: Any) -> None:
    with tracer.start_as_current_span("client-request"):
        job_id = upload_fixture(client, "landscape.jpg").json()["job_id"]
        expected_trace_id, _ = current_span_ids()

    record = clients.table.get_job(job_id)
    assert record is not None
    assert record.trace_id == expected_trace_id


def test_message_body_carries_no_telemetry(client: Any, clients: AwsClients, tracer: Any) -> None:
    """Trace context belongs in attributes; the body stays a data contract."""
    with tracer.start_as_current_span("client-request"):
        upload_fixture(client, "landscape.jpg")

    body = json.loads(clients.queue.receive(max_messages=1, wait_seconds=0)[0]["Body"])

    assert TRACEPARENT_HEADER not in body
    assert set(body) == {
        "schema_version",
        "job_id",
        "bucket",
        "input_key",
        "content_type",
        "filename",
        "submitted_at",
    }


def test_tracing_is_a_no_op_without_an_endpoint() -> None:
    """Unset OTEL_EXPORTER_OTLP_ENDPOINT must degrade silently, not raise."""
    assert configure_tracing("api", None) is False
    assert configure_tracing("api", "") is False


def test_spans_work_without_a_configured_provider() -> None:
    from shared.tracing import span

    with span("standalone", attributes={"job.id": "abc"}) as current:
        current.set_attribute("extra", 1)

    # No exporter, no provider, no exception.
    assert True
