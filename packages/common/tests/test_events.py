from __future__ import annotations

import io
import json
import uuid

import pytest
from opentelemetry import trace
from sqlalchemy import select

from common.db.models import Event
from common.db.session import session_scope
from common.events import emit_event, stage


@pytest.fixture
def metrics_out(runtime):
    out = io.StringIO()
    runtime.metrics._stream = out
    return out


def metric_names(out: io.StringIO) -> list[str]:
    names = []
    for line in out.getvalue().splitlines():
        record = json.loads(line)
        names.append(record["_aws"]["CloudWatchMetrics"][0]["Metrics"][0]["Name"])
    return names


def events_for(runtime, trace_id: str) -> list[Event]:
    with session_scope(runtime.require_sessions()) as s:
        return list(
            s.scalars(select(Event).where(Event.trace_id == trace_id).order_by(Event.created_at))
        )


def test_emit_event_writes_row_logs_and_metrics(runtime, metrics_out, span_exporter):
    job_id = uuid.uuid4()
    with trace.get_tracer("t").start_as_current_span("fetch") as span:
        emit_event(
            stage="fetch",
            event_type="job_fetched",
            entity_type="job",
            entity_id=job_id,
            details={"source": "greenhouse", "contact": "hr@acme.com", "body": "secret text"},
            metric="jobs_fetched",
        )
        trace_id = format(span.get_span_context().trace_id, "032x")

    (row,) = events_for(runtime, trace_id)
    assert row.entity_id == job_id
    assert row.stage == "fetch"
    assert row.status == "ok"
    assert row.details == {"source": "greenhouse", "contact": "h***@acme.com", "body": "[REDACTED]"}
    assert metric_names(metrics_out) == ["jobs_fetched"]


def test_stage_success_emits_started_and_completed(runtime, metrics_out, span_exporter):
    with stage("normalize", entity_type="job", entity_id=uuid.uuid4()) as result:
        result["embedded"] = True

    (span,) = [s for s in span_exporter.get_finished_spans() if s.name == "stage.normalize"]
    trace_id = format(span.context.trace_id, "032x")
    rows = events_for(runtime, trace_id)
    assert [r.event_type for r in rows] == ["started", "completed"]
    assert rows[1].duration_ms is not None and rows[1].duration_ms >= 0
    assert rows[1].details == {"embedded": True}
    assert metric_names(metrics_out) == ["stage_duration_ms"]


def test_stage_failure_emits_failed_and_reraises(runtime, metrics_out, span_exporter):
    with pytest.raises(ZeroDivisionError), stage("match", entity_type="job"):
        _ = 1 / 0

    (span,) = [s for s in span_exporter.get_finished_spans() if s.name == "stage.match"]
    assert span.status.status_code == trace.StatusCode.ERROR
    rows = events_for(runtime, format(span.context.trace_id, "032x"))
    assert [(r.event_type, r.status) for r in rows] == [("started", "ok"), ("failed", "error")]
    assert rows[1].details["error_type"] == "ZeroDivisionError"
    assert metric_names(metrics_out) == ["stage_errors", "stage_duration_ms"]


def test_event_in_caller_session_rolls_back_with_it(runtime, span_exporter):
    with trace.get_tracer("t").start_as_current_span("tx") as span:
        trace_id = format(span.get_span_context().trace_id, "032x")
        session = runtime.require_sessions()()
        try:
            emit_event(
                stage="send",
                event_type="status_changed",
                entity_type="application",
                session=session,
            )
            session.rollback()
        finally:
            session.close()
    assert events_for(runtime, trace_id) == []


def test_db_write_failure_does_not_break_caller(runtime, metrics_out):
    # entity_type longer than the column allows -> insert fails -> logged, not raised.
    row = emit_event(stage="fetch", event_type="x", entity_type="e" * 100)
    assert row.event_type == "x"
