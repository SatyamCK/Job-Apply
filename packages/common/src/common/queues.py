"""SQS message envelope (SPEC §4 queue rules).

Bodies carry IDs only, never PII: `{message_id, type, created_at, payload: {...ids}}`.
The trace context travels as the `traceparent` message attribute.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from common.tracing import inject_sqs_attributes

IdValue = str | int | uuid.UUID


class InvalidPayloadError(ValueError):
    pass


def _check_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Allow only `*_id` / `*_ids` keys with scalar ID values, so PII can't slip into a queue."""
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if key.endswith("_ids"):
            if not isinstance(value, list) or not all(isinstance(v, IdValue) for v in value):
                raise InvalidPayloadError(f"{key} must be a list of IDs")
            clean[key] = [str(v) for v in value]
        elif key.endswith("_id"):
            if not isinstance(value, IdValue):
                raise InvalidPayloadError(f"{key} must be an ID")
            clean[key] = str(value)
        else:
            raise InvalidPayloadError(f"payload key {key!r} is not an ID field (*_id / *_ids)")
    return clean


@dataclass(frozen=True)
class QueueMessage:
    type: str
    payload: dict[str, Any]
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _check_payload(self.payload))

    def to_body(self) -> str:
        return json.dumps(
            {
                "message_id": self.message_id,
                "type": self.type,
                "created_at": self.created_at,
                "payload": self.payload,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_body(cls, body: str) -> QueueMessage:
        data = json.loads(body)
        return cls(
            type=data["type"],
            payload=data["payload"],
            message_id=data["message_id"],
            created_at=data["created_at"],
        )


def send_message_kwargs(queue_url: str, message: QueueMessage) -> dict[str, Any]:
    """Arguments for boto3 `sqs.send_message`, with the current trace context attached."""
    return {
        "QueueUrl": queue_url,
        "MessageBody": message.to_body(),
        "MessageAttributes": inject_sqs_attributes(),
    }
