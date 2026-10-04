"""Config loading, validation and hot reload.

A new version is validated (JSON Schema + cross-field checks) before it replaces the current
one. An invalid version is rejected and the last good config stays live.
"""

from __future__ import annotations

import json
import threading
import zoneinfo
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
import yaml
from jsonschema import Draft202012Validator

from common.config.secrets import SecretResolver, is_secret_ref, resolver_from_env, secret_name
from common.config.sources import ConfigSource

log = structlog.get_logger("common.config")

ChangeListener = Callable[["Config", "Config"], None]


class ConfigError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("invalid config:\n  - " + "\n  - ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class Config:
    data: Mapping[str, Any]
    version: str
    resolver: SecretResolver

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, path: str) -> Any:
        """Dotted lookup: `cfg.get("llm.default_provider")`. Raises KeyError if missing."""
        node: Any = self.data
        for part in path.split("."):
            if not isinstance(node, Mapping) or part not in node:
                raise KeyError(path)
            node = node[part]
        return node

    def secret(self, path: str) -> str:
        """Resolve the `secret:<name>` reference at a dotted path. Resolution is lazy."""
        ref = self.get(path)
        if not is_secret_ref(ref):
            raise ValueError(f"config value at {path!r} is not a secret reference")
        return self.resolver.resolve(secret_name(ref))

    @property
    def environment(self) -> str:
        return str(self.data["environment"])


def load_schema(path: str | Path) -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(Path(path).read_text())
    Draft202012Validator.check_schema(schema)
    return schema


def _semantic_errors(data: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    llm = data["llm"]
    providers = llm["providers"]
    if llm["default_provider"] not in providers:
        errors.append(
            f"llm.default_provider {llm['default_provider']!r} is not a configured provider"
        )
    fallback = llm["fallback_provider"]
    if fallback is not None:
        if fallback not in providers:
            errors.append(f"llm.fallback_provider {fallback!r} is not a configured provider")
        elif fallback == llm["default_provider"]:
            errors.append("llm.fallback_provider must differ from llm.default_provider")
    for task, spec in llm["tasks"].items():
        if spec["provider"] != "default" and spec["provider"] not in providers:
            errors.append(
                f"llm.tasks.{task}.provider {spec['provider']!r} is not a configured provider"
            )
    for name in providers:
        if name not in data["pricing"]:
            errors.append(f"pricing has no entry for llm provider {name!r}")

    matching = data["matching"]
    if matching["rerank_top_k"] > matching["vector_top_k"]:
        errors.append("matching.rerank_top_k must be <= matching.vector_top_k")

    email = data["email"]
    if email["gap_minutes"]["min"] > email["gap_minutes"]["max"]:
        errors.append("email.gap_minutes.min must be <= email.gap_minutes.max")
    start, end = email["send_window_recipient_local"].split("-")
    if start >= end:
        errors.append("email.send_window_recipient_local start must be before end")
    if email["approval_mode"] == "auto_above_score" and not email["allow_auto_send"]:
        errors.append("email.approval_mode auto_above_score requires email.allow_auto_send")
    try:
        zoneinfo.ZoneInfo(email["default_recipient_timezone"])
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        errors.append(
            f"email.default_recipient_timezone {email['default_recipient_timezone']!r} "
            "is not a valid IANA zone"
        )
    return errors


def parse_and_validate(content: bytes, schema: Mapping[str, Any]) -> dict[str, Any]:
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ConfigError([f"YAML parse error: {exc}"]) from exc
    if not isinstance(data, dict):
        raise ConfigError(["config root must be a mapping"])

    validator = Draft202012Validator(schema)
    schema_errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    if schema_errors:
        raise ConfigError(
            [
                f"{'.'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
                for e in schema_errors
            ]
        )
    semantic = _semantic_errors(data)
    if semantic:
        raise ConfigError(semantic)
    return data


class ConfigManager:
    """Holds the live config; swaps it atomically when the source publishes a valid version."""

    def __init__(
        self,
        source: ConfigSource,
        schema: Mapping[str, Any],
        resolver: SecretResolver | None = None,
    ) -> None:
        self._source = source
        self._schema = schema
        self._resolver = resolver or resolver_from_env()
        self._current: Config | None = None
        self._last_seen_version: str | None = None
        self._listeners: list[ChangeListener] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def current(self) -> Config:
        if self._current is None:
            raise RuntimeError("config not loaded; call load() first")
        return self._current

    def on_change(self, listener: ChangeListener) -> None:
        self._listeners.append(listener)

    def load(self) -> Config:
        """Initial load. Raises ConfigError if the config is invalid."""
        raw = self._source.fetch()
        data = parse_and_validate(raw.content, self._schema)
        with self._lock:
            self._current = Config(data=data, version=raw.version, resolver=self._resolver)
            self._last_seen_version = raw.version
        log.info("config_loaded", source=self._source.description, config_version=raw.version)
        return self._current

    def refresh(self) -> bool:
        """Check the source once. Returns True if a new valid version was applied."""
        try:
            raw = self._source.fetch()
        except Exception as exc:
            log.warning(
                "config_fetch_failed",
                source=self._source.description,
                error_type=type(exc).__name__,
            )
            return False
        if raw.version == self._last_seen_version:
            return False
        self._last_seen_version = raw.version
        try:
            data = parse_and_validate(raw.content, self._schema)
        except ConfigError as exc:
            log.error(
                "config_rejected",
                source=self._source.description,
                config_version=raw.version,
                error_type="ConfigError",
                errors=exc.errors,
            )
            return False
        with self._lock:
            old = self.current
            new = Config(data=data, version=raw.version, resolver=self._resolver)
            self._current = new
        log.info("config_reloaded", source=self._source.description, config_version=raw.version)
        for listener in list(self._listeners):
            try:
                listener(old, new)
            except Exception:
                log.exception("config_listener_failed")
        return True

    def start_polling(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, name="config-poller", daemon=True)
        self._thread.start()

    def stop_polling(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _poll_loop(self) -> None:
        # The interval itself comes from config, so it hot-reloads too.
        while not self._stop.wait(float(self.current.get("runtime.config_poll_seconds"))):
            self.refresh()
