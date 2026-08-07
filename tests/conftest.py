"""Shared pytest fixtures.

Every test runs fully offline against moto. Before any AWS client is created
the environment is scrubbed of real credentials and pointed at paths that do
not exist, so a developer with a populated ``~/.aws`` cannot accidentally have
the suite talk to a real account.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from api.chaos import ChaosController
from api.main import create_app
from shared.aws import AwsClients
from shared.config import Config
from worker.consumer import Worker

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"

TEST_REGION = "us-east-1"
TEST_BUCKET = "pixelforge-test"
TEST_QUEUE_NAME = "pixelforge-test-jobs"
TEST_DLQ_NAME = "pixelforge-test-jobs-dlq"
TEST_TABLE = "pixelforge-test-jobs"


@pytest.fixture(scope="session", autouse=True)
def _isolate_aws_environment() -> Iterator[None]:
    """Guarantee the suite cannot reach a real AWS account.

    moto needs *some* credentials to be present for signing, so dummy ones are
    injected. Anything that could point botocore at a real endpoint or a real
    credentials file is removed.
    """
    saved = dict(os.environ)
    for name in (
        "AWS_PROFILE",
        "AWS_ENDPOINT_URL",
        "AWS_ENDPOINT_URL_S3",
        "AWS_ENDPOINT_URL_SQS",
        "AWS_ENDPOINT_URL_DYNAMODB",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_ROLE_ARN",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    ):
        os.environ.pop(name, None)
    os.environ.update(
        {
            "AWS_ACCESS_KEY_ID": "testing",
            "AWS_SECRET_ACCESS_KEY": "testing",
            "AWS_SESSION_TOKEN": "testing",
            "AWS_SECURITY_TOKEN": "testing",
            "AWS_DEFAULT_REGION": TEST_REGION,
            "AWS_REGION": TEST_REGION,
            "AWS_SHARED_CREDENTIALS_FILE": str(Path(__file__).parent / "no-such-credentials"),
            "AWS_CONFIG_FILE": str(Path(__file__).parent / "no-such-config"),
            "AWS_EC2_METADATA_DISABLED": "true",
        }
    )
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


@pytest.fixture
def aws() -> Iterator[None]:
    """Activate moto for the duration of one test."""
    with mock_aws():
        yield


@pytest.fixture
def aws_resources(aws: None) -> dict[str, str]:
    """Create the bucket, queue (+DLQ with redrive) and table moto will serve.

    Mirrors what ``docker/localstack/init-resources.sh`` creates locally and
    what the platform team provisions for real, including
    ``maxReceiveCount=3``.
    """
    s3 = boto3.client("s3", region_name=TEST_REGION)
    s3.create_bucket(Bucket=TEST_BUCKET)

    sqs = boto3.client("sqs", region_name=TEST_REGION)
    dlq_url = sqs.create_queue(QueueName=TEST_DLQ_NAME)["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    queue_url = sqs.create_queue(
        QueueName=TEST_QUEUE_NAME,
        Attributes={
            "VisibilityTimeout": "60",
            "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "3"}),
        },
    )["QueueUrl"]

    dynamodb = boto3.client("dynamodb", region_name=TEST_REGION)
    dynamodb.create_table(
        TableName=TEST_TABLE,
        AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )

    return {"queue_url": queue_url, "dlq_url": dlq_url, "bucket": TEST_BUCKET}


@pytest.fixture
def config(aws_resources: dict[str, str]) -> Config:
    """Configuration wired to the moto-backed resources."""
    return Config(
        aws_region=TEST_REGION,
        s3_bucket=TEST_BUCKET,
        sqs_queue_url=aws_resources["queue_url"],
        dynamodb_table=TEST_TABLE,
        log_level="DEBUG",
        shutdown_grace_seconds=5,
        max_upload_bytes=1_048_576,
        thumbnail_sizes=(150, 400, 800),
        enable_chaos_endpoint=False,
    )


@pytest.fixture
def clients(config: Config) -> AwsClients:
    """AWS wrappers built against moto."""
    return AwsClients.build(config)


@pytest.fixture
def raw_clients(config: Config) -> dict[str, Any]:
    """Low-level boto3 clients, for assertions about what actually landed."""
    return {
        "s3": boto3.client("s3", region_name=TEST_REGION),
        "sqs": boto3.client("sqs", region_name=TEST_REGION),
        "dynamodb": boto3.client("dynamodb", region_name=TEST_REGION),
    }


@pytest.fixture
def chaos() -> ChaosController:
    """A fresh chaos controller per test."""
    return ChaosController()


@pytest.fixture
def app(config: Config, clients: AwsClients, chaos: ChaosController) -> Any:
    """The FastAPI application wired to moto."""
    return create_app(config=config, clients=clients, chaos=chaos)


@pytest.fixture
def client(app: Any) -> Iterator[Any]:
    """A TestClient that runs the application lifespan."""
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def chaos_client(config: Config, clients: AwsClients, chaos: ChaosController) -> Iterator[Any]:
    """A TestClient with ``ENABLE_CHAOS_ENDPOINT`` switched on."""
    from fastapi.testclient import TestClient

    enabled = Config(**{**_config_kwargs(config), "enable_chaos_endpoint": True})
    with TestClient(create_app(config=enabled, clients=clients, chaos=chaos)) as test_client:
        yield test_client


@pytest.fixture
def worker(config: Config, clients: AwsClients) -> Worker:
    """A worker that polls without blocking and never really sleeps."""
    return Worker(
        config,
        clients,
        poll_wait_seconds=0,
        heartbeat_interval_seconds=3600,
        sleep=lambda _seconds: None,
    )


@pytest.fixture
def broken_clients(config: Config) -> AwsClients:
    """Wrappers pointed at resources that do not exist, for failure paths."""
    missing = Config(
        **{
            **_config_kwargs(config),
            "s3_bucket": "pixelforge-does-not-exist",
            "sqs_queue_url": config.sqs_queue_url.rsplit("/", 1)[0] + "/no-such-queue",
            "dynamodb_table": "no-such-table",
        }
    )
    return AwsClients.build(missing)


@pytest.fixture
def fixture_bytes() -> Any:
    """Return a loader for files in ``fixtures/``."""

    def load(name: str) -> bytes:
        return (FIXTURES_DIR / name).read_bytes()

    return load


def _config_kwargs(config: Config) -> dict[str, Any]:
    """Explode a Config into constructor kwargs (it is a slotted dataclass)."""
    return {
        "aws_region": config.aws_region,
        "s3_bucket": config.s3_bucket,
        "sqs_queue_url": config.sqs_queue_url,
        "dynamodb_table": config.dynamodb_table,
        "log_level": config.log_level,
        "shutdown_grace_seconds": config.shutdown_grace_seconds,
        "max_upload_bytes": config.max_upload_bytes,
        "thumbnail_sizes": config.thumbnail_sizes,
        "enable_chaos_endpoint": config.enable_chaos_endpoint,
        "otel_exporter_otlp_endpoint": config.otel_exporter_otlp_endpoint,
        "aws_endpoint_url": config.aws_endpoint_url,
    }


def metric_value(name: str, labels: dict[str, str] | None = None) -> float:
    """Read one sample from whichever registry owns the metric.

    Metrics are process-global, so tests compare a before/after delta rather
    than an absolute value.
    """
    from shared.metrics import API_REGISTRY, WORKER_REGISTRY

    for registry in (API_REGISTRY, WORKER_REGISTRY):
        sample = registry.get_sample_value(name, labels or {})
        if sample is not None:
            return float(sample)
    return 0.0


def upload_fixture(client: Any, name: str, *, content_type: str = "image/jpeg") -> Any:
    """POST a fixture file to the jobs endpoint."""
    payload = (FIXTURES_DIR / name).read_bytes()
    return client.post("/api/v1/jobs", files={"file": (name, payload, content_type)})


def receive_one(clients: AwsClients) -> dict[str, Any]:
    """Receive exactly one message, failing the test if none arrives."""
    messages = clients.queue.receive(max_messages=1, wait_seconds=0)
    assert messages, "expected a message on the queue"
    return messages[0]
