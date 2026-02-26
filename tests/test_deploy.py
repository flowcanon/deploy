"""Tests for deploy.py — full deploy lifecycle, rollback, dry-run."""

import json

import pytest

from flow_deploy import process
from flow_deploy.deploy import deploy, rollback


COMPOSE_CMD = ["docker", "compose"]

COMPOSE_CONFIG_YAML = """\
services:
  web:
    image: ghcr.io/myorg/myapp:latest
    labels:
      deploy.role: app
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
  worker:
    image: ghcr.io/myorg/myapp:latest
    labels:
      deploy.role: app
      deploy.order: "200"
    healthcheck:
      test: ["CMD", "celery", "inspect", "ping"]
  postgres:
    image: postgres:16
    labels:
      deploy.role: accessory
"""

WEB_CONTAINER_OLD = json.dumps({
    "ID": "old_web_111", "Image": "ghcr.io/myorg/myapp:oldtag",
    "CreatedAt": "2024-01-01 00:00:00", "State": "running",
})
WEB_CONTAINER_NEW = json.dumps({
    "ID": "new_web_222", "Image": "ghcr.io/myorg/myapp:abc123",
    "CreatedAt": "2024-01-02 00:00:00", "State": "running",
})
WORKER_CONTAINER_OLD = json.dumps({
    "ID": "old_wrk_333", "Image": "ghcr.io/myorg/myapp:oldtag",
    "CreatedAt": "2024-01-01 00:00:00", "State": "running",
})
WORKER_CONTAINER_NEW = json.dumps({
    "ID": "new_wrk_444", "Image": "ghcr.io/myorg/myapp:abc123",
    "CreatedAt": "2024-01-02 00:00:00", "State": "running",
})


def _ok(stdout=""):
    return process.Result(0, stdout, "")


def _err(stderr="error"):
    return process.Result(1, "", stderr)


def _setup_happy_path(mock_process, monkeypatch, tmp_path):
    """Set up mock responses for a successful 2-service deploy."""
    monkeypatch.chdir(tmp_path)
    mock_process.responses.extend([
        # compose config
        _ok(COMPOSE_CONFIG_YAML),
        # web: pull
        _ok(),
        # web: scale to 2
        _ok(),
        # web: docker ps (get containers)
        _ok(WEB_CONTAINER_OLD + "\n" + WEB_CONTAINER_NEW + "\n"),
        # web: health check (healthy)
        _ok("healthy\n"),
        # web: docker stop old
        _ok(),
        # web: docker rm old
        _ok(),
        # web: scale back to 1
        _ok(),
        # worker: pull
        _ok(),
        # worker: scale to 2
        _ok(),
        # worker: docker ps
        _ok(WORKER_CONTAINER_OLD + "\n" + WORKER_CONTAINER_NEW + "\n"),
        # worker: health check (healthy)
        _ok("healthy\n"),
        # worker: docker stop old
        _ok(),
        # worker: docker rm old
        _ok(),
        # worker: scale back to 1
        _ok(),
    ])


def test_deploy_happy_path(mock_process, monkeypatch, tmp_path):
    _setup_happy_path(mock_process, monkeypatch, tmp_path)
    result = deploy(tag="abc123", cmd=COMPOSE_CMD)
    assert result == 0
    # Verify tag was written
    tag_file = tmp_path / ".deploy-tag"
    assert tag_file.exists()
    assert "abc123" in tag_file.read_text()


def test_deploy_service_filter(mock_process, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    mock_process.responses.extend([
        _ok(COMPOSE_CONFIG_YAML),
        # web only: pull, scale, ps, health, stop, rm, scale back
        _ok(), _ok(),
        _ok(WEB_CONTAINER_OLD + "\n" + WEB_CONTAINER_NEW + "\n"),
        _ok("healthy\n"),
        _ok(), _ok(), _ok(),
    ])
    result = deploy(tag="abc123", services_filter=["web"], cmd=COMPOSE_CMD)
    assert result == 0


def test_deploy_dry_run(mock_process, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    mock_process.responses.append(_ok(COMPOSE_CONFIG_YAML))
    result = deploy(tag="abc123", dry_run=True, cmd=COMPOSE_CMD)
    assert result == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "web" in out
    assert "worker" in out
    # No lock file should exist
    assert not (tmp_path / ".deploy-lock").exists()


def test_deploy_health_check_failure(mock_process, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    # Patch _wait_for_healthy to avoid real sleep
    monkeypatch.setattr("flow_deploy.deploy._wait_for_healthy", lambda *a, **kw: False)
    mock_process.responses.extend([
        _ok(COMPOSE_CONFIG_YAML),
        # web: pull
        _ok(),
        # web: scale to 2
        _ok(),
        # web: docker ps
        _ok(WEB_CONTAINER_OLD + "\n" + WEB_CONTAINER_NEW + "\n"),
        # web: stop new (rollback)
        _ok(),
        # web: rm new
        _ok(),
        # web: scale back to 1
        _ok(),
    ])
    result = deploy(tag="abc123", cmd=COMPOSE_CMD)
    assert result == 1


def test_deploy_pull_failure(mock_process, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    mock_process.responses.extend([
        _ok(COMPOSE_CONFIG_YAML),
        _err("pull failed"),
    ])
    result = deploy(tag="abc123", cmd=COMPOSE_CMD)
    assert result == 1


def test_deploy_lock_held(mock_process, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    mock_process.responses.append(_ok(COMPOSE_CONFIG_YAML))
    # Pre-acquire lock with current PID
    from flow_deploy import lock
    lock.acquire()
    try:
        result = deploy(tag="abc123", cmd=COMPOSE_CMD)
        assert result == 2
    finally:
        lock.release()


def test_deploy_missing_healthcheck(mock_process, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    config_no_hc = """\
services:
  web:
    image: app:latest
    labels:
      deploy.role: app
"""
    mock_process.responses.append(_ok(config_no_hc))
    result = deploy(tag="abc123", cmd=COMPOSE_CMD)
    assert result == 1


def test_deploy_no_services(mock_process, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    config_empty = "services:\n  redis:\n    image: redis:7\n"
    mock_process.responses.append(_ok(config_empty))
    result = deploy(tag="abc123", cmd=COMPOSE_CMD)
    assert result == 1


def test_deploy_compose_config_failure(mock_process, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    mock_process.responses.append(_err("compose error"))
    result = deploy(tag="abc123", cmd=COMPOSE_CMD)
    assert result == 1


def test_deploy_container_count_mismatch(mock_process, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    single_svc_config = """\
services:
  web:
    image: app:latest
    labels:
      deploy.role: app
    healthcheck:
      test: ["CMD", "true"]
"""
    mock_process.responses.extend([
        _ok(single_svc_config),
        _ok(),  # pull
        _ok(),  # scale to 2
        _ok(WEB_CONTAINER_OLD + "\n"),  # only 1 container returned
        _ok(),  # scale back to 1
    ])
    result = deploy(tag="abc123", cmd=COMPOSE_CMD)
    assert result == 1


def test_deploy_order(mock_process, monkeypatch, tmp_path, capsys):
    """Verify services deploy in order (web before worker due to deploy.order)."""
    _setup_happy_path(mock_process, monkeypatch, tmp_path)
    deploy(tag="abc123", cmd=COMPOSE_CMD)
    out = capsys.readouterr().out
    web_pos = out.index("▸ web")
    worker_pos = out.index("▸ worker")
    assert web_pos < worker_pos


def test_rollback(mock_process, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    # Write tag history
    from flow_deploy import tags
    tags.write_tag("v1")
    tags.write_tag("v2")

    # Setup deploy responses for rollback to v1
    single_svc_config = """\
services:
  web:
    image: app:latest
    labels:
      deploy.role: app
    healthcheck:
      test: ["CMD", "true"]
"""
    mock_process.responses.extend([
        _ok(single_svc_config),
        _ok(), _ok(),
        _ok(WEB_CONTAINER_OLD + "\n" + WEB_CONTAINER_NEW + "\n"),
        _ok("healthy\n"),
        _ok(), _ok(), _ok(),
    ])
    result = rollback(cmd=COMPOSE_CMD)
    assert result == 0


def test_rollback_no_previous(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    result = rollback(cmd=COMPOSE_CMD)
    assert result == 1
