"""Structured JSON logging to stdout.

Every log line is a single JSON object on stdout carrying at minimum
``timestamp``, ``level``, ``service`` and ``event`` (the message). ``job_id``
and ``trace_id`` are attached automatically when they are bound to the current
context, so call sites do not have to thread them through by hand.

Two safety properties are enforced by processors rather than by convention:

* secrets (anything whose key looks like a credential) are replaced with
  ``"[redacted]"``;
* EXIF GPS data is dropped entirely, so a caller cannot leak a photographer's
  location by logging a metadata dict.

stdlib logging (uvicorn, botocore) is routed through the same renderer so the
container emits exactly one log format.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any, Final

import structlog
from structlog.types import EventDict, WrappedLogger

__all__ = [
    "bind_job_context",
    "clear_job_context",
    "configure_logging",
    "get_logger",
    "redact_exif",
]

#: Substrings that mark a key as sensitive. Matched case-insensitively.
_SECRET_KEY_MARKERS: Final[tuple[str, ...]] = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "access_key",
    "accesskey",
    "session_token",
    "credential",
    "api_key",
    "apikey",
    "private_key",
)

#: EXIF keys that carry location data and must never be logged.
_GPS_KEY_MARKERS: Final[tuple[str, ...]] = ("gps", "geolocation", "latitude", "longitude")

_REDACTED: Final[str] = "[redacted]"

#: Log values longer than this are truncated so a stray payload cannot turn a
#: log line into a file dump.
_MAX_VALUE_CHARS: Final[int] = 512

_NOISY_LOGGERS: Final[tuple[str, ...]] = (
    "botocore",
    "boto3",
    "urllib3",
    "s3transfer",
    "PIL",
)


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _SECRET_KEY_MARKERS)


def _is_gps_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _GPS_KEY_MARKERS)


def redact_exif(exif: MutableMapping[str, Any] | None) -> dict[str, Any]:
    """Return ``exif`` with every location-bearing key removed.

    Args:
        exif: Extracted EXIF metadata, or ``None``.

    Returns:
        A new dict safe to log. Never mutates the input.
    """
    if not exif:
        return {}
    return {key: value for key, value in exif.items() if not _is_gps_key(str(key))}


def _scrub_value(value: Any, depth: int = 0) -> Any:
    """Recursively drop secrets/GPS and truncate oversized values."""
    if depth > 4:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            key: (_REDACTED if _is_secret_key(str(key)) else _scrub_value(val, depth + 1))
            for key, val in value.items()
            if not _is_gps_key(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [_scrub_value(item, depth + 1) for item in value[:32]]
    if isinstance(value, bytes):
        return f"[{len(value)} bytes]"
    if isinstance(value, str) and len(value) > _MAX_VALUE_CHARS:
        return value[:_MAX_VALUE_CHARS] + "…[truncated]"
    return value


def _redaction_processor(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
    """structlog processor enforcing the redaction rules."""
    for key in list(event_dict.keys()):
        if _is_gps_key(key) and key != "event":
            del event_dict[key]
            continue
        if _is_secret_key(key):
            event_dict[key] = _REDACTED
            continue
        event_dict[key] = _scrub_value(event_dict[key])
    return event_dict


def _service_processor(service: str):
    """Build a processor that stamps every line with the service name."""

    def processor(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", service)
        return event_dict

    return processor


def _trace_processor(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
    """Attach the active OpenTelemetry trace id when there is one."""
    from shared.tracing import current_span_ids  # local import: avoids a cycle

    trace_id, span_id = current_span_ids()
    if trace_id:
        event_dict.setdefault("trace_id", trace_id)
    if span_id:
        event_dict.setdefault("span_id", span_id)
    return event_dict


def _rename_message(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
    """Expose the log message under the conventional ``message`` key.

    structlog calls it ``event``; both are emitted so that greps written
    against either convention keep working.
    """
    event = event_dict.get("event")
    if event is not None:
        event_dict["message"] = event
    return event_dict


def configure_logging(service: str, level: str = "INFO") -> None:
    """Configure structlog and stdlib logging for JSON output on stdout.

    Safe to call more than once; the last call wins. Idempotent enough to be
    used from both ``main()`` and test fixtures.

    Args:
        service: Value stamped into the ``service`` field, e.g. ``"api"``.
        level: Minimum level name, e.g. ``"INFO"``.
    """
    numeric_level = logging.getLevelName(level.upper())
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        _service_processor(service),
        _trace_processor,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _redaction_processor,
        _rename_message,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
    )

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(numeric_level)

    # uvicorn installs its own handlers; strip them so nothing bypasses the
    # JSON formatter and writes plain text (or writes to stderr).
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(max(numeric_level, logging.WARNING))


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger."""
    return structlog.get_logger(name)


def bind_job_context(*, job_id: str | None = None, **extra: Any) -> None:
    """Bind fields onto every subsequent log line in this context.

    Uses context variables, so the binding is per-task/per-thread and never
    leaks between concurrently processed jobs.
    """
    if job_id is not None:
        structlog.contextvars.bind_contextvars(job_id=job_id)
    if extra:
        structlog.contextvars.bind_contextvars(**extra)


def clear_job_context() -> None:
    """Drop everything bound by :func:`bind_job_context`."""
    structlog.contextvars.clear_contextvars()
