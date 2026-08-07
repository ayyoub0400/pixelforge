"""Chaos controls, registered only when ``ENABLE_CHAOS_ENDPOINT`` is true.

When the flag is false the router is never mounted, so the path returns ``404``
and there is no code path that could be tripped accidentally in production.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends

from api.chaos import ChaosController
from api.deps import get_chaos
from shared.models import ChaosRequest, ChaosState

__all__ = ["router"]

_LOG = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["chaos"])


@router.post(
    "/chaos",
    response_model=ChaosState,
    summary="Inject failures into this API replica",
)
async def set_chaos(
    body: ChaosRequest,
    chaos: ChaosController = Depends(get_chaos),
) -> ChaosState:
    """Apply chaos settings to subsequent requests.

    Fields omitted from the body keep their current value. Settings are
    per-replica and are lost on restart, which is exactly the behaviour you
    want for a demo: rolling the deployment clears them.
    """
    state = chaos.apply(body)
    _LOG.warning(
        "chaos_configured",
        fail_readiness=state.fail_readiness,
        latency_ms=state.latency_ms,
        error_rate=state.error_rate,
    )
    return state
