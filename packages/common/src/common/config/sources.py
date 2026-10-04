"""Where raw config bytes come from: a local YAML file, or the AWS AppConfig Agent sidecar."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

# The AppConfig Agent listens on localhost:2772 by default (AWS documented default).
DEFAULT_APPCONFIG_AGENT_URL = "http://localhost:2772"


@dataclass(frozen=True)
class RawConfig:
    version: str
    content: bytes


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()[:16]


class ConfigSource(Protocol):
    description: str

    def fetch(self) -> RawConfig: ...


class FileSource:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.description = f"file:{self.path}"

    def fetch(self) -> RawConfig:
        content = self.path.read_bytes()
        return RawConfig(version=_digest(content), content=content)


class AppConfigAgentSource:
    def __init__(
        self,
        application: str,
        environment: str,
        profile: str,
        agent_url: str = DEFAULT_APPCONFIG_AGENT_URL,
        client: httpx.Client | None = None,
    ) -> None:
        self.url = (
            f"{agent_url.rstrip('/')}/applications/{application}"
            f"/environments/{environment}/configurations/{profile}"
        )
        self.description = f"appconfig:{application}/{environment}/{profile}"
        self._client = client or httpx.Client(timeout=5.0)

    def fetch(self) -> RawConfig:
        resp = self._client.get(self.url)
        resp.raise_for_status()
        content = resp.content
        version = resp.headers.get("Configuration-Version") or _digest(content)
        return RawConfig(version=version, content=content)


def source_from_env() -> tuple[ConfigSource, Path]:
    """Build the config source and locate the JSON Schema from bootstrap env vars.

    CONFIG_SOURCE=file      -> CONFIG_PATH (required); schema defaults to <its dir>/schema.json
    CONFIG_SOURCE=appconfig -> APPCONFIG_APPLICATION/_ENVIRONMENT/_PROFILE, CONFIG_SCHEMA_PATH
    """
    kind = os.environ.get("CONFIG_SOURCE", "file")
    schema_override = os.environ.get("CONFIG_SCHEMA_PATH")
    if kind == "file":
        path = os.environ.get("CONFIG_PATH")
        if not path:
            raise RuntimeError("CONFIG_PATH must be set when CONFIG_SOURCE=file")
        schema = Path(schema_override) if schema_override else Path(path).parent / "schema.json"
        return FileSource(path), schema
    if kind == "appconfig":
        if not schema_override:
            raise RuntimeError("CONFIG_SCHEMA_PATH must be set when CONFIG_SOURCE=appconfig")
        source = AppConfigAgentSource(
            application=os.environ["APPCONFIG_APPLICATION"],
            environment=os.environ["APPCONFIG_ENVIRONMENT"],
            profile=os.environ["APPCONFIG_PROFILE"],
            agent_url=os.environ.get("APPCONFIG_AGENT_URL", DEFAULT_APPCONFIG_AGENT_URL),
        )
        return source, Path(schema_override)
    raise RuntimeError(f"unknown CONFIG_SOURCE: {kind!r}")
