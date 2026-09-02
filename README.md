# pixelforge

# The following application was developed strictly by Claude AI, with the only purpose of giving me something to use in my DevOps project.

Asynchronous image processing. Upload an image, get a job id back immediately,
and collect three thumbnails plus EXIF metadata when the worker has finished.

The system is deliberately split into two services that never talk to each
other directly. The API is latency-sensitive and cheap to run; the worker is
CPU-bound and bursty. A queue between them means each scales on its own signal,
and a backlog becomes a metric instead of a timeout.

```
                                   ┌──────────────┐
   client ──POST /api/v1/jobs──▶   │              │ ──PutObject──▶  S3
                                   │     API      │ ──PutItem────▶  DynamoDB
   client ◀──202 {job_id}──────    │  (port 8000) │ ──SendMessage─▶ SQS ──┐
                                   └──────────────┘                       │
                                                                          │
                                   ┌──────────────┐                       │
   client ──GET  /api/v1/jobs/{id}─▶│              │ ◀─GetItem──── DynamoDB│
                                   └──────────────┘                       │
                                                                          │
                                   ┌──────────────┐    ReceiveMessage ◀───┘
                                   │    worker    │ ──GetObject──▶  S3
                                   │ (metrics on  │ ──PutObject──▶  S3  outputs/
                                   │    9090)     │ ──UpdateItem─▶  DynamoDB
                                   └──────────────┘ ──DeleteMessage▶ SQS
                                          │
                                          └── failures ─▶ DLQ (maxReceiveCount=3)
```

**Scope.** This repository is the application only: two container images and
the contract below. There is no Terraform, no Kubernetes manifest, no Helm
chart and no CI workflow here — those are the platform engineer's, built
against the [CONTRACT](#contract).

---

## Quickstart

Requires Docker. Nothing else, and no network access to AWS.

```bash
make up
```

That starts LocalStack (S3, SQS, DynamoDB), provisions the bucket, the queue
with its dead-letter queue, and the table, then starts the API and the worker.

Submit a job and watch it complete:

```bash
make smoke
```

Or by hand:

```bash
curl -X POST http://localhost:8000/api/v1/jobs -F "file=@fixtures/landscape.jpg;type=image/jpeg"
```

```bash
curl http://localhost:8000/api/v1/jobs/<job_id>
```

| What | Where |
| --- | --- |
| API | <http://localhost:8000> (OpenAPI at `/docs`) |
| Worker metrics | <http://localhost:9090/metrics> |
| LocalStack | <http://localhost:4566> |

Other commands: `make logs`, `make ps`, `make test`, `make load`, `make down`.
Run `make help` for the full list.

> Without `make` (Windows without WSL, for example) every target is a one-liner
> you can run directly — `docker compose up --build -d`,
> `docker compose logs -f api worker`, `docker compose down -v`. Read the
> `Makefile`; it is deliberately thin.

---

## CONTRACT

Everything in this section is what the platform layer builds against. It is
covered by tests; if you change one of these, a test fails and this section
must change with it.

### Services at a glance

| | API | Worker |
| --- | --- | --- |
| Image | `docker/Dockerfile.api` | `docker/Dockerfile.worker` |
| Entrypoint | `python -m api.main` | `python -m worker.main` |
| Ingress port | **8000** (HTTP) | none |
| Metrics port | 8000, path `/metrics` | **9090**, path `/metrics` |
| Runs as | UID/GID **10001**, non-root | UID/GID **10001**, non-root |
| Scales on | request rate / CPU | SQS queue depth |
| Replicas | many, interchangeable | many, interchangeable |

Both images are `python:3.12-slim`, multi-stage, with the Python process as
PID 1 in exec form. `STOPSIGNAL` is `SIGTERM` and both services handle it.

### Environment variables

Configuration comes **only** from environment variables. A missing required
variable is a startup failure: the process prints `FATAL: invalid
configuration: ...` to stderr and exits **78** (`EX_CONFIG`). All missing
variables are reported in one line, so a broken Deployment is fixed in one
pass.

| Variable | Required | Default | API | Worker | Meaning |
| --- | --- | --- | :-: | :-: | --- |
| `AWS_REGION` | yes | — | ● | ● | Region for all three clients. |
| `S3_BUCKET` | yes | — | ● | ● | Bucket holding originals and outputs. |
| `SQS_QUEUE_URL` | yes | — | ● | ● | Full queue URL, not the name. |
| `DYNAMODB_TABLE` | yes | — | ● | ● | Job table name. |
| `LOG_LEVEL` | no | `INFO` | ● | ● | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`. |
| `SHUTDOWN_GRACE_SECONDS` | no | `30` | ● | ● | Drain budget on SIGTERM. |
| `MAX_UPLOAD_BYTES` | no | `10485760` | ● | | Uploads above this get `413`. |
| `THUMBNAIL_SIZES` | no | `150,400,800` | | ● | Comma-separated bounding-box edges. |
| `ENABLE_CHAOS_ENDPOINT` | no | `false` | ● | | Registers `POST /admin/chaos`. Keep false in production. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | no | unset | ● | ● | OTLP/HTTP collector base URL. Unset ⇒ tracing is a no-op. |
| `AWS_ENDPOINT_URL` | no | unset | ● | ● | Override for LocalStack. Never set in a real environment. |

Booleans accept `true/false`, `1/0`, `yes/no`, `on/off` (case-insensitive).
A variable set to whitespace counts as unset. A value that fails validation —
a non-integer `MAX_UPLOAD_BYTES`, a thumbnail size of `0` — is fatal, not
silently defaulted.

**No credentials are read from configuration.** There is no
`AWS_ACCESS_KEY_ID` handling anywhere in the code and no credentials file is
ever read. Clients are built with the default boto3 provider chain, so IRSA
works with zero code changes: project the service-account token and set
`AWS_ROLE_ARN`/`AWS_WEB_IDENTITY_TOKEN_FILE` as usual.

### API endpoints

| Method | Path | Success | Notes |
| --- | --- | --- | --- |
| `POST` | `/api/v1/jobs` | `202` | `multipart/form-data`, field name `file`. |
| `GET` | `/api/v1/jobs/{job_id}` | `200` | `404` if unknown or malformed. |
| `GET` | `/healthz` | `200` | Liveness. Touches nothing external. |
| `GET` | `/readyz` | `200` / `503` | Readiness. Probes S3, SQS, DynamoDB. |
| `GET` | `/metrics` | `200` | Prometheus text exposition. |
| `POST` | `/admin/chaos` | `200` | Only when `ENABLE_CHAOS_ENDPOINT=true`, else `404`. |

`POST /api/v1/jobs` responses:

```json
202  {"job_id": "0f1c…", "status": "PENDING"}
400  {"detail": "file is not a recognised image format", "code": "invalid_image"}
413  {"detail": "file exceeds the 10485760 byte limit", "code": "payload_too_large"}
415  {"detail": "unsupported content type 'text/plain'; …", "code": "unsupported_media_type"}
422  {"detail": "file: Field required", "code": "validation_error"}
503  {"detail": "job could not be queued for processing; retry", "code": "enqueue_failed"}
```

Every non-2xx response in the service uses the same `{"detail", "code"}` shape.
Accepted content types: `image/jpeg`, `image/jpg`, `image/png`, `image/webp`,
`image/gif`, `image/bmp`, `image/tiff`. The bytes are decoded before the job is
accepted, so a payload that merely *claims* to be an image is rejected at the
edge with `400` rather than becoming a failed job.

`GET /api/v1/jobs/{job_id}` returns the DynamoDB record with unset attributes
omitted. Once `COMPLETE` it carries `outputs` and `exif`:

```json
{
  "job_id": "0f1c…", "status": "COMPLETE",
  "created_at": "2026-01-15T10:30:00.123Z",
  "started_at": "…", "completed_at": "…", "updated_at": "…",
  "filename": "landscape.jpg", "size_bytes": 529770,
  "content_type": "image/jpeg",
  "input_key": "uploads/0f1c…/original.jpg",
  "source_width": 1600, "source_height": 900, "source_format": "JPEG",
  "processing_ms": 317,
  "outputs": {
    "150": {"size": 150, "key": "outputs/0f1c…/thumb_150.jpg", "width": 150, "height": 84,  "bytes": 8019},
    "400": {"size": 400, "key": "outputs/0f1c…/thumb_400.jpg", "width": 400, "height": 225, "bytes": 56772},
    "800": {"size": 800, "key": "outputs/0f1c…/thumb_800.jpg", "width": 800, "height": 450, "bytes": 171634}
  },
  "exif": {"Make": "PixelForge", "Model": "…", "DateTime": "2024:01:15 10:30:00"},
  "trace_id": "9f2c…"
}
```

### Health and readiness semantics

| Probe | Path | Port | Meaning | On failure |
| --- | --- | --- | --- | --- |
| Liveness | `/healthz` | 8000 | The process is running. | Restart the pod. |
| Readiness | `/readyz` | 8000 | S3, SQS and DynamoDB are reachable. | Remove the pod from Service endpoints. |

`/healthz` deliberately checks **nothing external**. Wiring liveness to a
dependency turns a five-minute DynamoDB blip into a fleet-wide restart storm.

`/readyz` probes all three dependencies and returns `503` if any fails, with a
body naming the culprit:

```json
{"status": "not_ready", "checks": {"s3": "ok", "sqs": "error: EndpointConnectionError", "dynamodb": "ok"}}
```

Readiness means *reachable*, so a `403 AccessDenied` from a probe counts as a
pass: the optional probe permissions below stay optional.

The worker exposes no readiness endpoint because it takes no ingress traffic.
It waits for its dependencies at startup with exponential backoff (10 rounds,
0.5s→15s) rather than crash-looping. Port 9090 answers `GET /metrics` from the
moment the process starts — before the dependency wait — so a worker blocked on
AWS is a visible, scrapeable target rather than a dead one. If you want a
liveness probe for the worker, an HTTP GET on `9090/metrics` is the right
choice, with the caveat that it proves the process is up, not that the poll
loop is making progress; alert on `pixelforge_jobs_processed_total` for that.

### Metrics

Exact names. Dashboards, alerts and the autoscaler are built against these, so
a rename is a breaking change.

**API** — scraped from `:8000/metrics`

| Metric | Type | Labels |
| --- | --- | --- |
| `pixelforge_http_requests_total` | counter | `method`, `endpoint`, `status` |
| `pixelforge_http_request_duration_seconds` | histogram | `endpoint` |
| `pixelforge_uploads_total` | counter | `result` |
| `pixelforge_upload_size_bytes` | histogram | — |

`endpoint` is the **route template** (`/api/v1/jobs/{job_id}`), never the
concrete path, so cardinality is bounded by the number of routes. Anything that
matched no route collapses onto `endpoint="unmatched"`.

`result` is one of `accepted`, `rejected_too_large`, `rejected_content_type`,
`rejected_invalid_image`, `error`. Every terminated upload attempt lands in
exactly one of them.

**Worker** — scraped from `:9090/metrics`

| Metric | Type | Labels |
| --- | --- | --- |
| `pixelforge_jobs_processed_total` | counter | `status` ∈ {`complete`, `failed`} |
| `pixelforge_job_duration_seconds` | histogram | — |
| `pixelforge_jobs_inflight` | gauge | — |
| `pixelforge_job_stage_duration_seconds` | histogram | `stage` ∈ {`download`, `process`, `upload`} |
| `pixelforge_sqs_errors_total` | counter | `operation` |

The two services use separate registries, so an API scrape never reports worker
series and vice versa. The one exception is `pixelforge_sqs_errors_total`,
which both emit: the API sends messages, the worker receives and deletes them,
and a failure on either side is worth alerting on. `operation` is the SQS call
name — `send_message`, `receive_message`, `delete_message`,
`change_message_visibility`, `get_queue_attributes`.

Standard `process_*` and `python_gc_*` collectors are registered on both.

**Autoscaling.** Scale the worker on SQS queue depth
(`ApproximateNumberOfMessagesVisible`), which is the signal that leads demand.
`pixelforge_jobs_inflight` is a per-replica saturation gauge and lags; use it
for dashboards, not for the scaling rule. `make load-burst` produces a burst
that outruns a single worker by roughly an order of magnitude.

### IAM — least privilege, per service

`${BUCKET}`, `${QUEUE_ARN}` and `${TABLE_ARN}` below are the resources named by
`S3_BUCKET`, `SQS_QUEUE_URL` and `DYNAMODB_TABLE`.

**API** — writes originals, records jobs, enqueues work. It never reads or
writes an output object and has no `UpdateItem`, by design.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "StoreOriginals",  "Effect": "Allow",
      "Action": ["s3:PutObject"],
      "Resource": "arn:aws:s3:::${BUCKET}/uploads/*" },
    { "Sid": "EnqueueJobs",     "Effect": "Allow",
      "Action": ["sqs:SendMessage"],
      "Resource": "${QUEUE_ARN}" },
    { "Sid": "RecordAndReadJobs", "Effect": "Allow",
      "Action": ["dynamodb:PutItem", "dynamodb:GetItem"],
      "Resource": "${TABLE_ARN}" }
  ]
}
```

**Worker** — reads originals, writes outputs, drives the queue, updates records.
No `PutItem`: the worker only ever mutates jobs the API created.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "ReadOriginals", "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": "arn:aws:s3:::${BUCKET}/uploads/*" },
    { "Sid": "WriteOutputs",  "Effect": "Allow",
      "Action": ["s3:PutObject"],
      "Resource": "arn:aws:s3:::${BUCKET}/outputs/*" },
    { "Sid": "ConsumeJobs",   "Effect": "Allow",
      "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility"],
      "Resource": "${QUEUE_ARN}" },
    { "Sid": "UpdateJobs",    "Effect": "Allow",
      "Action": ["dynamodb:UpdateItem", "dynamodb:GetItem"],
      "Resource": "${TABLE_ARN}" }
  ]
}
```

**Optional, for nicer probes.** Readiness works without these — the probe treats
`AccessDenied` as proof the dependency answered — but granting them turns a
"reachable" check into a real one:

| Action | Resource | Used by |
| --- | --- | --- |
| `s3:ListBucket` | `arn:aws:s3:::${BUCKET}` | `HeadBucket` in `/readyz` and worker startup |
| `sqs:GetQueueAttributes` | `${QUEUE_ARN}` | queue probe in `/readyz` and worker startup |

The DynamoDB probe issues a `GetItem` for the sentinel key
`job_id = "__readiness_probe__"`, which needs no permission beyond the
`dynamodb:GetItem` both services already hold. That key is never written.

### SQS message schema

Body — JSON, UTF-8:

```json
{
  "schema_version": 1,
  "job_id": "0f1c8e5a-6d2b-4a91-9c4e-2f0a1b3c4d5e",
  "bucket": "pixelforge-prod",
  "input_key": "uploads/0f1c8e5a-…/original.jpg",
  "content_type": "image/jpeg",
  "filename": "landscape.jpg",
  "submitted_at": "2026-01-15T10:30:00.123Z"
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `schema_version` | int | `1`. A worker that sees a higher version leaves the message for the DLQ instead of guessing. |
| `job_id` | string | UUIDv4. Partition key of the DynamoDB item. |
| `bucket` | string | Must match the consumer's `S3_BUCKET`; a mismatch fails the job rather than reading across environments. |
| `input_key` | string | `uploads/{job_id}/original{ext}` |
| `content_type` | string | Normalised, lowercase, no parameters. |
| `filename` | string | Sanitised client filename. Display only — it never influences a key. |
| `submitted_at` | string | ISO-8601 UTC, milliseconds, `Z` suffix. |

Message attributes — all `String`:

| Attribute | Notes |
| --- | --- |
| `traceparent` | W3C trace context. Present when a span was active at upload. |
| `tracestate` | Present only when the incoming context carried one. |
| `job_id` | Duplicated from the body for queue-level filtering and debugging. |

Telemetry travels in attributes, never in the body, so the body stays a stable
data contract.

### DynamoDB item schema

**Key design.** Partition key `job_id` (String). **No sort key, no secondary
index, no scans.** Every access is a single-item `GetItem` or `UpdateItem` by
id, which is why the table needs nothing else. `PAY_PER_REQUEST` suits the
bursty write pattern; provisioned capacity works too if you have a baseline.

| Attribute | Type | Written by | When |
| --- | --- | --- | --- |
| `job_id` | S | API | always (partition key) |
| `status` | S | both | always — `PENDING` → `PROCESSING` → `COMPLETE` \| `FAILED` |
| `created_at` | S | API | always |
| `filename` | S | API | always |
| `size_bytes` | N | API | always |
| `content_type` | S | API | always |
| `input_key` | S | API | always |
| `source_width` / `source_height` | N | API, refreshed by worker | always |
| `source_format` | S | API, refreshed by worker | always |
| `trace_id` | S | API | when a trace was active |
| `updated_at` | S | both | on every transition |
| `started_at` | S | worker | on `PROCESSING` |
| `completed_at` | S | worker | on `COMPLETE` or `FAILED` |
| `outputs` | M | worker | on `COMPLETE` |
| `exif` | M | worker | on `COMPLETE` |
| `processing_ms` | N | worker | on `COMPLETE` |
| `error` | S | worker | on `FAILED`, truncated to 512 chars |

`outputs` is a map keyed by the thumbnail size as a string:

```
outputs: { "400": { size: N 400, key: S "outputs/{job_id}/thumb_400.jpg",
                    width: N 400, height: N 225, bytes: N 56772 } }
```

`exif` is a flat map of tag name to string/number. **GPS tags are dropped
during extraction**, so location data is never stored, never returned by the
API, and therefore cannot leak into a log line. Attributes that are not yet
known are absent from the item rather than stored as null.

Status transitions are conditional: `PENDING`/`PROCESSING` → `PROCESSING`
succeeds only when the item exists and is not already `COMPLETE`, which is what
makes a duplicate SQS delivery a no-op.

### S3 key layout

| Prefix | Written by | Read by | Content |
| --- | --- | --- | --- |
| `uploads/{job_id}/original{ext}` | API | worker | The submitted bytes, unmodified. |
| `outputs/{job_id}/thumb_{size}.jpg` | worker | clients | Rendered thumbnail, always JPEG. |

`{ext}` comes from the **detected** image format, never from the client's
filename, so a crafted name cannot influence the key. The two prefixes are
disjoint, which is what lets the IAM policies above be split by prefix.

Nothing in the application deletes objects; lifecycle policy is yours.

### Infrastructure the services expect

| Resource | Requirement |
| --- | --- |
| S3 bucket | One bucket. Versioning and encryption are your call; the app is indifferent. |
| SQS queue | Standard queue. `VisibilityTimeout` ≥ 60s recommended. |
| SQS redrive | Dead-letter queue with `maxReceiveCount = 3`. |
| DynamoDB table | Partition key `job_id` (S). No sort key, no GSI. |

The worker extends the visibility timeout of an in-flight message every 15
seconds, pushing the deadline 60 seconds out, so a slow job is not redelivered
while it is still being rendered. A message is deleted **only after** DynamoDB
confirms `COMPLETE`, so a crash mid-job results in redelivery, never in a lost
job.

### Runtime contract

| Property | Value |
| --- | --- |
| User | UID 10001, GID 10001, non-root, no shell |
| Writable path | `/tmp` only (`TMPDIR=/tmp`) |
| Root filesystem | Can be read-only, with an `emptyDir` mounted at `/tmp` |
| Capabilities | None required; drop `ALL` |
| Signals | `SIGTERM` handled directly by PID 1 |

**No local state.** Nothing is written to the working directory, there is no
in-memory job registry, and every temporary file lives in a private `/tmp`
directory removed in a `finally` block. Replicas are interchangeable; a pod can
be killed at any point and another finishes the work.

**Shutdown budgets.** On `SIGTERM` the API stops accepting connections and
drains in-flight requests for up to `SHUTDOWN_GRACE_SECONDS`; the worker stops
polling, finishes the job in its hands, and exits.

| Service | `terminationGracePeriodSeconds` |
| --- | --- |
| API | `SHUTDOWN_GRACE_SECONDS + 5` (35 by default) |
| Worker | `SHUTDOWN_GRACE_SECONDS + 25` (55 by default) |

The worker needs the extra headroom because a signal can arrive at the start of
a 20-second SQS long poll, which must return before the loop notices the stop
flag. Measured on the local stack: SIGTERM to exit takes up to ~21s when idle.
A watchdog forces exit once `SHUTDOWN_GRACE_SECONDS` has elapsed, so the pod
never has to be SIGKILLed.

### Logging

One JSON object per line on **stdout**, nothing on stderr (except the
configuration-failure message, which is emitted before logging is configured).

```json
{"timestamp":"2026-01-15T10:30:00.123456Z","level":"info","service":"worker",
 "message":"job_completed","event":"job_completed","job_id":"0f1c…",
 "trace_id":"9f2c…","duration_ms":317.23,"thumbnails":["150","400","800"]}
```

`timestamp`, `level`, `service` and `message` are always present. `job_id` and
`trace_id` are attached automatically from context. uvicorn and botocore are
routed through the same renderer, so the container emits exactly one format.

Three rules are enforced by processors rather than by convention: keys that
look like credentials are replaced with `[redacted]`, keys that look like
location data are dropped, and long values are truncated so a stray payload
cannot turn a log line into a file dump.

### Tracing

The API injects the W3C `traceparent` into the SQS message attributes and the
worker extracts it, so one trace spans "upload accepted" through "thumbnails
written" across the queue hop. Set `OTEL_EXPORTER_OTLP_ENDPOINT` to an
OTLP/HTTP collector base URL (the exporter appends `/v1/traces`). Unset, the
whole path degrades to a no-op: no exporter, no network traffic, no errors.

---

## Chaos endpoint

Registered only when `ENABLE_CHAOS_ENDPOINT=true`; otherwise `POST
/admin/chaos` is a plain `404`. It exists so that a rollback, an alert or a
readiness-driven traffic shift can be demonstrated on a live deployment without
shipping a broken build.

```bash
curl -X POST http://localhost:8000/admin/chaos \
  -H 'Content-Type: application/json' \
  -d '{"latency_ms": 2000, "error_rate": 0.5}'
```

| Field | Type | Range | Default | Effect |
| --- | --- | --- | --- | --- |
| `fail_readiness` | bool | — | `false` | `/readyz` returns `503` without probing AWS. `/healthz` stays `200`, so the pod leaves the Service endpoints without being restarted. |
| `latency_ms` | int | 0–60000 | `0` | Delay added to every `/api/v1` request. |
| `error_rate` | float | 0.0–1.0 | `0.0` | Fraction of `/api/v1` requests failed with `500 chaos_injected_error`. |

Omitted fields keep their current value, so one knob can be nudged without
restating the others. The response body is the settings now in force.

Two things worth knowing before you demo it:

* **Settings are per-replica.** They apply only to the pod that received the
  `POST`. That is usually what you want — one sick pod among healthy ones — but
  it explains why only some requests slow down behind a Service.
* **Only `/api/v1` is affected.** `/healthz`, `/readyz` and `/metrics` keep
  telling the truth, which is what makes the effect observable. Injected
  failures are counted and logged exactly like real ones, attributed to the
  endpoint they hit.

Restarting the pod clears everything. `make chaos-latency`, `make chaos-errors`,
`make chaos-readiness` and `make chaos-reset` are shortcuts.

---

## Load generator

```bash
make load                                  # 5/s for 30s, polling to completion
make load RATE=50 DURATION=60 CONCURRENCY=32
make load-burst                            # 120/s for 60s: outruns one worker
python -m loadgen.generate --rate 20 --duration 30 --poll --base-url http://localhost:8000
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--base-url` | `$PIXELFORGE_BASE_URL` or `http://localhost:8000` | Target API. |
| `--rate` | `5` | Uploads per second. |
| `--duration` | `30` | Length of the upload phase, in seconds. |
| `--concurrency` | `8` | Parallel connections. |
| `--poll` | off | Follow every accepted job to `COMPLETE`/`FAILED`. |
| `--poll-timeout` | `120` | Give up on one job after this long. |
| `--width` / `--height` | `1600` / `1200` | Size of the generated JPEGs. |
| `--distinct-images` | `4` | Payloads pre-rendered and cycled. |

It reports throughput, upload-latency percentiles, a status-code breakdown and
— with `--poll` — end-to-end completion percentiles. Polling runs on its own
thread pool so following jobs never throttles the offered rate. Sample against
a single local worker:

```
upload phase      9.9s
accepted (202)    80
throughput        8.0 accepted/s
upload latency (ms)   p50 58.35   p90 78.41   p95 86.4   p99 95.61
end-to-end completion (ms)  p50 4428.72   p90 7214.22   p99 7496.1
```

Uploads stay fast while end-to-end latency climbs: the queue is absorbing the
difference, which is exactly the backlog an autoscaler should react to.

---

## Tests

```bash
make test        # in a container, no host Python needed
make test-local  # host Python
make test-cov    # with a coverage report
```

197 tests, 92% coverage, entirely offline: moto stands in for AWS and the suite
scrubs the environment of real credentials, pointing
`AWS_SHARED_CREDENTIALS_FILE` and `AWS_CONFIG_FILE` at paths that do not exist.
`make test` runs them in a bare `python:3.12-slim` container with only
`requirements/dev.txt` installed, which doubles as a check that the pinned
closure is complete.

The failure paths carry as much weight as the happy path:

| File | Covers |
| --- | --- |
| `test_config.py` | Fail-fast on missing/invalid variables, defaults, immutability |
| `test_images.py` | Aspect ratio, no upscaling, alpha flattening, EXIF, GPS stripping |
| `test_api_upload.py` | Oversized, wrong type, corrupt, filename sanitising, write sequence |
| `test_api_jobs.py` | Status shapes, 404s, Decimal round-trip, label cardinality |
| `test_api_health.py` | Liveness independence, readiness failure, metric separation |
| `test_api_chaos.py` | Gating, each knob, merge semantics, validation |
| `test_worker_pipeline.py` | End to end, idempotency, poison messages, infra outages |
| `test_shutdown.py` | Signal handling, in-flight completion, watchdog, drain config |
| `test_state_isolation.py` | Temp cleanup, no working-directory writes, interchangeability |
| `test_tracing.py` | `traceparent` across the queue, silent degradation |
| `test_logging.py` | JSON shape, context binding, credential and GPS redaction |
| `test_retry.py` | Transient vs permanent classification, backoff bounds |

`fixtures/` holds committed test images, including `corrupt.jpg` (garbage
behind a JPEG header, rejected at upload) and `truncated.jpg` (a real JPEG cut
off mid-scan — the header parses, the decode does not, which is the poison
message the worker must survive). Regenerate them with `make fixtures`.

---

## Design notes

**Why the write order is S3 → DynamoDB → SQS.** A failure at any point leaves
either no trace or a record the client can observe, never a queued job with no
object behind it. If the enqueue fails after the record is written, the API
marks the job `FAILED` and returns `503`, so a poller gets a definitive answer
instead of a job stuck in `PENDING` forever.

**Why the message is deleted last.** DynamoDB is the record of truth. Deleting
the message before the update would turn a crash into a silently lost job;
deleting it after turns the same crash into a redelivery, which idempotency
absorbs.

**Two kinds of failure, two behaviours.** A bad upload is permanent: the job is
marked `FAILED` and the message deleted, because retrying will never help and a
poison message must not block the queue. A dependency outage is transient: the
message is left alone so SQS redelivers, and the redrive policy parks it on the
DLQ after three attempts. The distinction is `ImageProcessingError` versus
`TransientDependencyError` and it is what keeps the worker alive through both.

**Why validation decodes the image at the edge.** Rejecting undecodable bytes
in the API costs a header parse and saves a queue round-trip, a DynamoDB write
and a worker slot. The worker still handles corrupt input, because an object
can be replaced between upload and processing — and it is tested that way.

**Where the small amount of global state lives.** The chaos controller, and
nothing else. It is thread-safe, per-process, flag-gated and inert by default.

**Retries are layered.** botocore's `standard` mode handles the first tier; an
explicit ladder in `shared/retry.py` sits on top for what botocore gives up on.
Only errors that can plausibly succeed on retry are retried — `AccessDenied`,
`ValidationException`, `NoSuchKey` and `ConditionalCheckFailedException` raise
immediately, so a misconfiguration is visible in seconds rather than after a
backoff ladder.

---

## Repository layout

```
api/          FastAPI service: routes, upload/status logic, chaos controller
worker/       SQS consumer, job pipeline, visibility heartbeat
shared/       config, AWS wrappers, models, logging, metrics, tracing, images
loadgen/      load generator (Pillow + stdlib only)
tests/        pytest suite, offline via moto
fixtures/     committed test images + the script that regenerates them
docker/       Dockerfile.api, Dockerfile.worker, Dockerfile.loadgen,
              localstack/init-resources.sh
requirements/ *.in sources and fully pinned *.txt closures
```

All AWS access is isolated behind thin wrappers in `shared/aws.py`, which is
what lets the tests mock cleanly and what keeps retries, error classification
and SQS error metrics in exactly one place.

Dependencies are pinned to exact versions as complete transitive closures,
compiled per service so the worker image carries no web framework. Regenerate
with `make deps` (requires [uv](https://github.com/astral-sh/uv)).
