"""Tests for compose.py — command resolution + execution."""

import os

from flow_deploy import process
from flow_deploy.compose import compose_config, compose_run, resolve_command


def test_resolve_command_env(monkeypatch):
    monkeypatch.setenv("COMPOSE_COMMAND", "custom/cmd --flag")
    assert resolve_command() == ["custom/cmd", "--flag"]


def test_resolve_command_script_prod(monkeypatch, tmp_path):
    monkeypatch.delenv("COMPOSE_COMMAND", raising=False)
    script_dir = tmp_path / "script"
    script_dir.mkdir()
    prod = script_dir / "prod"
    prod.write_text("#!/bin/bash\n")
    prod.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    assert resolve_command() == ["script/prod"]


def test_resolve_command_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("COMPOSE_COMMAND", raising=False)
    monkeypatch.chdir(tmp_path)
    assert resolve_command() == ["docker", "compose"]


def test_compose_run(mock_process):
    mock_process.responses.append(process.Result(0, "ok\n", ""))
    result = compose_run(["pull", "web"], cmd=["docker", "compose"])
    assert mock_process.calls[0][1] == ["docker", "compose", "pull", "web"]
    assert result.stdout == "ok\n"


def test_compose_run_with_env(mock_process):
    mock_process.responses.append(process.Result(0, "", ""))
    compose_run(["pull", "web"], env={"DEPLOY_TAG": "abc123"}, cmd=["docker", "compose"])
    _, args, env, _ = mock_process.calls[0]
    assert env == {"DEPLOY_TAG": "abc123"}


def test_compose_config_parses_yaml(mock_process):
    yaml_output = "services:\n  web:\n    image: myapp:latest\n"
    mock_process.responses.append(process.Result(0, yaml_output, ""))
    config = compose_config(cmd=["docker", "compose"])
    assert config["services"]["web"]["image"] == "myapp:latest"


def test_compose_config_raises_on_failure(mock_process):
    mock_process.responses.append(process.Result(1, "", "error"))
    try:
        compose_config(cmd=["docker", "compose"])
        assert False, "Should have raised"
    except RuntimeError as e:
        assert "compose config failed" in str(e)
