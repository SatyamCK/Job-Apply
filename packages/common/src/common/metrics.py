"""CloudWatch Embedded Metric Format (EMF) emitter.

Each metric is one JSON line on stdout; CloudWatch Logs extracts it into a metric. Locally the
lines are just logs, and the admin console computes metrics from the `events` table instead.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, TextIO

# Metric catalogue from SPEC §11, with CloudWatch units. Unknown names are rejected so
# dashboards and alarms never silently miss a misspelled metric.
METRICS: dict[str, str] = {
    # pipeline
    "jobs_fetched": "Count",
    "jobs_new": "Count",
    "jobs_deduped": "Count",
    "embeddings_created": "Count",
    "matches_created": "Count",
    "shortlisted": "Count",
    "contacts_found": "Count",
    "contacts_verified": "Count",
    "drafts_created": "Count",
    "claim_check_failed": "Count",
    "approvals": "Count",
    "emails_sent": "Count",
    "emails_failed": "Count",
    "bounces_reported": "Count",
    "opt_outs": "Count",
    "cap_hits": "Count",
    # llm
    "llm_calls": "Count",
    "llm_errors": "Count",
    "llm_latency_ms": "Milliseconds",
    "llm_input_tokens": "Count",
    "llm_output_tokens": "Count",
    "llm_cost_usd": "None",
    # stages and external APIs
    "stage_duration_ms": "Milliseconds",
    "stage_errors": "Count",
    "external_api_calls": "Count",
    "external_api_errors": "Count",
}

ALLOWED_DIMENSIONS = frozenset({"stage", "source", "task", "provider"})


class UnknownMetricError(ValueError):
    pass


class MetricsEmitter:
    def __init__(
        self,
        *,
        namespace: Callable[[], str],
        env: str,
        service: str,
        stream: TextIO | None = None,
    ) -> None:
        # `namespace` is a callable so a hot-reloaded config value is picked up.
        self._namespace = namespace
        self._env = env
        self._service = service
        self._stream = stream or sys.stdout
        self._lock = threading.Lock()

    def put(self, name: str, value: float = 1, **dimensions: str | None) -> dict[str, Any]:
        unit = METRICS.get(name)
        if unit is None:
            raise UnknownMetricError(name)
        bad = set(dimensions) - ALLOWED_DIMENSIONS
        if bad:
            raise ValueError(f"unsupported metric dimensions: {sorted(bad)}")
        dims = {"env": self._env, **{k: v for k, v in dimensions.items() if v is not None}}
        record: dict[str, Any] = {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": self._namespace(),
                        "Dimensions": [sorted(dims)],
                        "Metrics": [{"Name": name, "Unit": unit}],
                    }
                ],
            },
            **dims,
            "service": self._service,
            name: value,
        }
        line = json.dumps(record, separators=(",", ":"))
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()
        return record
