"""Liveness, readiness and metrics endpoints.

The distinction matters to Kubernetes:

* ``/healthz`` answers "is this process alive". It touches nothing external, so
  a DynamoDB outage can never trigger a restart loop across the fleet.
* ``/readyz`` answers "should this pod receive traffic". It probes S3, SQS and
  DynamoDB and returns ``503`` if any of them is unreachable, so a pod that
  cannot do its job is pulled from the Service endpoints instead of failing
  requests.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from api.chaos import ChaosController
from api.deps import get_chaos, get_probe
from shared.aws import ReadinessProbe
from shared.metrics import render_metrics
from shared.models import ReadinessResponse

__all__ = ["router"]

_LOG = structlog.get_logger(__name__)

router = APIRouter(tags=["operations"])


@router.get(
    "/healthz",
    summary="Liveness probe",
    response_model=dict,
)
async def healthz() -> JSONResponse:
    """Report that the process is running.

    Deliberately checks nothing external: a liveness probe that depends on a
    downstream service turns a dependency blip into a fleet-wide restart storm.
    """
    return JSONResponse(status_code=200, content={"status": "ok", "service": "api"})


@router.get(
    "/readyz",
    summary="Readiness probe",
    response_model=ReadinessResponse,
    responses={503: {"description": "At least one dependency is unreachable."}},
)
async def readyz(
    probe: ReadinessProbe = Depends(get_probe),
    chaos: ChaosController = Depends(get_chaos),
) -> JSONResponse:
    """Verify S3, SQS and DynamoDB are reachable.

    Returns ``200`` when all three answer, ``503`` otherwise. The per-dependency
    result is included in the body so an operator can tell which one is down
    without reading logs.
    """
    if chaos.fail_readiness:
        _LOG.warning("readiness_forced_failure")
        return JSONResponse(
            status_code=503,
            content=ReadinessResponse(
                status="not_ready", checks={"chaos": "readiness failure forced"}
            ).model_dump(),
        )

    ready, checks = await run_in_threadpool(probe.run)
    body = ReadinessResponse(status="ready" if ready else "not_ready", checks=checks)
    return JSONResponse(status_code=200 if ready else 503, content=body.model_dump())


@router.get(
    "/metrics",
    summary="Prometheus metrics",
    response_class=Response,
)
async def metrics() -> Response:
    """Expose the metric registry in Prometheus text exposition format."""
    payload, content_type = render_metrics()
    return Response(content=payload, media_type=content_type)
