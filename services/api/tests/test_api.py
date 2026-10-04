from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from api.main import create_app
from common.db.models import Event
from common.db.session import session_scope
from common.runtime import init_service, set_runtime


@pytest.fixture
def client_for(make_manager, tracer_provider):
    """Build a TestClient around a runtime made from the given config (and optional DB)."""
    clients = []

    def _make(config_data, database_url=None):
        manager, _ = make_manager(config_data)

        def init():
            return init_service(
                "api",
                config_manager=manager,
                with_db=database_url is not None,
                database_url=database_url,
                tracer_provider=tracer_provider,
                poll_config=False,
            )

        client = TestClient(create_app(init=init))
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for c in clients:
        c.__exit__(None, None, None)
    set_runtime(None)


def test_healthz(client_for, config_data):
    client = client_for(config_data)
    assert client.get("/api/healthz").json() == {"status": "ok"}


def test_readyz_unavailable_without_db(client_for, config_data):
    resp = client_for(config_data).get("/api/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["database"] == "error"


def test_readyz_ok_with_db(client_for, config_data, migrated_db):
    resp = client_for(config_data, migrated_db).get("/api/readyz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["checks"]["config_version"] == "v1"


def test_ping_writes_events_with_request_trace(client_for, config_data, migrated_db, span_exporter):
    client = client_for(config_data, migrated_db)
    resp = client.post("/api/diagnostics/ping")
    assert resp.status_code == 200
    trace_id = resp.json()["trace_id"]
    assert trace_id and len(trace_id) == 32

    from common.runtime import get_runtime

    with session_scope(get_runtime().require_sessions()) as s:
        rows = list(
            s.scalars(select(Event).where(Event.trace_id == trace_id).order_by(Event.created_at))
        )
    assert [r.event_type for r in rows] == ["started", "completed"]

    span_names = {
        s.name
        for s in span_exporter.get_finished_spans()
        if format(s.context.trace_id, "032x") == trace_id
    }
    assert "stage.diagnostics" in span_names
    assert any("diagnostics/ping" in n for n in span_names)  # the FastAPI server span


def test_ping_disabled_outside_local_and_dev(client_for, config_data):
    config_data["environment"] = "prod"
    assert client_for(config_data).post("/api/diagnostics/ping").status_code == 404
