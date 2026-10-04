from __future__ import annotations

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from common.db import models  # noqa: F401
from common.db.base import Base
from common.db.session import normalize_url
from conftest import alembic_config

SPEC_TABLES = {
    "users", "profiles", "resumes", "preferences", "mailbox_connections", "companies", "jobs",
    "matches", "contacts", "applications", "suppression_list", "llm_calls", "events",
    "pipeline_runs", "external_api_usage", "audit_log", "data_requests",
}  # fmt: skip


def test_schema_has_every_spec_table_and_index(migrated_db):
    engine = create_engine(normalize_url(migrated_db))
    insp = inspect(engine)
    assert set(insp.get_table_names()) >= SPEC_TABLES

    def index_cols(table: str) -> dict[str, list[str]]:
        return {i["name"]: i["column_names"] for i in insp.get_indexes(table)}

    assert index_cols("events")["ix_events_trace_id"] == ["trace_id"]
    assert index_cols("events")["ix_events_entity_type_entity_id"] == ["entity_type", "entity_id"]
    assert index_cols("llm_calls")["ix_llm_calls_created_at"] == ["created_at"]
    assert index_cols("applications")["ix_applications_user_id_status"] == ["user_id", "status"]
    with engine.connect() as conn:
        hnsw = conn.exec_driver_sql(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_jobs_embedding_hnsw'"
        ).scalar_one()
    assert "hnsw" in hnsw and "vector_cosine_ops" in hnsw
    engine.dispose()


def test_models_match_migrations(migrated_db):
    engine = create_engine(normalize_url(migrated_db))
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()
    assert diff == [], f"models and migrations differ; generate a new revision: {diff}"


def test_downgrade_and_upgrade_round_trip(migrated_db):
    cfg = alembic_config(migrated_db)
    engine = create_engine(normalize_url(migrated_db))
    try:
        command.downgrade(cfg, "base")
        assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    finally:
        command.upgrade(cfg, "head")
    assert set(inspect(engine).get_table_names()) >= SPEC_TABLES
    engine.dispose()
