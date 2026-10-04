from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from api import diagnostics, health
from common.runtime import Runtime, init_service

SERVICE_NAME = "api"


def create_app(init: Callable[[], Runtime] | None = None) -> FastAPI:
    """App factory. `init` lets tests supply a pre-built runtime instead of reading env vars."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        runtime = init() if init else init_service(SERVICE_NAME)
        try:
            yield
        finally:
            runtime.shutdown()

    app = FastAPI(
        title="Job Applier API",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.include_router(health.router, prefix="/api")
    app.include_router(diagnostics.router, prefix="/api")
    FastAPIInstrumentor.instrument_app(app, excluded_urls="/api/healthz,/api/readyz")
    return app


app = create_app()
