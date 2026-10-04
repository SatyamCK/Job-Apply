from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
import yaml

from common.config import (
    AppConfigAgentSource,
    ConfigError,
    EnvSecretResolver,
    FileSource,
    SecretNotFoundError,
    parse_and_validate,
)
from common.config.sources import source_from_env


def dump(data: dict) -> bytes:
    return yaml.safe_dump(data).encode()


# --- validation -------------------------------------------------------------------------------


def test_local_config_is_valid(config_data, schema):
    data = parse_and_validate(dump(config_data), schema)
    assert data["environment"] == "local"
    assert data["llm"]["default_provider"] == "bedrock"


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda d: d.update(unexpected_key=1), "Additional properties"),
        (lambda d: d["matching"].update(vector_top_k="lots"), "matching.vector_top_k"),
        (
            lambda d: d["contact_finder"]["providers"]["hunter"].update(
                api_key="sk_live_plaintext"
            ),
            "api_key",
        ),
        (lambda d: d.update(environment="qa"), "environment"),
        (lambda d: d["email"].update(send_window_recipient_local="9am-5pm"), "send_window"),
        (lambda d: d["llm"]["tasks"].pop("check_claims"), "check_claims"),
    ],
)
def test_schema_rejects_bad_values(config_data, schema, mutate, expected):
    mutate(config_data)
    with pytest.raises(ConfigError) as exc:
        parse_and_validate(dump(config_data), schema)
    assert any(expected in e for e in exc.value.errors), exc.value.errors


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda d: d["llm"].update(default_provider="openai"), "default_provider"),
        (lambda d: d["llm"].update(fallback_provider="bedrock"), "must differ"),
        (lambda d: d["llm"]["tasks"]["parse_job"].update(provider="nope"), "parse_job"),
        (lambda d: d["pricing"].pop("selfhosted"), "pricing"),
        (lambda d: d["matching"].update(rerank_top_k=500), "rerank_top_k"),
        (lambda d: d["email"].update(gap_minutes={"min": 10, "max": 2}), "gap_minutes"),
        (lambda d: d["email"].update(send_window_recipient_local="17:30-09:30"), "send_window"),
        (lambda d: d["email"].update(approval_mode="auto_above_score"), "allow_auto_send"),
        (lambda d: d["email"].update(default_recipient_timezone="Mars/Olympus"), "timezone"),
    ],
)
def test_semantic_checks(config_data, schema, mutate, expected):
    mutate(config_data)
    with pytest.raises(ConfigError) as exc:
        parse_and_validate(dump(config_data), schema)
    assert any(expected in e for e in exc.value.errors), exc.value.errors


def test_yaml_garbage_is_rejected(schema):
    with pytest.raises(ConfigError):
        parse_and_validate(b"llm: [unclosed", schema)
    with pytest.raises(ConfigError):
        parse_and_validate(b"- just\n- a list\n", schema)


def test_dotted_get(config_data, make_manager):
    manager, _ = make_manager(config_data)
    assert manager.current.get("llm.tasks.draft_email.temperature") == 0.4
    with pytest.raises(KeyError):
        manager.current.get("llm.nope")


# --- hot reload -------------------------------------------------------------------------------


def test_refresh_applies_new_version_and_notifies(config_data, make_manager):
    manager, source = make_manager(config_data)
    seen = []
    manager.on_change(lambda old, new: seen.append((old.version, new.version)))

    config_data["llm"]["providers"]["selfhosted"]["enabled"] = True
    config_data["llm"]["default_provider"] = "selfhosted"
    config_data["llm"]["fallback_provider"] = "bedrock"
    source.publish(config_data)

    assert manager.refresh() is True
    assert manager.current.get("llm.default_provider") == "selfhosted"
    assert seen == [("v1", "v2")]


def test_refresh_same_version_is_noop(config_data, make_manager):
    manager, _ = make_manager(config_data)
    assert manager.refresh() is False


def test_invalid_new_version_keeps_last_good(config_data, make_manager):
    manager, source = make_manager(config_data)
    good_version = manager.current.version
    config_data["llm"]["default_provider"] = "does-not-exist"
    source.publish(config_data)

    assert manager.refresh() is False
    assert manager.current.version == good_version
    assert manager.current.get("llm.default_provider") == "bedrock"
    # A broken version is only reported once, not on every poll.
    assert manager.refresh() is False


def test_source_failure_keeps_current(config_data, make_manager):
    manager, source = make_manager(config_data)
    source.fail = True
    assert manager.refresh() is False
    assert manager.current.version == "v1"


def test_listener_failure_does_not_block_reload(config_data, make_manager):
    manager, source = make_manager(config_data)

    def boom(old, new):
        raise RuntimeError("listener bug")

    manager.on_change(boom)
    config_data["matching"]["min_fit_score"] = 80
    source.publish(config_data)
    assert manager.refresh() is True
    assert manager.current.get("matching.min_fit_score") == 80


def test_polling_thread_picks_up_changes(config_data, make_manager):
    config_data["runtime"]["config_poll_seconds"] = 0.02
    manager, source = make_manager(config_data)
    manager.start_polling()
    try:
        config_data["matching"]["min_fit_score"] = 55
        source.publish(config_data)
        deadline = time.monotonic() + 2
        while manager.current.get("matching.min_fit_score") != 55 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert manager.current.get("matching.min_fit_score") == 55
    finally:
        manager.stop_polling()


# --- secrets ----------------------------------------------------------------------------------


def test_secret_resolution_is_lazy(config_data, make_manager):
    # Loading succeeds although no secret env vars are set.
    manager, _ = make_manager(config_data, environ={"DATABASE_URL": "postgresql://x"})
    assert manager.current.secret("database.url") == "postgresql://x"
    with pytest.raises(SecretNotFoundError):
        manager.current.secret("contact_finder.providers.hunter.api_key")
    with pytest.raises(ValueError, match="not a secret reference"):
        manager.current.secret("embeddings.model")


def test_env_secret_resolver_rejects_empty():
    with pytest.raises(SecretNotFoundError):
        EnvSecretResolver({"HUNTER_API_KEY": ""}).resolve("hunter_api_key")


# --- sources ----------------------------------------------------------------------------------


def test_file_source_version_tracks_content(tmp_path: Path):
    path = tmp_path / "c.yaml"
    path.write_text("a: 1\n")
    source = FileSource(path)
    v1 = source.fetch().version
    assert source.fetch().version == v1
    path.write_text("a: 2\n")
    assert source.fetch().version != v1


def test_appconfig_agent_source():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"a: 1\n", headers={"Configuration-Version": "7"})

    source = AppConfigAgentSource(
        "jobapplier",
        "prod",
        "main",
        agent_url="http://agent:2772",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    raw = source.fetch()
    assert raw.version == "7"
    assert raw.content == b"a: 1\n"
    assert (
        str(requests[0].url)
        == "http://agent:2772/applications/jobapplier/environments/prod/configurations/main"
    )


def test_source_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_SOURCE", "file")
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "local.yaml"))
    monkeypatch.delenv("CONFIG_SCHEMA_PATH", raising=False)
    source, schema_path = source_from_env()
    assert isinstance(source, FileSource)
    assert schema_path == tmp_path / "schema.json"

    monkeypatch.setenv("CONFIG_SOURCE", "appconfig")
    with pytest.raises(RuntimeError, match="CONFIG_SCHEMA_PATH"):
        source_from_env()
