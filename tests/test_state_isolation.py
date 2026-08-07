"""Statelessness guarantees: no local state, temp files always cleaned up.

Every instance has to be interchangeable, which means a pod that dies
mid-render must leave nothing behind and a replacement must be able to pick the
work up with no local context.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import TEST_BUCKET, upload_fixture

from api.service import (
    EnqueueFailedError,
    JobService,
    UploadRejectedError,
    is_valid_job_id,
    sanitise_filename,
)
from shared.aws import AwsClients
from shared.config import Config
from shared.errors import TransientDependencyError
from shared.models import JobStatus
from shared.tempfiles import temp_workspace
from worker.consumer import Worker


def _temp_entries() -> set[str]:
    """Names currently present in the system temp directory."""
    return {entry.name for entry in Path(tempfile.gettempdir()).iterdir()}


def test_temp_workspace_is_removed_on_success() -> None:
    with temp_workspace() as workspace:
        (workspace / "scratch").write_bytes(b"data")
        assert workspace.exists()

    assert not workspace.exists()


def test_temp_workspace_is_removed_when_the_body_raises() -> None:
    captured: Path | None = None

    with pytest.raises(RuntimeError), temp_workspace() as workspace:
        captured = workspace
        (workspace / "scratch").write_bytes(b"data")
        raise RuntimeError("render blew up")

    assert captured is not None and not captured.exists()


def test_worker_leaves_no_temporary_files_behind(client: Any, worker: Worker) -> None:
    upload_fixture(client, "landscape.jpg")
    before = _temp_entries()

    worker.run_once()

    leftovers = {name for name in _temp_entries() - before if name.startswith("pixelforge-")}
    assert leftovers == set()


def test_failed_render_leaves_no_temporary_files_behind(
    client: Any,
    clients: AwsClients,
    worker: Worker,
    raw_clients: dict[str, Any],
    fixture_bytes: Callable[[str], bytes],
) -> None:
    job_id = upload_fixture(client, "truncated.jpg").json()["job_id"]
    raw_clients["s3"].put_object(
        Bucket=TEST_BUCKET,
        Key=f"uploads/{job_id}/original.jpg",
        Body=fixture_bytes("truncated.jpg"),
    )
    before = _temp_entries()

    worker.run_once()

    leftovers = {name for name in _temp_entries() - before if name.startswith("pixelforge-")}
    assert leftovers == set()


def test_nothing_is_written_to_the_working_directory(
    client: Any, worker: Worker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_fixture(client, "landscape.jpg")
    monkeypatch.chdir(tmp_path)

    worker.run_once()

    assert list(tmp_path.iterdir()) == []


def test_two_worker_instances_are_interchangeable(
    client: Any, clients: AwsClients, config: Config
) -> None:
    """No in-memory registry: a second instance can finish the first's queue."""
    first_id = upload_fixture(client, "landscape.jpg").json()["job_id"]
    second_id = upload_fixture(client, "portrait.png", content_type="image/png").json()["job_id"]

    worker_a = Worker(config, clients, poll_wait_seconds=0, sleep=lambda _s: None)
    worker_b = Worker(config, clients, poll_wait_seconds=0, sleep=lambda _s: None)
    worker_a.run_once()
    worker_b.run_once()

    for job_id in (first_id, second_id):
        record = clients.table.get_job(job_id)
        assert record is not None and record.status is JobStatus.COMPLETE


def test_enqueue_failure_marks_the_job_failed_and_returns_503(
    client: Any, clients: AwsClients, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job that cannot be queued must not sit in PENDING forever."""

    def explode(*_args: object, **_kwargs: object) -> None:
        raise TransientDependencyError("sqs is down", operation="sqs.send_message")

    monkeypatch.setattr(clients.queue, "send_job", explode)

    response = upload_fixture(client, "landscape.jpg")

    assert response.status_code == 503
    assert response.json()["code"] == "enqueue_failed"

    # The API cannot list jobs, so find the record via the object it stored.
    listing = clients.store.client.list_objects_v2(Bucket=TEST_BUCKET, Prefix="uploads/")
    keys = [item["Key"] for item in listing.get("Contents", [])]
    assert len(keys) == 1
    job_id = keys[0].split("/")[1]

    record = clients.table.get_job(job_id)
    assert record is not None
    assert record.status is JobStatus.FAILED
    assert record.error and "could not enqueue" in record.error


def test_enqueue_failure_is_counted_as_an_upload_error(
    client: Any, clients: AwsClients, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import metric_value

    monkeypatch.setattr(
        clients.queue,
        "send_job",
        lambda *_a, **_k: (_ for _ in ()).throw(EnqueueFailedError("nope")),
    )
    before = metric_value("pixelforge_uploads_total", {"result": "error"})

    upload_fixture(client, "landscape.jpg")

    assert metric_value("pixelforge_uploads_total", {"result": "error"}) == before + 1


def test_service_validates_before_touching_aws(config: Config, clients: AwsClients) -> None:
    """Rejections must not create objects, records or messages."""
    service = JobService(config, clients)

    with pytest.raises(UploadRejectedError):
        service.create_job(data=b"not an image", filename="x.jpg", content_type="image/jpeg")

    listing = clients.store.client.list_objects_v2(Bucket=TEST_BUCKET)
    assert listing.get("KeyCount", 0) == 0
    assert clients.queue.receive(max_messages=1, wait_seconds=0) == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "upload"),
        ("", "upload"),
        ("photo.jpg", "photo.jpg"),
        ("../../etc/passwd", "etcpasswd"),
        ("a\x00b.jpg", "ab.jpg"),
        ("dir/sub/name.png", "dirsubname.png"),
        ("   ", "upload"),
    ],
)
def test_filename_sanitisation(raw: str | None, expected: str) -> None:
    assert sanitise_filename(raw) == expected


def test_filename_length_is_bounded() -> None:
    assert len(sanitise_filename("x" * 1000)) == 255


@pytest.mark.parametrize(
    ("job_id", "valid"),
    [
        ("6ba7b810-9dad-11d1-80b4-00c04fd430c8", True),
        ("6BA7B810-9DAD-11D1-80B4-00C04FD430C8", True),
        ("not-a-uuid", False),
        ("", False),
        ("../../../etc/passwd", False),
        ("6ba7b810-9dad-11d1-80b4-00c04fd430c", False),
    ],
)
def test_job_id_validation(job_id: str, valid: bool) -> None:
    assert is_valid_job_id(job_id) is valid
