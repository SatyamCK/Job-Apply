from __future__ import annotations

import io
import json
import logging

import pytest
from opentelemetry import trace

from common.logging import (
    REQUIRED_FIELDS,
    configure_logging,
    get_logger,
    mask_email,
    redact,
    set_log_level,
)


@pytest.fixture
def stream():
    out = io.StringIO()
    configure_logging(service="svc", env="local", version="1.2.3", level="INFO", stream=out)
    yield out
    configure_logging(service="test", env="local", version="dev", level="INFO")


def lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_every_required_field_is_present(stream):
    get_logger("t").info("job_fetched", job_id="j-1", stage="fetch")
    (record,) = lines(stream)
    assert set(REQUIRED_FIELDS) <= set(record)
    assert record["service"] == "svc"
    assert record["env"] == "local"
    assert record["version"] == "1.2.3"
    assert record["event"] == "job_fetched"
    assert record["message"] == "job_fetched"
    assert record["level"] == "info"
    assert record["job_id"] == "j-1"


def test_trace_ids_come_from_active_span(stream, tracer_provider):
    with trace.get_tracer("t").start_as_current_span("s") as span:
        get_logger("t").info("inside")
        ctx = span.get_span_context()
    (record,) = lines(stream)
    assert record["trace_id"] == format(ctx.trace_id, "032x")
    assert record["span_id"] == format(ctx.span_id, "016x")


def test_emails_are_masked_everywhere(stream):
    get_logger("t").info(
        "contact_found",
        message="found jane.doe@acme.com",
        details={"to": ["bob@example.co.uk"]},
    )
    raw = stream.getvalue()
    assert "jane.doe@acme.com" not in raw
    assert "bob@example.co.uk" not in raw
    record = lines(stream)[0]
    assert record["message"] == "found j***@acme.com"
    assert record["details"]["to"] == ["b***@example.co.uk"]


def test_sensitive_keys_are_redacted(stream):
    get_logger("t").info(
        "draft_created",
        body="Dear hiring manager...",
        details={"refresh_token": "1//abc", "nested": {"resume_text": "10 years of..."}},
    )
    record = lines(stream)[0]
    assert record["body"] == "[REDACTED]"
    assert record["details"]["refresh_token"] == "[REDACTED]"
    assert record["details"]["nested"]["resume_text"] == "[REDACTED]"
    assert "Dear hiring manager" not in stream.getvalue()


def test_stdlib_logs_are_json_and_redacted(stream):
    logging.getLogger("thirdparty").warning("sent to someone@corp.io")
    record = lines(stream)[0]
    assert record["level"] == "warning"
    assert record["event"] == "sent to s***@corp.io"
    assert record["service"] == "svc"


def test_set_log_level_filters(stream):
    log = get_logger("t")
    log.debug("hidden")
    set_log_level("DEBUG")
    log.debug("shown")
    set_log_level("ERROR")
    log.info("hidden_again")
    assert [r["event"] for r in lines(stream)] == ["shown"]


def test_mask_email_and_redact_helpers():
    assert mask_email("a@b.co") == "a***@b.co"
    assert mask_email("no email here") == "no email here"
    assert redact({"Token": "x", "ok": ("c@d.io",)}) == {"Token": "[REDACTED]", "ok": ["c***@d.io"]}
