"""Job submission and status endpoints."""

from __future__ import annotations

from typing import Final

import structlog
from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from api.chaos import apply_chaos
from api.deps import get_config, get_service
from api.service import JobService, UploadRejectedError
from shared.config import Config
from shared.metrics import UPLOADS_TOTAL, UploadResult
from shared.models import JobAcceptedResponse, JobRecord
from shared.tracing import span

__all__ = ["router"]

_LOG = structlog.get_logger(__name__)

# The chaos dependency is attached at the router so it covers every job
# endpoint and nothing else: /healthz, /readyz and /metrics keep telling the
# truth while failures are being injected.
router = APIRouter(prefix="/api/v1", tags=["jobs"], dependencies=[Depends(apply_chaos)])

#: Upload body is consumed in chunks so an oversized payload is rejected after
#: one megabyte rather than after buffering the whole thing.
_CHUNK_BYTES: Final[int] = 1 << 20

#: Multipart framing adds boundary and header overhead on top of the file, so
#: the early Content-Length rejection allows a margin before it fires.
_MULTIPART_OVERHEAD_BYTES: Final[int] = 8192


async def _read_limited(upload: UploadFile, max_bytes: int) -> bytes:
    """Read an upload into memory, refusing to exceed ``max_bytes``.

    Args:
        upload: The multipart file part.
        max_bytes: Hard ceiling from ``MAX_UPLOAD_BYTES``.

    Returns:
        The complete payload.

    Raises:
        UploadRejectedError: The payload is larger than the configured limit.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise UploadRejectedError(
                detail=f"file exceeds the {max_bytes} byte limit",
                status_code=413,
                code="payload_too_large",
                metric_result=UploadResult.REJECTED_TOO_LARGE,
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(
    "/jobs",
    status_code=202,
    response_model=JobAcceptedResponse,
    summary="Submit an image for asynchronous processing",
    responses={
        400: {"description": "The payload is not a decodable image."},
        413: {"description": "The payload exceeds MAX_UPLOAD_BYTES."},
        415: {"description": "The declared content type is not a supported image type."},
        503: {"description": "A dependency was unavailable; retry."},
    },
)
async def create_job(
    request: Request,
    file: UploadFile = File(..., description="The image to process."),
    service: JobService = Depends(get_service),
    config: Config = Depends(get_config),
) -> JSONResponse:
    """Accept an upload and return immediately with a job id.

    The response is ``202 Accepted``: the image has been stored and queued, but
    no thumbnails exist yet. Poll ``GET /api/v1/jobs/{job_id}`` for the result.
    """
    declared_length = request.headers.get("content-length")
    if (
        declared_length
        and declared_length.isdigit()
        and int(declared_length) > config.max_upload_bytes + _MULTIPART_OVERHEAD_BYTES
    ):
        raise UploadRejectedError(
            detail=f"file exceeds the {config.max_upload_bytes} byte limit",
            status_code=413,
            code="payload_too_large",
            metric_result=UploadResult.REJECTED_TOO_LARGE,
        )

    with span("api.create_job"):
        try:
            data = await _read_limited(file, config.max_upload_bytes)
            record = await run_in_threadpool(
                service.create_job,
                data=data,
                filename=file.filename,
                content_type=file.content_type,
            )
        except UploadRejectedError:
            # Counted by the exception handler, which also sees rejections
            # raised before this block.
            raise
        except Exception:
            # Dependency failures are counted here rather than in the service
            # so that every terminated upload attempt lands in exactly one
            # bucket of pixelforge_uploads_total.
            UPLOADS_TOTAL.labels(result=UploadResult.ERROR).inc()
            raise

    return JSONResponse(
        status_code=202,
        content=JobAcceptedResponse(job_id=record.job_id, status=record.status).model_dump(
            mode="json"
        ),
    )


@router.get(
    "/jobs/{job_id}",
    summary="Fetch the current state of a job",
    responses={404: {"description": "No job exists with that id."}},
)
async def get_job(
    job_id: str,
    service: JobService = Depends(get_service),
) -> JSONResponse:
    """Return the job record.

    While the job is ``PENDING`` or ``PROCESSING`` the record carries only the
    submission details. Once ``COMPLETE`` it also carries ``outputs`` (the S3
    keys and dimensions of each thumbnail) and ``exif``. A ``FAILED`` job
    carries ``error``.
    """
    with span("api.get_job", attributes={"job.id": job_id}):
        record: JobRecord | None = await run_in_threadpool(service.get_job, job_id)

    if record is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"no job with id {job_id}", "code": "job_not_found"},
        )
    return JSONResponse(status_code=200, content=record.public_view())
