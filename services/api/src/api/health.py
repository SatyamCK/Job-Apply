from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from common.logging import get_logger
from common.runtime import get_runtime

router = APIRouter(tags=["health"])
log = get_logger("api.health")


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness: the process is up."""
    return {"status": "ok"}


@router.get("/readyz")
def readyz() -> JSONResponse:
    """Readiness: config is loaded and the database answers."""
    rt = get_runtime()
    checks: dict[str, Any] = {"config_version": rt.config.version}
    ok = True
    try:
        with rt.require_sessions()() as session:
            session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        ok = False
        checks["database"] = "error"
        log.warning("readiness_check_failed", stage="api", error_type=type(exc).__name__)
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ok" if ok else "unavailable", "checks": checks},
    )
