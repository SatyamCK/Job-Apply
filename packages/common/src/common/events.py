"""`emit_event()`: one call writes the `events` row, logs, and emits metrics (SPEC §11).

Call it at the start, end and error of every stage and on every status change. The `stage()`
context manager does start/end/error plus a span and duration for you.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry.trace import Status, StatusCode
from sqlalchemy.orm import Session

from common.db.models import Event
from common.db.session import session_scope
from common.logging import get_logger, redact
from common.runtime import get_runtime
from common.tracing import current_trace_id, get_tracer

log = get_logger("common.events")
tracer = get_tracer("common.events")

# Entity types whose ID is also surfaced as a first-class log field.
_LOG_ID_FIELDS = {"job": "job_id", "application": "application_id", "user": "user_id"}


def emit_event(
    *,
    stage: str,
    event_type: str,
    entity_type: str,
    entity_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    status: str = "ok",
    duration_ms: int | None = None,
    details: dict[str, Any] | None = None,
    error_type: str | None = None,
    metric: str | None = None,
    metric_value: float = 1,
    session: Session | None = None,
) -> Event:
    """Record one timeline event.

    With `session`, the row joins the caller's transaction (and fails with it). Without one,
    it is written in its own transaction; a write failure is logged but never breaks the caller.
    """
    rt = get_runtime()
    safe_details = redact(details or {})
    if error_type:
        safe_details.setdefault("error_type", error_type)
    row = Event(
        id=uuid.uuid4(),
        trace_id=current_trace_id(),
        entity_type=entity_type,
        entity_id=entity_id,
        user_id=user_id,
        stage=stage,
        event_type=event_type,
        status=status,
        duration_ms=duration_ms,
        details=safe_details,
    )

    if session is not None:
        session.add(row)
        session.flush()
    else:
        try:
            with session_scope(rt.require_sessions()) as own:
                own.add(row)
        except Exception as exc:
            log.error(
                "event_write_failed",
                stage=stage,
                event_type=event_type,
                error_type=type(exc).__name__,
            )

    log_fields: dict[str, Any] = {
        "stage": stage,
        "entity_type": entity_type,
        "entity_id": str(entity_id) if entity_id else None,
        "user_id": str(user_id) if user_id else None,
        "status": status,
        "duration_ms": duration_ms,
        "error_type": error_type,
        "details": safe_details,
    }
    id_field = _LOG_ID_FIELDS.get(entity_type)
    if id_field and entity_id and not log_fields.get(id_field):
        log_fields[id_field] = str(entity_id)
    if status == "error":
        log.error(event_type, **log_fields)
    else:
        log.info(event_type, **log_fields)

    if metric:
        rt.metrics.put(metric, metric_value, stage=stage)
    if status == "error":
        rt.metrics.put("stage_errors", 1, stage=stage)
    if duration_ms is not None:
        rt.metrics.put("stage_duration_ms", duration_ms, stage=stage)
    return row


@contextmanager
def stage(
    name: str,
    *,
    entity_type: str,
    entity_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Run a stage inside a span, emitting `started` and then `completed` or `failed`.

    Yields a dict the caller can add result details (counts, IDs) to; they go on the
    `completed` event.
    """
    result: dict[str, Any] = {}
    common: dict[str, Any] = {
        "stage": name,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "user_id": user_id,
    }
    with tracer.start_as_current_span(f"stage.{name}") as span:
        span.set_attribute("stage", name)
        span.set_attribute("entity_type", entity_type)
        if entity_id:
            span.set_attribute("entity_id", str(entity_id))
        emit_event(event_type="started", details=details, **common)
        started = time.perf_counter()
        try:
            yield result
        except Exception as exc:
            duration = int((time.perf_counter() - started) * 1000)
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            emit_event(
                event_type="failed",
                status="error",
                duration_ms=duration,
                error_type=type(exc).__name__,
                details={**(details or {}), **result},
                **common,
            )
            raise
        duration = int((time.perf_counter() - started) * 1000)
        emit_event(
            event_type="completed",
            duration_ms=duration,
            details={**(details or {}), **result},
            **common,
        )
