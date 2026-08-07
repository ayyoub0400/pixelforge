"""Configuration must be complete and valid before anything else happens."""

from __future__ import annotations

import pytest

from shared.config import REQUIRED_VARS, Config, load_config
from shared.errors import ConfigError

BASE_ENV = {
    "AWS_REGION": "eu-west-1",
    "S3_BUCKET": "bucket",
    "SQS_QUEUE_URL": "https://sqs.eu-west-1.amazonaws.com/123456789012/jobs",
    "DYNAMODB_TABLE": "jobs",
}


def test_loads_with_only_required_variables() -> None:
    config = load_config(BASE_ENV)

    assert config.aws_region == "eu-west-1"
    assert config.log_level == "INFO"
    assert config.shutdown_grace_seconds == 30
    assert config.max_upload_bytes == 10_485_760
    assert config.thumbnail_sizes == (150, 400, 800)
    assert config.enable_chaos_endpoint is False
    assert config.otel_exporter_otlp_endpoint is None
    assert config.aws_endpoint_url is None


def test_missing_everything_reports_every_missing_variable() -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config({})

    message = str(excinfo.value)
    for name in REQUIRED_VARS:
        assert name in message, f"{name} should be named in the error"


@pytest.mark.parametrize("omitted", REQUIRED_VARS)
def test_each_required_variable_is_required(omitted: str) -> None:
    env = {key: value for key, value in BASE_ENV.items() if key != omitted}

    with pytest.raises(ConfigError, match=omitted):
        load_config(env)


def test_whitespace_only_counts_as_missing() -> None:
    with pytest.raises(ConfigError, match="S3_BUCKET"):
        load_config({**BASE_ENV, "S3_BUCKET": "   "})


def test_optional_values_are_parsed() -> None:
    config = load_config(
        {
            **BASE_ENV,
            "LOG_LEVEL": "debug",
            "SHUTDOWN_GRACE_SECONDS": "45",
            "MAX_UPLOAD_BYTES": "2048",
            "THUMBNAIL_SIZES": "64, 128 ,256,128",
            "ENABLE_CHAOS_ENDPOINT": "TRUE",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318",
            "AWS_ENDPOINT_URL": "http://localstack:4566",
        }
    )

    assert config.log_level == "DEBUG"
    assert config.shutdown_grace_seconds == 45
    assert config.max_upload_bytes == 2048
    # Sorted and de-duplicated.
    assert config.thumbnail_sizes == (64, 128, 256)
    assert config.enable_chaos_endpoint is True
    assert config.otel_exporter_otlp_endpoint == "http://collector:4318"
    assert config.aws_endpoint_url == "http://localstack:4566"


@pytest.mark.parametrize("value", ["yes", "on", "1"])
def test_truthy_boolean_spellings(value: str) -> None:
    assert load_config({**BASE_ENV, "ENABLE_CHAOS_ENDPOINT": value}).enable_chaos_endpoint


@pytest.mark.parametrize("value", ["no", "off", "0", ""])
def test_falsy_boolean_spellings(value: str) -> None:
    assert not load_config({**BASE_ENV, "ENABLE_CHAOS_ENDPOINT": value}).enable_chaos_endpoint


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("LOG_LEVEL", "chatty", "LOG_LEVEL"),
        ("SHUTDOWN_GRACE_SECONDS", "soon", "integer"),
        ("SHUTDOWN_GRACE_SECONDS", "-1", ">= 0"),
        ("MAX_UPLOAD_BYTES", "0", ">= 1"),
        ("THUMBNAIL_SIZES", "150,big", "integers"),
        ("THUMBNAIL_SIZES", "0", "between"),
        ("THUMBNAIL_SIZES", "99999", "between"),
        ("THUMBNAIL_SIZES", ",,", "at least one"),
        ("ENABLE_CHAOS_ENDPOINT", "perhaps", "boolean"),
    ],
)
def test_invalid_values_fail_fast(name: str, value: str, expected: str) -> None:
    with pytest.raises(ConfigError, match=expected):
        load_config({**BASE_ENV, name: value})


def test_config_is_immutable() -> None:
    config = load_config(BASE_ENV)

    with pytest.raises((AttributeError, TypeError)):
        config.s3_bucket = "somewhere-else"  # type: ignore[misc]


def test_redacted_view_omits_the_account_id_and_never_shows_secrets() -> None:
    config = load_config(BASE_ENV)

    view = config.redacted()

    assert view["sqs_queue_url"] == "jobs"
    assert "123456789012" not in str(view)
    assert view["otel_enabled"] is False


def test_load_config_reads_the_process_environment_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in BASE_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)

    assert load_config().s3_bucket == "bucket"


def test_defaults_match_the_documented_contract() -> None:
    """The CONTRACT section of the README quotes these; keep them in step."""
    defaults = Config(aws_region="r", s3_bucket="b", sqs_queue_url="q", dynamodb_table="t")

    assert defaults.log_level == "INFO"
    assert defaults.shutdown_grace_seconds == 30
    assert defaults.max_upload_bytes == 10_485_760
    assert defaults.thumbnail_sizes == (150, 400, 800)
    assert defaults.enable_chaos_endpoint is False
