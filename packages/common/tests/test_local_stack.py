"""Keep the local docker stack in sync with config/local.yaml."""

from __future__ import annotations

import re

from conftest import REPO_ROOT


def test_elasticmq_queues_match_config(config_data):
    conf = (REPO_ROOT / "docker" / "elasticmq" / "elasticmq.conf").read_text()
    queues = config_data["queues"]
    for name in queues["names"].values():
        dlq = f"{name}{queues['dlq_suffix']}"
        pattern = (
            rf"{re.escape(name)} \{{ deadLettersQueue \{{ "
            rf'name = "{re.escape(dlq)}", maxReceiveCount = (\d+) \}} \}}'
        )
        match = re.search(pattern, conf)
        assert match, f"queue {name} with DLQ {dlq} missing from elasticmq.conf"
        assert int(match.group(1)) == queues["max_receive_count"]
        assert f"  {dlq} {{}}" in conf


def test_s3_buckets_match_config(config_data):
    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    init_line = next(line for line in compose.splitlines() if "for b in" in line)
    for uri in (config_data["prompts"]["bucket"], config_data["job_sources"]["registry_file"]):
        bucket = uri.removeprefix("s3://").split("/")[0]
        assert bucket in init_line.replace(";", " ").split(), f"{bucket} is not created by s3-init"
