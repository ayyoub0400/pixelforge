"""Environment-driven configuration for every pixelforge process.

Configuration is read *exclusively* from environment variables. There are no
config files, no defaults pointing at a developer machine, and no credential
handling: AWS credentials come from the default boto3 provider chain so that
IAM Roles for Service Accounts works with zero code changes.

A missing or malformed required variable raises :class:`ConfigError` at
startup, which is deliberately fatal — a pod that cannot be configured should
fail its readiness gate loudly rather than serve wrong behaviour quietly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final, Mapping

from shared.errors import ConfigError

__all__ = ["Config", "ConfigError", "load_config", "REQUIRED_VARS", "OPTIONAL_VARS"]

#: Variables that must be present and non-empty for any process to start.
REQUIRED_VARS: Final[tuple[str, ...]] = (
    "AWS_REGION",
    "S3_BUCKET",
    "SQS_QUEUE_URL",
    "DYNAMODB_TABLE",
)

#: Variables that have a documented default (``None`` means "unset is valid").
OPTIONAL_VARS: Final[Mapping[str, str | None]] = {
    "LOG_LEVEL": "INFO",
    "SHUTDOWN_GRACE_SECONDS": "30",
    "MAX_UPLOAD_BYTES": "10485760",
    "THUMBNAIL_SIZES": "150,400,800",
    "ENABLE_CHAOS_ENDPOINT": "false",
    "OTEL_EXPORTER_OTLP_ENDPOINT": None,
    "AWS_ENDPOINT_URL": None,
}

_VALID_LOG_LEVELS: Final[frozenset[str]] = frozenset(
    {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
)

_TRUE_VALUES: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES: Final[frozenset[str]] = frozenset({"0", "false", "no", "off", ""})

#: Upper bound on a single thumbnail edge. Guards against a typo in
#: ``THUMBNAIL_SIZES`` turning every job into a memory exhaustion event.
MAX_THUMBNAIL_EDGE: Final[int] = 8192


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable snapshot of the process environment.

    Instances are created once at startup and passed explicitly; nothing reads
    ``os.environ`` after that point.
    """

    aws_region: str
    s3_bucket: str
    sqs_queue_url: str
    dynamodb_table: str
    log_level: str = "INFO"
    shutdown_grace_seconds: int = 30
    max_upload_bytes: int = 10_485_760
    thumbnail_sizes: tuple[int, ...] = (150, 400, 800)
    enable_chaos_endpoint: bool = False
    otel_exporter_otlp_endpoint: str | None = None
    aws_endpoint_url: str | None = None

    def redacted(self) -> dict[str, object]:
        """Return a log-safe view of the configuration.

        Nothing here is secret today, but the accessor exists so that adding a
        sensitive field later cannot accidentally leak it into a startup log
        line.
        """
        return {
            "aws_region": self.aws_region,
            "s3_bucket": self.s3_bucket,
            "dynamodb_table": self.dynamodb_table,
            "sqs_queue_url": _mask_queue_url(self.sqs_queue_url),
            "log_level": self.log_level,
            "shutdown_grace_seconds": self.shutdown_grace_seconds,
            "max_upload_bytes": self.max_upload_bytes,
            "thumbnail_sizes": list(self.thumbnail_sizes),
            "enable_chaos_endpoint": self.enable_chaos_endpoint,
            "otel_enabled": self.otel_exporter_otlp_endpoint is not None,
            "aws_endpoint_url": self.aws_endpoint_url,
        }


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Build a :class:`Config` from the environment.

    Args:
        env: Mapping to read from. Defaults to :data:`os.environ`; tests pass
            an explicit mapping so they never depend on ambient state.

    Returns:
        A validated, immutable configuration object.

    Raises:
        ConfigError: If a required variable is missing or any value is
            malformed. All missing required variables are reported together so
            an operator fixes them in one pass.
    """
    source: Mapping[str, str] = os.environ if env is None else env

    missing = [name for name in REQUIRED_VARS if not _get(source, name)]
    if missing:
        raise ConfigError(
            "missing required environment variable(s): " + ", ".join(sorted(missing))
        )

    return Config(
        aws_region=_require(source, "AWS_REGION"),
        s3_bucket=_require(source, "S3_BUCKET"),
        sqs_queue_url=_require(source, "SQS_QUEUE_URL"),
        dynamodb_table=_require(source, "DYNAMODB_TABLE"),
        log_level=_log_level(source),
        shutdown_grace_seconds=_int(source, "SHUTDOWN_GRACE_SECONDS", 30, minimum=0),
        max_upload_bytes=_int(source, "MAX_UPLOAD_BYTES", 10_485_760, minimum=1),
        thumbnail_sizes=_thumbnail_sizes(source),
        enable_chaos_endpoint=_bool(source, "ENABLE_CHAOS_ENDPOINT", default=False),
        otel_exporter_otlp_endpoint=_get(source, "OTEL_EXPORTER_OTLP_ENDPOINT"),
        aws_endpoint_url=_get(source, "AWS_ENDPOINT_URL"),
    )


def _get(source: Mapping[str, str], name: str) -> str | None:
    """Return a stripped variable, treating whitespace-only as unset."""
    value = source.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _require(source: Mapping[str, str], name: str) -> str:
    value = _get(source, name)
    if value is None:  # pragma: no cover - guarded by load_config
        raise ConfigError(f"missing required environment variable: {name}")
    return value


def _log_level(source: Mapping[str, str]) -> str:
    raw = _get(source, "LOG_LEVEL") or "INFO"
    level = raw.upper()
    if level not in _VALID_LOG_LEVELS:
        raise ConfigError(
            f"LOG_LEVEL must be one of {sorted(_VALID_LOG_LEVELS)}, got {raw!r}"
        )
    return level


def _int(source: Mapping[str, str], name: str, default: int, *, minimum: int) -> int:
    raw = _get(source, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _bool(source: Mapping[str, str], name: str, *, default: bool) -> bool:
    raw = source.get(name)
    if raw is None:
        return default
    normalised = raw.strip().lower()
    if normalised in _TRUE_VALUES:
        return True
    if normalised in _FALSE_VALUES:
        return False
    raise ConfigError(
        f"{name} must be a boolean (true/false/1/0/yes/no/on/off), got {raw!r}"
    )


def _thumbnail_sizes(source: Mapping[str, str]) -> tuple[int, ...]:
    raw = _get(source, "THUMBNAIL_SIZES") or "150,400,800"
    sizes: set[int] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            size = int(token)
        except ValueError as exc:
            raise ConfigError(
                f"THUMBNAIL_SIZES must be a comma-separated list of integers, got {raw!r}"
            ) from exc
        if size <= 0 or size > MAX_THUMBNAIL_EDGE:
            raise ConfigError(
                f"THUMBNAIL_SIZES entries must be between 1 and {MAX_THUMBNAIL_EDGE}, got {size}"
            )
        sizes.add(size)
    if not sizes:
        raise ConfigError("THUMBNAIL_SIZES must contain at least one size")
    return tuple(sorted(sizes))


def _mask_queue_url(queue_url: str) -> str:
    """Keep the queue name but drop the account id from log output."""
    parts = queue_url.rstrip("/").split("/")
    return parts[-1] if parts else queue_url
