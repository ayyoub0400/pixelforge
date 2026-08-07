"""Generate upload load against a running pixelforge API.

Uploads synthetic JPEGs at a target rate for a fixed duration, then reports
throughput and latency percentiles. With ``--poll`` it also follows each job to
a terminal state and reports end-to-end completion times, which is the number
that actually tells you whether the worker fleet is keeping up.

Examples:
    Steady trickle, checking the whole pipeline works::

        python -m loadgen.generate --rate 2 --duration 20 --poll

    Burst big enough to drive worker autoscaling (queue depth climbs because
    uploads are accepted far faster than one worker can render them)::

        python -m loadgen.generate --rate 120 --duration 60 --concurrency 64

Only Pillow and the standard library are used, so the tool runs anywhere the
API image runs.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import queue
import random
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Final, Sequence

from PIL import Image

__all__ = ["main", "Results", "run_load"]

DEFAULT_BASE_URL: Final[str] = "http://localhost:8000"

#: Percentiles reported for every latency series.
_PERCENTILES: Final[tuple[int, ...]] = (50, 90, 95, 99)

#: How long to wait between polls of a single job.
_POLL_INTERVAL_SECONDS: Final[float] = 0.5

_TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({"COMPLETE", "FAILED"})


@dataclass
class Results:
    """Everything one run measured."""

    upload_latencies: list[float] = field(default_factory=list)
    completion_latencies: list[float] = field(default_factory=list)
    status_counts: Counter[str] = field(default_factory=Counter)
    terminal_counts: Counter[str] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)
    started_at: float = 0.0
    finished_at: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def wall_seconds(self) -> float:
        """Duration of the run in seconds."""
        return max(self.finished_at - self.started_at, 1e-9)

    @property
    def accepted(self) -> int:
        """Number of uploads the API answered with 202."""
        return self.status_counts.get("202", 0)

    @property
    def total_requests(self) -> int:
        """Total upload attempts, including failures."""
        return sum(self.status_counts.values()) + sum(self.errors.values())


def _percentiles(samples: Sequence[float]) -> dict[str, float]:
    """Compute the reported percentiles of a latency series, in milliseconds."""
    if not samples:
        return {f"p{p}": 0.0 for p in _PERCENTILES}
    ordered = sorted(samples)
    result: dict[str, float] = {}
    for percentile in _PERCENTILES:
        index = min(len(ordered) - 1, int(round((percentile / 100.0) * len(ordered))) - 1)
        result[f"p{percentile}"] = round(ordered[max(index, 0)] * 1000, 2)
    return result


def make_image(width: int, height: int, *, seed: int | None = None) -> bytes:
    """Render a synthetic JPEG.

    The image is filled with coloured noise rather than a flat colour so that
    JPEG cannot compress it to a few hundred bytes: the payload size needs to
    be representative for the upload path to be exercised honestly.

    Args:
        width: Image width in pixels.
        height: Image height in pixels.
        seed: Optional seed for reproducible payloads.

    Returns:
        Encoded JPEG bytes.
    """
    rng = random.Random(seed)
    # Build the noise at 1/8 scale and expand it with NEAREST: generating
    # 1600x1200 random pixels one at a time in Python takes seconds per image,
    # which would make the generator itself the bottleneck.
    small_width = max(1, width // 8)
    small_height = max(1, height // 8)
    noise = rng.randbytes(small_width * small_height * 3)
    blocky = Image.frombytes("RGB", (small_width, small_height), noise)
    image = blocky.resize((width, height), Image.Resampling.NEAREST)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=88)
    return buffer.getvalue()


def _multipart(payload: bytes, filename: str) -> tuple[bytes, str]:
    """Encode ``payload`` as a single-part multipart/form-data body.

    Returns:
        ``(body, content_type_header)``.
    """
    boundary = f"----pixelforge{uuid.uuid4().hex}"
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: image/jpeg\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head + payload + tail, f"multipart/form-data; boundary={boundary}"


def _post_upload(base_url: str, payload: bytes, filename: str, timeout: float) -> tuple[str, str | None]:
    """Upload one image.

    Returns:
        ``(status_code, job_id)``; ``job_id`` is ``None`` unless the API
        accepted the upload.
    """
    body, content_type = _multipart(payload, filename)
    request = urllib.request.Request(
        f"{base_url}/api/v1/jobs",
        data=body,
        method="POST",
        headers={"Content-Type": content_type, "Content-Length": str(len(body))},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            document = json.loads(response.read().decode("utf-8"))
            return str(response.status), document.get("job_id")
    except urllib.error.HTTPError as exc:
        exc.read()
        return str(exc.code), None


def _poll_job(base_url: str, job_id: str, timeout: float, deadline: float) -> str:
    """Poll a job until it reaches a terminal state or ``deadline`` passes.

    Returns:
        The final status seen, or ``"TIMEOUT"``.
    """
    while time.monotonic() < deadline:
        request = urllib.request.Request(f"{base_url}/api/v1/jobs/{job_id}", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                document = json.loads(response.read().decode("utf-8"))
                status = str(document.get("status", "UNKNOWN"))
                if status in _TERMINAL_STATUSES:
                    return status
        except urllib.error.HTTPError as exc:
            exc.read()
            if exc.code != 404:
                return f"HTTP_{exc.code}"
        except OSError:
            pass
        time.sleep(_POLL_INTERVAL_SECONDS)
    return "TIMEOUT"


def _worker(
    task_queue: "queue.Queue[float | None]",
    args: argparse.Namespace,
    payloads: Sequence[bytes],
    results: Results,
    origin: float,
) -> None:
    """Consume scheduled send times and issue the requests."""
    while True:
        scheduled = task_queue.get()
        try:
            if scheduled is None:
                return
            wait = origin + scheduled - time.monotonic()
            if wait > 0:
                time.sleep(wait)

            payload = random.choice(payloads)
            started = time.monotonic()
            try:
                status, job_id = _post_upload(
                    args.base_url, payload, f"load-{uuid.uuid4().hex[:8]}.jpg", args.timeout
                )
            except OSError as exc:
                with results.lock:
                    results.errors[type(exc).__name__] += 1
                continue

            upload_seconds = time.monotonic() - started
            with results.lock:
                results.status_counts[status] += 1
                results.upload_latencies.append(upload_seconds)

            if args.poll and job_id:
                final = _poll_job(
                    args.base_url,
                    job_id,
                    args.timeout,
                    time.monotonic() + args.poll_timeout,
                )
                with results.lock:
                    results.terminal_counts[final] += 1
                    if final in _TERMINAL_STATUSES:
                        results.completion_latencies.append(time.monotonic() - started)
        finally:
            task_queue.task_done()


def run_load(args: argparse.Namespace) -> Results:
    """Execute a load run and return its measurements."""
    total = max(1, int(round(args.rate * args.duration)))
    payloads = [
        make_image(args.width, args.height, seed=index) for index in range(args.distinct_images)
    ]

    results = Results()
    task_queue: "queue.Queue[float | None]" = queue.Queue()
    for index in range(total):
        task_queue.put(index / args.rate)
    for _ in range(args.concurrency):
        task_queue.put(None)

    print(
        f"→ {total} uploads at {args.rate}/s for {args.duration}s "
        f"across {args.concurrency} connections to {args.base_url}",
        file=sys.stderr,
    )

    origin = time.monotonic()
    results.started_at = origin
    threads = [
        threading.Thread(
            target=_worker,
            args=(task_queue, args, payloads, results, origin),
            name=f"loadgen-{index}",
            daemon=True,
        )
        for index in range(args.concurrency)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    results.finished_at = time.monotonic()
    return results


def report(results: Results, args: argparse.Namespace) -> None:
    """Print a human-readable summary to stdout."""
    upload = _percentiles(results.upload_latencies)
    lines = [
        "",
        "pixelforge load report",
        "──────────────────────",
        f"target            {args.base_url}",
        f"duration          {results.wall_seconds:.1f}s",
        f"requests          {results.total_requests}",
        f"accepted (202)    {results.accepted}",
        f"throughput        {results.accepted / results.wall_seconds:.1f} accepted/s",
        "",
        "upload latency (ms)",
        f"  p50 {upload['p50']:>9}   p90 {upload['p90']:>9}"
        f"   p95 {upload['p95']:>9}   p99 {upload['p99']:>9}",
        "",
        "status codes      "
        + (
            ", ".join(f"{code}={count}" for code, count in sorted(results.status_counts.items()))
            or "none"
        ),
    ]
    if results.errors:
        lines.append(
            "transport errors  "
            + ", ".join(f"{name}={count}" for name, count in sorted(results.errors.items()))
        )
    if args.poll:
        completion = _percentiles(results.completion_latencies)
        lines += [
            "",
            "end-to-end completion (ms, upload → COMPLETE)",
            f"  p50 {completion['p50']:>9}   p90 {completion['p90']:>9}"
            f"   p95 {completion['p95']:>9}   p99 {completion['p99']:>9}",
            "terminal states   "
            + (
                ", ".join(
                    f"{status}={count}" for status, count in sorted(results.terminal_counts.items())
                )
                or "none"
            ),
        ]
        if results.completion_latencies:
            lines.append(
                f"  mean {round(statistics.fmean(results.completion_latencies) * 1000, 2)} ms"
            )
    lines.append("")
    print("\n".join(lines))


def build_parser() -> argparse.ArgumentParser:
    """Define the command-line interface."""
    parser = argparse.ArgumentParser(
        prog="loadgen",
        description="Generate upload load against a pixelforge API.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("PIXELFORGE_BASE_URL", DEFAULT_BASE_URL),
        help="API base URL (env: PIXELFORGE_BASE_URL).",
    )
    parser.add_argument("--rate", type=float, default=5.0, help="Uploads per second.")
    parser.add_argument("--duration", type=float, default=30.0, help="Run length in seconds.")
    parser.add_argument("--concurrency", type=int, default=8, help="Parallel connections.")
    parser.add_argument("--width", type=int, default=1600, help="Generated image width.")
    parser.add_argument("--height", type=int, default=1200, help="Generated image height.")
    parser.add_argument(
        "--distinct-images",
        type=int,
        default=4,
        help="How many payloads to pre-render and cycle through.",
    )
    parser.add_argument(
        "--poll",
        action="store_true",
        help="Follow each accepted job until it is COMPLETE or FAILED.",
    )
    parser.add_argument(
        "--poll-timeout",
        type=float,
        default=120.0,
        help="Give up on a single job after this many seconds.",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-request HTTP timeout.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Console entrypoint."""
    args = build_parser().parse_args(argv)
    if args.rate <= 0 or args.duration <= 0 or args.concurrency <= 0:
        print("rate, duration and concurrency must all be positive", file=sys.stderr)
        return 2
    args.base_url = args.base_url.rstrip("/")
    args.distinct_images = max(1, args.distinct_images)

    results = run_load(args)
    report(results, args)
    # Non-zero when nothing was accepted, so CI can gate on it.
    return 0 if results.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
