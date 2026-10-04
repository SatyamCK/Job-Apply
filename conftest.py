"""Shared pytest fixtures for all Python packages and services."""

from __future__ import annotations

import copy
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import create_engine, text

from common.config import ConfigManager, EnvSecretResolver, RawConfig, load_schema
from common.db.session import normalize_url
from common.runtime import Runtime, init_service, set_runtime

REPO_ROOT = Path(__file__).resolve().parent


class MemorySource:
    """Config source backed by a dict; bump it with `publish()`."""

    description = "memory"

    def __init__(self, data: dict[str, Any]) -> None:
        self._counter = 0
        self.fail = False
        self.publish(data)

    def publish(self, data: dict[str, Any] | bytes) -> None:
        self._counter += 1
        self._content = data if isinstance(data, bytes) else yaml.safe_dump(data).encode()

    def fetch(self) -> RawConfig:
        if self.fail:
            raise ConnectionError("source unavailable")
        return RawConfig(version=f"v{self._counter}", content=self._content)


@pytest.fixture(scope="session")
def schema() -> dict[str, Any]:
    return load_schema(REPO_ROOT / "config" / "schema.json")


@pytest.fixture(scope="session")
def _local_config() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((REPO_ROOT / "config" / "local.yaml").read_text())
    return data


@pytest.fixture
def config_data(_local_config: dict[str, Any]) -> dict[str, Any]:
    """A fresh, mutable copy of config/local.yaml for each test."""
    data = copy.deepcopy(_local_config)
    data["observability"]["otlp_endpoint"] = None  # never export spans from tests
    return data


@pytest.fixture
def make_manager(schema: dict[str, Any]) -> Any:
    def _make(
        data: dict[str, Any], environ: dict[str, str] | None = None
    ) -> tuple[ConfigManager, MemorySource]:
        source = MemorySource(data)
        manager = ConfigManager(source, schema, resolver=EnvSecretResolver(environ or {}))
        manager.load()
        return manager, source

    return _make


# --- tracing ----------------------------------------------------------------------------------


_SPAN_EXPORTER = InMemorySpanExporter()


@pytest.fixture(scope="session")
def tracer_provider() -> TracerProvider:
    """One SDK provider for the whole test session (OTel allows setting the global only once)."""
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_SPAN_EXPORTER))
    trace.set_tracer_provider(provider)
    return provider


@pytest.fixture
def span_exporter(tracer_provider: TracerProvider) -> InMemorySpanExporter:
    _SPAN_EXPORTER.clear()
    return _SPAN_EXPORTER


# --- database ---------------------------------------------------------------------------------


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Auto-mark every test that (transitively) needs the database as `db`."""
    for item in items:
        if "db_url" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.db)


def _require_db() -> bool:
    return os.environ.get("REQUIRE_DB") == "1"


@pytest.fixture(scope="session")
def db_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    reason = None
    if not url:
        reason = "TEST_DATABASE_URL is not set"
    else:
        try:
            engine = create_engine(normalize_url(url))
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            engine.dispose()
        except Exception as exc:
            reason = f"test database unreachable: {type(exc).__name__}: {exc}"
    if reason:
        if _require_db():
            pytest.fail(f"{reason} (REQUIRE_DB=1; run `make up`)")
        pytest.skip(reason)
    assert url
    return url


def alembic_config(url: str) -> Any:
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(str(REPO_ROOT / "migrations" / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(scope="session")
def migrated_db(db_url: str) -> str:
    """Test database rebuilt from scratch with every migration applied once per session."""
    from alembic import command

    cfg = alembic_config(db_url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    return db_url


@pytest.fixture
def runtime(
    config_data: dict[str, Any],
    make_manager: Any,
    tracer_provider: TracerProvider,
    migrated_db: str,
) -> Iterator[Runtime]:
    manager, _ = make_manager(config_data)
    rt = init_service(
        "test",
        config_manager=manager,
        database_url=migrated_db,
        tracer_provider=tracer_provider,
        poll_config=False,
    )
    yield rt
    rt.shutdown()
    set_runtime(None)
