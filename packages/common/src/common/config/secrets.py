"""Resolution of `secret:<name>` references.

Config never holds secret values, only references. Locally a reference resolves from the
env var with the upper-cased name (`secret:database_url` -> `DATABASE_URL`). On AWS it
resolves from Secrets Manager (wired up with the infrastructure in Phase 10).
"""

from __future__ import annotations

import os
import re
from typing import Protocol

SECRET_PREFIX = "secret:"  # noqa: S105  (a reference prefix, not a secret)
_NAME_RE = re.compile(r"^[a-z0-9_]+$")


class SecretNotFoundError(LookupError):
    pass


def is_secret_ref(value: object) -> bool:
    return isinstance(value, str) and value.startswith(SECRET_PREFIX)


def secret_name(ref: str) -> str:
    if not is_secret_ref(ref):
        raise ValueError("not a secret reference")
    name = ref[len(SECRET_PREFIX) :]
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid secret name: {name!r}")
    return name


class SecretResolver(Protocol):
    def resolve(self, name: str) -> str: ...


class EnvSecretResolver:
    """Local dev: `secret:foo_bar` -> env var `FOO_BAR`."""

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        self._environ = environ if environ is not None else os.environ

    def resolve(self, name: str) -> str:
        value = self._environ.get(name.upper())
        if not value:
            raise SecretNotFoundError(f"secret {name!r} is not set (env var {name.upper()})")
        return value


class AwsSecretsManagerResolver:
    """Resolves from AWS Secrets Manager. Implemented with the AWS infrastructure (Phase 10)."""

    def resolve(self, name: str) -> str:
        raise NotImplementedError("Secrets Manager resolution is added in Phase 10")


def resolver_from_env() -> SecretResolver:
    backend = os.environ.get("SECRETS_BACKEND", "env")
    if backend == "env":
        return EnvSecretResolver()
    if backend == "aws":
        return AwsSecretsManagerResolver()
    raise ValueError(f"unknown SECRETS_BACKEND: {backend!r}")
