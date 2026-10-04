from __future__ import annotations

import json
import uuid

import pytest
from opentelemetry import trace

from common.queues import InvalidPayloadError, QueueMessage, send_message_kwargs
from common.tracing import extract_sqs_attributes


def test_envelope_round_trip():
    job_id = uuid.uuid4()
    msg = QueueMessage(type="job.normalize", payload={"job_id": job_id, "user_ids": [1, "u-2"]})
    body = json.loads(msg.to_body())
    assert set(body) == {"message_id", "type", "created_at", "payload"}
    assert body["payload"] == {"job_id": str(job_id), "user_ids": ["1", "u-2"]}

    again = QueueMessage.from_body(msg.to_body())
    assert again == msg


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "jane@acme.com"},
        {"job_id": {"nested": 1}},
        {"user_ids": "not-a-list"},
        {"user_ids": [{"x": 1}]},
    ],
)
def test_rejects_non_id_payloads(payload):
    with pytest.raises(InvalidPayloadError):
        QueueMessage(type="x", payload=payload)


def test_send_kwargs_carry_traceparent(tracer_provider):
    msg = QueueMessage(type="job.match", payload={"job_id": "j-1"})
    with trace.get_tracer("t").start_as_current_span("enqueue") as span:
        kwargs = send_message_kwargs("http://q/fetch", msg)
        trace_id = span.get_span_context().trace_id
    assert kwargs["QueueUrl"] == "http://q/fetch"
    assert json.loads(kwargs["MessageBody"])["type"] == "job.match"
    ctx = extract_sqs_attributes(kwargs["MessageAttributes"])
    assert trace.get_current_span(ctx).get_span_context().trace_id == trace_id
