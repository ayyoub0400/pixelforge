"""Structured logging: shape, context and the redaction rules."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
import structlog

from shared.logging_setup import (
    bind_job_context,
    clear_job_context,
    configure_logging,
    get_logger,
    redact_exif,
)


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    """Restore the default logging configuration after each test."""
    yield
    clear_job_context()
    structlog.reset_defaults()
    logging.getLogger().handlers = []


def _emit(capsys: pytest.CaptureFixture[str], **fields: Any) -> dict[str, Any]:
    """Log one line and return it parsed."""
    get_logger("test").info("something_happened", **fields)
    captured = capsys.readouterr().out.strip().splitlines()
    assert captured, "expected a log line on stdout"
    return json.loads(captured[-1])


def test_log_lines_are_json_on_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("api", "INFO")

    line = _emit(capsys)

    assert line["service"] == "api"
    assert line["level"] == "info"
    assert line["message"] == "something_happened"
    assert line["event"] == "something_happened"
    assert line["timestamp"].endswith("Z") or "T" in line["timestamp"]


def test_nothing_is_written_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("worker", "INFO")

    get_logger("test").warning("careful")

    assert capsys.readouterr().err == ""


def test_job_id_is_attached_from_context(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("worker", "INFO")
    bind_job_context(job_id="job-123")

    line = _emit(capsys)

    assert line["job_id"] == "job-123"


def test_context_is_cleared_between_jobs(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("worker", "INFO")
    bind_job_context(job_id="job-123")
    clear_job_context()

    line = _emit(capsys)

    assert "job_id" not in line


def test_log_level_is_respected(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("api", "WARNING")

    get_logger("test").info("quiet")
    get_logger("test").warning("loud")

    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["message"] == "loud"


@pytest.mark.parametrize(
    "field",
    [
        "password",
        "aws_secret_access_key",
        "session_token",
        "api_key",
        "authorization",
        "private_key",
    ],
)
def test_credentials_are_redacted(capsys: pytest.CaptureFixture[str], field: str) -> None:
    configure_logging("api", "INFO")

    line = _emit(capsys, **{field: "super-secret-value"})

    assert line[field] == "[redacted]"
    assert "super-secret-value" not in json.dumps(line)


def test_gps_fields_are_dropped_entirely(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("worker", "INFO")

    line = _emit(capsys, gps_latitude=51.5, GPSInfo={"lat": 51.5})

    assert "gps_latitude" not in line
    assert "GPSInfo" not in line


def test_gps_inside_a_nested_dict_is_dropped(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("worker", "INFO")

    line = _emit(capsys, exif={"Make": "PixelForge", "GPSInfo": {"lat": 51.5}})

    assert line["exif"] == {"Make": "PixelForge"}


def test_long_values_are_truncated(capsys: pytest.CaptureFixture[str]) -> None:
    """A stray payload must not turn a log line into a file dump."""
    configure_logging("worker", "INFO")

    line = _emit(capsys, body="x" * 10_000)

    assert len(line["body"]) < 600
    assert line["body"].endswith("[truncated]")


def test_binary_values_are_summarised(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("worker", "INFO")

    line = _emit(capsys, payload=b"\x00" * 2048)

    assert line["payload"] == "[2048 bytes]"


def test_stdlib_logs_share_the_json_format(capsys: pytest.CaptureFixture[str]) -> None:
    """uvicorn and botocore must not emit a second, differently shaped format."""
    configure_logging("api", "INFO")

    logging.getLogger("uvicorn.error").warning("started on %s", 8000)

    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["service"] == "api"
    assert line["level"] == "warning"
    assert "8000" in line["message"]


def test_exception_info_is_rendered(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("worker", "INFO")

    try:
        raise ValueError("boom")
    except ValueError:
        get_logger("test").exception("job_unexpected_error")

    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "ValueError" in line["exception"]
    assert "boom" in line["exception"]


def test_redact_exif_removes_location_keys() -> None:
    exif = {"Make": "PixelForge", "GPSInfo": 1, "GPSLatitude": 51.5, "Model": "X"}

    assert redact_exif(exif) == {"Make": "PixelForge", "Model": "X"}
    # The input is not mutated.
    assert "GPSInfo" in exif


def test_redact_exif_handles_none() -> None:
    assert redact_exif(None) == {}
