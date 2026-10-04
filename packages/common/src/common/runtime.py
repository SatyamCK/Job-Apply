"""Service bootstrap: config, logging, tracing, metrics and DB wired together once per process.

Bootstrap env vars (deployment wiring, not runtime settings):
    SERVICE_VERSION   build/git version string (default "dev")
    CONFIG_SOURCE, CONFIG_PATH, CONFIG_SCHEMA_PATH, APPCONFIG_*   see common.config.sources
    SECRETS_BACKEND   env | aws
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from opentelemetry.sdk.trace import TracerProvider
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from common.config import Config, ConfigManager, load_schema
from common.config.sources import source_from_env
from common.db.models import EMBEDDING_DIM
from common.db.session import make_engine, make_session_factory
from common.logging import configure_logging, get_logger, set_log_level
from common.metrics import MetricsEmitter
from common.tracing import configure_tracing

log = get_logger("common.runtime")


@dataclass
class Runtime:
    service: str
    version: str
    config_manager: ConfigManager
    metrics: MetricsEmitter
    tracer_provider: TracerProvider
    engine: Engine | None = None
    session_factory: sessionmaker[Session] | None = field(default=None)
    owns_tracer_provider: bool = True

    @property
    def config(self) -> Config:
        return self.config_manager.current

    def require_sessions(self) -> sessionmaker[Session]:
        if self.session_factory is None:
            raise RuntimeError(f"service {self.service!r} was started without a database")
        return self.session_factory

    def shutdown(self) -> None:
        self.config_manager.stop_polling()
        self.tracer_provider.force_flush()
        if self.owns_tracer_provider:
            self.tracer_provider.shutdown()
        if self.engine is not None:
            self.engine.dispose()


_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    if _runtime is None:
        raise RuntimeError("runtime not initialised; call init_service() first")
    return _runtime


def set_runtime(runtime: Runtime | None) -> None:
    global _runtime
    _runtime = runtime


def init_service(
    service: str,
    *,
    config_manager: ConfigManager | None = None,
    with_db: bool = True,
    database_url: str | None = None,
    tracer_provider: TracerProvider | None = None,
    poll_config: bool = True,
) -> Runtime:
    """Wire up config, logging, tracing, metrics and DB for this process.

    Pass `tracer_provider` to reuse an already-installed provider (OpenTelemetry allows the
    global provider to be set only once per process, which matters in tests).
    """
    version = os.environ.get("SERVICE_VERSION", "dev")

    if config_manager is None:
        source, schema_path = source_from_env()
        config_manager = ConfigManager(source, load_schema(schema_path))
        config_manager.load()
    cfg = config_manager.current
    obs = cfg["observability"]

    configure_logging(service=service, env=cfg.environment, version=version, level=obs["log_level"])

    if cfg.get("embeddings.dimensions") != EMBEDDING_DIM:
        raise RuntimeError(
            f"config embeddings.dimensions={cfg.get('embeddings.dimensions')} does not match the "
            f"DB vector column size {EMBEDDING_DIM}; a migration is required to change it"
        )

    owns_tracer_provider = tracer_provider is None
    if tracer_provider is None:
        tracer_provider = configure_tracing(
            service=service,
            env=cfg.environment,
            version=version,
            sample_rate=float(obs["trace_sample_rate"]),
            otlp_endpoint=obs["otlp_endpoint"],
        )
    metrics = MetricsEmitter(
        namespace=lambda: str(config_manager.current.get("observability.metrics_namespace")),
        env=cfg.environment,
        service=service,
    )

    engine = None
    session_factory = None
    if with_db:
        url = database_url or cfg.secret("database.url")
        engine = make_engine(url, pool_size=int(cfg.get("database.pool_size")))
        session_factory = make_session_factory(engine)

    def _on_config_change(old: Config, new: Config) -> None:
        if old.get("observability.log_level") != new.get("observability.log_level"):
            set_log_level(new.get("observability.log_level"))

    config_manager.on_change(_on_config_change)
    if poll_config:
        config_manager.start_polling()

    runtime = Runtime(
        service=service,
        version=version,
        config_manager=config_manager,
        metrics=metrics,
        tracer_provider=tracer_provider,
        engine=engine,
        session_factory=session_factory,
        owns_tracer_provider=owns_tracer_provider,
    )
    set_runtime(runtime)
    log.info("service_started", config_version=cfg.version)
    return runtime
