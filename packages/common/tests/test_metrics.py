from __future__ import annotations

import io
import json

import pytest

from common.metrics import METRICS, MetricsEmitter, UnknownMetricError


@pytest.fixture
def emitter():
    ns = {"value": "JobApplier"}
    out = io.StringIO()
    return (
        MetricsEmitter(namespace=lambda: ns["value"], env="local", service="svc", stream=out),
        out,
        ns,
    )


def test_emf_shape(emitter):
    m, out, _ = emitter
    m.put("jobs_fetched", 12, stage="fetch", source="greenhouse")
    record = json.loads(out.getvalue())
    directive = record["_aws"]["CloudWatchMetrics"][0]
    assert directive["Namespace"] == "JobApplier"
    assert directive["Dimensions"] == [["env", "source", "stage"]]
    assert directive["Metrics"] == [{"Name": "jobs_fetched", "Unit": "Count"}]
    assert isinstance(record["_aws"]["Timestamp"], int)
    assert record["jobs_fetched"] == 12
    assert record["env"] == "local"
    assert record["stage"] == "fetch"
    assert record["source"] == "greenhouse"


def test_units_and_none_dimensions_dropped(emitter):
    m, out, _ = emitter
    m.put("llm_latency_ms", 830.5, task="draft_email", provider=None)
    record = json.loads(out.getvalue())
    assert record["_aws"]["CloudWatchMetrics"][0]["Metrics"][0]["Unit"] == "Milliseconds"
    assert record["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["env", "task"]]
    assert "provider" not in record


def test_namespace_is_read_each_time(emitter):
    m, out, ns = emitter
    ns["value"] = "Other"
    m.put("approvals")
    assert json.loads(out.getvalue())["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "Other"


def test_rejects_unknown_metric_and_dimension(emitter):
    m, _, _ = emitter
    with pytest.raises(UnknownMetricError):
        m.put("jobs_fetchd")
    with pytest.raises(ValueError, match="dimensions"):
        m.put("jobs_fetched", user_id="u-1")


def test_catalogue_covers_spec_metrics():
    spec = {
        # pipeline
        "jobs_fetched", "jobs_new", "jobs_deduped", "embeddings_created", "matches_created",
        "shortlisted", "contacts_found", "contacts_verified", "drafts_created",
        "claim_check_failed", "approvals", "emails_sent", "emails_failed", "bounces_reported",
        "opt_outs", "cap_hits",
        # llm
        "llm_calls", "llm_errors", "llm_latency_ms", "llm_input_tokens", "llm_output_tokens",
        "llm_cost_usd",
        # stages and external APIs
        "stage_duration_ms", "stage_errors", "external_api_calls", "external_api_errors",
    }  # fmt: skip
    assert spec == set(METRICS)
