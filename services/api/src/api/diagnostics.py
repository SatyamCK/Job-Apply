"""Diagnostics endpoint used by `make smoke` to prove events and traces flow end to end.

Only enabled in the `local` and `dev` environments; returns 404 elsewhere.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from common.events import stage
from common.runtime import get_runtime
from common.tracing import current_trace_id

router = APIRouter(tags=["diagnostics"])

ENABLED_ENVIRONMENTS = frozenset({"local", "dev"})


@router.post("/diagnostics/ping")
def ping() -> dict[str, str | None]:
    rt = get_runtime()
    if rt.config.environment not in ENABLED_ENVIRONMENTS:
        raise HTTPException(status_code=404)
    with stage("diagnostics", entity_type="system") as result:
        result["config_version"] = rt.config.version
    return {"status": "ok", "trace_id": current_trace_id()}
