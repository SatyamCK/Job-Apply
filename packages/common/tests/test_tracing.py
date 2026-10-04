from __future__ import annotations

from opentelemetry import trace

from common.tracing import (
    current_span_id,
    current_trace_id,
    extract_headers,
    extract_sqs_attributes,
    inject_headers,
    inject_sqs_attributes,
)


def test_no_trace_outside_span(tracer_provider):
    assert current_trace_id() is None
    assert current_span_id() is None


def test_http_header_round_trip(tracer_provider):
    tracer = trace.get_tracer("t")
    with tracer.start_as_current_span("producer") as span:
        headers = inject_headers({"Accept": "application/json"})
        trace_id = span.get_span_context().trace_id
    assert headers["Accept"] == "application/json"
    assert headers["traceparent"].startswith("00-")

    ctx = extract_headers({"Traceparent": headers["traceparent"]})
    with tracer.start_as_current_span("consumer", context=ctx) as child:
        assert child.get_span_context().trace_id == trace_id
        assert current_trace_id() == format(trace_id, "032x")


def test_sqs_attribute_round_trip(tracer_provider):
    tracer = trace.get_tracer("t")
    with tracer.start_as_current_span("enqueue") as span:
        attrs = inject_sqs_attributes({"kind": {"DataType": "String", "StringValue": "x"}})
        trace_id = span.get_span_context().trace_id
    assert attrs["traceparent"]["DataType"] == "String"
    assert attrs["kind"]["StringValue"] == "x"

    ctx = extract_sqs_attributes(attrs)
    with tracer.start_as_current_span("consume", context=ctx) as child:
        assert child.get_span_context().trace_id == trace_id


def test_extract_without_traceparent_starts_new_trace(tracer_provider):
    ctx = extract_sqs_attributes({})
    with trace.get_tracer("t").start_as_current_span("consume", context=ctx) as span:
        assert span.get_span_context().is_valid
        assert span.parent is None
