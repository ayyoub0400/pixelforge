#!/usr/bin/env bash
#
# Create the local S3 bucket, SQS queue (with DLQ and redrive policy) and
# DynamoDB table inside LocalStack.
#
# Run as a one-shot container by docker-compose after LocalStack reports
# healthy; api and worker wait for it to exit successfully. Re-running it is
# safe: every step tolerates the resource already existing.
#
# This is local development tooling. Real environments are provisioned
# separately by the platform team from the CONTRACT section of the README.

set -euo pipefail

ENDPOINT="${AWS_ENDPOINT_URL:-http://localhost:4566}"
REGION="${AWS_REGION:-us-east-1}"
BUCKET="${S3_BUCKET:-pixelforge-local}"
QUEUE="${SQS_QUEUE_NAME:-pixelforge-jobs}"
DLQ="${SQS_DLQ_NAME:-pixelforge-jobs-dlq}"
TABLE="${DYNAMODB_TABLE:-pixelforge-jobs}"
MAX_RECEIVE_COUNT="${SQS_MAX_RECEIVE_COUNT:-3}"
VISIBILITY_TIMEOUT="${SQS_VISIBILITY_TIMEOUT:-60}"

# Prefer LocalStack's awslocal wrapper; fall back to a plain aws CLI so the
# script also works from an amazon/aws-cli container.
if command -v awslocal >/dev/null 2>&1; then
  AWS_BIN="awslocal"
else
  AWS_BIN="aws"
fi

aws_local() {
  "${AWS_BIN}" --endpoint-url "${ENDPOINT}" --region "${REGION}" "$@"
}

log() {
  echo "[init-resources] $*"
}

log "endpoint=${ENDPOINT} region=${REGION}"

# --- S3 --------------------------------------------------------------------
if aws_local s3api head-bucket --bucket "${BUCKET}" >/dev/null 2>&1; then
  log "bucket ${BUCKET} already exists"
else
  log "creating bucket ${BUCKET}"
  aws_local s3api create-bucket --bucket "${BUCKET}" >/dev/null
fi

# --- SQS dead-letter queue -------------------------------------------------
log "creating dead-letter queue ${DLQ}"
aws_local sqs create-queue --queue-name "${DLQ}" >/dev/null

DLQ_URL="$(aws_local sqs get-queue-url --queue-name "${DLQ}" --query 'QueueUrl' --output text)"
DLQ_ARN="$(aws_local sqs get-queue-attributes \
  --queue-url "${DLQ_URL}" \
  --attribute-names QueueArn \
  --query 'Attributes.QueueArn' \
  --output text)"
log "dlq arn=${DLQ_ARN}"

# --- SQS main queue --------------------------------------------------------
# maxReceiveCount=3 means a message that fails three deliveries is parked on
# the DLQ instead of cycling forever. VisibilityTimeout is 60s; the worker's
# heartbeat extends it for jobs that outlive that window.
ATTRS_FILE="$(mktemp)"
cat > "${ATTRS_FILE}" <<EOF
{
  "VisibilityTimeout": "${VISIBILITY_TIMEOUT}",
  "MessageRetentionPeriod": "345600",
  "RedrivePolicy": "{\"deadLetterTargetArn\":\"${DLQ_ARN}\",\"maxReceiveCount\":\"${MAX_RECEIVE_COUNT}\"}"
}
EOF

log "creating queue ${QUEUE} (maxReceiveCount=${MAX_RECEIVE_COUNT})"
aws_local sqs create-queue --queue-name "${QUEUE}" --attributes "file://${ATTRS_FILE}" >/dev/null
rm -f "${ATTRS_FILE}"

QUEUE_URL="$(aws_local sqs get-queue-url --queue-name "${QUEUE}" --query 'QueueUrl' --output text)"
log "queue url=${QUEUE_URL}"

# --- DynamoDB --------------------------------------------------------------
if aws_local dynamodb describe-table --table-name "${TABLE}" >/dev/null 2>&1; then
  log "table ${TABLE} already exists"
else
  log "creating table ${TABLE} (partition key job_id, no sort key, no GSI)"
  aws_local dynamodb create-table \
    --table-name "${TABLE}" \
    --attribute-definitions AttributeName=job_id,AttributeType=S \
    --key-schema AttributeName=job_id,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST >/dev/null
  aws_local dynamodb wait table-exists --table-name "${TABLE}"
fi

log "all resources ready"
