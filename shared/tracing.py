"""OpenTelemetry trace context that survives the queue hop.

The API injects the W3C ``traceparent`` header into SQS message attributes and
the worker extracts it, so a single trace spans "upload accepted" through
"thumbnails written" even though the two services never talk directly.

Everything here degrades to a no-op when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is
unset (or when the OpenTelemetry packages are absent). Trace *context* is still
propagated in that case — spans are created against the API's no-op provider,
so no network traffic is generated and no configuration is required in
development.
"""

from __future__ import annotations

import contextlib
from typing import Any, Final, Iterator, Mapping

__all__ = [
    "configure_tracing",
    "get_tracer",
    "inject_trace_context",
    "extract_trace_context",
    "current_span_ids",
    "span",
    "TRACEPARENT_HEADER",
    "TRACESTATE_HEADER",
]

TRACEPARENT_HEADER: Final[str] = "traceparent"
TRACESTATE_HEADER: Final[str] = "tracestate"

try:  # pragma: no cover - exercised by the absence of the dependency
    from opentelemetry import trace as _otel_trace
    from opentelemetry.propagators.textmap import CarrierT  # noqa: F401
    from opentelemetry.trace.propagation.tracecontext import (
        TraceContextTextMapPropagator,
    )

    _OTEL_AVAILABLE = True
    _PROPAGATOR = TraceContextTextMapPropagator()
except Exception:  # pragma: no cover - defensive
    _otel_trace = None  # type: ignore[assignment]
    _OTEL_AVAILABLE = False
    _PROPAGATOR = None  # type: ignore[assignment]

_CONFIGURED = False


def configure_tracing(service_name: str, endpoint: str | None) -> bool:
    """Install a tracer provider that exports to ``endpoint``.

    Args:
        service_name: Value used for the ``service.name`` resource attribute.
        endpoint: OTLP/HTTP collector endpoint. When ``None`` or empty, tracing
            stays a no-op and this function returns ``False``.

    Returns:
        ``True`` when a real exporter was installed.
    """
    global _CONFIGURED

    if not endpoint or not _OTEL_AVAILABLE:
        return False
    if _CONFIGURED:
        return True

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create(
            {"service.name": service_name, "service.namespace": "pixelforge"}
        )
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces"))
        )
        _otel_trace.set_tracer_provider(provider)
        _CONFIGURED = True
        return True
    except Exception:  # pragma: no cover - never let telemetry break the app
        return False


def get_tracer(name: str) -> Any:
    """Return a tracer, or a no-op stand-in when OpenTelemetry is unavailable."""
    if not _OTEL_AVAILABLE:
        return _NoopTracer()
    return _otel_trace.get_tracer(name)


@contextlib.contextmanager
def span(name: str, *, context: Any = None, attributes: Mapping[str, Any] | None = None) -> Iterator[Any]:
    """Start a span, tolerating a completely unconfigured OpenTelemetry stack.

    Args:
        name: Span name.
        context: Parent context, typically from :func:`extract_trace_context`.
        attributes: Initial span attributes.
    """
    tracer = get_tracer("pixelforge")
    with tracer.start_as_current_span(
        name, context=context, attributes=dict(attributes or {})
    ) as current:
        yield current


def inject_trace_context() -> dict[str, str]:
    """Serialise the active span into a W3C carrier.

    Returns:
        A dict with a ``traceparent`` key (and ``tracestate`` when present).
        Empty when there is no active span to propagate.
    """
    if not _OTEL_AVAILABLE:
        return {}
    carrier: dict[str, str] = {}
    _PROPAGATOR.inject(carrier)
    return {key: value for key, value in carrier.items() if value}


def extract_trace_context(carrier: Mapping[str, str] | None) -> Any:
    """Rebuild a parent context from a W3C carrier.

    Args:
        carrier: Mapping containing ``traceparent``/``tracestate``.

    Returns:
        An OpenTelemetry ``Context``, or ``None`` when nothing usable was
        supplied (in which case the caller starts a fresh trace).
    """
    if not _OTEL_AVAILABLE or not carrier:
        return None
    if TRACEPARENT_HEADER not in carrier:
        return None
    try:
        return _PROPAGATOR.extract(dict(carrier))
    except Exception:  # pragma: no cover - malformed header
        return None


def current_span_ids() -> tuple[str | None, str | None]:
    """Return ``(trace_id, span_id)`` as lowercase hex, or ``(None, None)``."""
    if not _OTEL_AVAILABLE:
        return (None, None)
    try:
        context = _otel_trace.get_current_span().get_span_context()
    except Exception:  # pragma: no cover - defensive
        return (None, None)
    if not context or not context.is_valid:
        return (None, None)
    return (format(context.trace_id, "032x"), format(context.span_id, "016x"))


class _NoopSpan:
    """Minimal stand-in implementing the slice of the span API we use."""

    def set_attribute(self, key: str, value: Any) -> None:  # noqa: D102
        return None

    def set_status(self, *args: Any, **kwargs: Any) -> None:  # noqa: D102
        return None

    def record_exception(self, *args: Any, **kwargs: Any) -> None:  # noqa: D102
        return None

    def __enter__(self) -> "_NoopSpan":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class _NoopTracer:
    """Tracer returned when OpenTelemetry is not importable."""

    def start_as_current_span(self, *_args: Any, **_kwargs: Any) -> _NoopSpan:  # noqa: D102
        return _NoopSpan()
