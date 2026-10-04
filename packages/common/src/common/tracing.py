"""OpenTelemetry setup and W3C `traceparent` propagation over HTTP headers and SQS attributes.

Spans export over OTLP/HTTP: to Jaeger locally, to the ADOT collector sidecar (-> X-Ray) on AWS.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

TRACEPARENT = "traceparent"
_propagator = TraceContextTextMapPropagator()


def configure_tracing(
    *,
    service: str,
    env: str,
    version: str,
    sample_rate: float,
    otlp_endpoint: str | None,
) -> TracerProvider:
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": service, "service.version": version, "deployment.environment": env}
        ),
        sampler=ParentBased(TraceIdRatioBased(sample_rate)),
    )
    if otlp_endpoint:
        exporter = OTLPSpanExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/traces")
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return provider


def get_tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


def current_span_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.span_id, "016x") if ctx.is_valid else None


# --- HTTP headers -----------------------------------------------------------------------------


def inject_headers(headers: dict[str, str] | None = None) -> dict[str, str]:
    """Add `traceparent` (and `tracestate`) for the current span to outgoing HTTP headers."""
    carrier = dict(headers or {})
    _propagator.inject(carrier)
    return carrier


def extract_headers(headers: Mapping[str, str]) -> otel_context.Context:
    return _propagator.extract({k.lower(): v for k, v in headers.items()})


# --- SQS message attributes -------------------------------------------------------------------


def inject_sqs_attributes(attributes: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return SQS MessageAttributes with `traceparent` for the current span added."""
    attrs = dict(attributes or {})
    carrier: dict[str, str] = {}
    _propagator.inject(carrier)
    for key, value in carrier.items():
        attrs[key] = {"DataType": "String", "StringValue": value}
    return attrs


def extract_sqs_attributes(attributes: Mapping[str, Any]) -> otel_context.Context:
    """Rebuild the trace context from a received message's MessageAttributes."""
    carrier: dict[str, str] = {}
    for key, value in attributes.items():
        if isinstance(value, Mapping) and "StringValue" in value:
            carrier[key.lower()] = str(value["StringValue"])
    return _propagator.extract(carrier)
