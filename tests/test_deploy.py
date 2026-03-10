"""Tests for deploy.py — full deploy lifecycle, dry-run."""

import json

from flow_deploy import process
from flow_deploy.deploy import deploy

COMPOSE_CMD = ["docker", "compose"]
PREV_SHA = "prev123abc"

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

WEB_CONTAINER_OLD = json.dumps(
    {
        "ID": "old_web_111",
        "Image": "ghcr.io/myorg/myapp:oldtag",
        "CreatedAt": "2024-01-01 00:00:00",
        "State": "running",
    }
)
WEB_CONTAINER_NEW = json.dumps(
    {
        "ID": "new_web_222",
        "Image": "ghcr.io/myorg/myapp:abc123",
        "CreatedAt": "2024-01-02 00:00:00",
        "State": "running",
    }
)
WORKER_CONTAINER_OLD = json.dumps(
    {
        "ID": "old_wrk_333",
        "Image": "ghcr.io/myorg/myapp:oldtag",
        "CreatedAt": "2024-01-01 00:00:00",
        "State": "running",
    }
)
WORKER_CONTAINER_NEW = json.dumps(
    {
        "ID": "new_wrk_444",
        "Image": "ghcr.io/myorg/myapp:abc123",
        "CreatedAt": "2024-01-02 00:00:00",
        "State": "running",
    }
)


def _ok(stdout=""):
    return process.Result(0, stdout, "")


def _err(stderr="error"):
    return process.Result(1, "", stderr)


def _git_preflight():
    """Return the 4 mock responses for a clean git preflight."""
    return [
        _ok(""),  # git status --porcelain (clean)
        _ok(),  # git fetch origin
        _ok(PREV_SHA + "\n"),  # git rev-parse HEAD
        _ok(),  # git checkout --detach <sha>
    ]


def _chdir(monkeypatch, tmp_path):
    """Set working directory and ensure .git/ exists for lock file."""
    (tmp_path / ".git").mkdir(exist_ok=True)
    monkeypatch.chdir(tmp_path)


def _setup_happy_path(mock_process, monkeypatch, tmp_path):
    """Set up mock responses for a successful 2-service deploy."""
    _chdir(monkeypatch, tmp_path)
    mock_process.responses.extend(
        [
            # git preflight (before compose config)
            *_git_preflight(),
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
        ]
    )


def test_deploy_happy_path(mock_process, monkeypatch, tmp_path):
    _setup_happy_path(mock_process, monkeypatch, tmp_path)
    result = deploy(tag="abc123", cmd=COMPOSE_CMD)
    assert result == 0


def test_deploy_service_filter(mock_process, monkeypatch, tmp_path):
    _chdir(monkeypatch, tmp_path)
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(COMPOSE_CONFIG_YAML),
            # web only: pull, scale, ps, health, stop, rm, scale back
            _ok(),
            _ok(),
            _ok(WEB_CONTAINER_OLD + "\n" + WEB_CONTAINER_NEW + "\n"),
            _ok("healthy\n"),
            _ok(),
            _ok(),
            _ok(),
        ]
    )
    result = deploy(tag="abc123", services_filter=["web"], cmd=COMPOSE_CMD)
    assert result == 0


def test_deploy_dry_run(mock_process, monkeypatch, tmp_path, capsys):
    _chdir(monkeypatch, tmp_path)
    mock_process.responses.append(_ok(COMPOSE_CONFIG_YAML))
    result = deploy(tag="abc123", dry_run=True, cmd=COMPOSE_CMD)
    assert result == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "web" in out
    assert "worker" in out
    # No lock file should exist
    assert not (tmp_path / ".git" / "deploy-lock").exists()


def test_deploy_health_check_failure(mock_process, monkeypatch, tmp_path):
    _chdir(monkeypatch, tmp_path)
    # Patch _wait_for_healthy to avoid real sleep
    monkeypatch.setattr("flow_deploy.deploy._wait_for_healthy", lambda *a, **kw: False)
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(COMPOSE_CONFIG_YAML),
            # web: pull
            _ok(),
            # web: scale to 2
            _ok(),
            # web: docker ps
            _ok(WEB_CONTAINER_OLD + "\n" + WEB_CONTAINER_NEW + "\n"),
            # web: stop new (abort)
            _ok(),
            # web: rm new
            _ok(),
            # web: scale back to 1
            _ok(),
            # git restore to previous SHA
            _ok(),
        ]
    )
    result = deploy(tag="abc123", cmd=COMPOSE_CMD)
    assert result == 1


def test_deploy_pull_failure(mock_process, monkeypatch, tmp_path):
    _chdir(monkeypatch, tmp_path)
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(COMPOSE_CONFIG_YAML),
            _err("pull failed"),
            # git restore to previous SHA
            _ok(),
        ]
    )
    result = deploy(tag="abc123", cmd=COMPOSE_CMD)
    assert result == 1


def test_deploy_lock_held(mock_process, monkeypatch, tmp_path):
    _chdir(monkeypatch, tmp_path)
    # No git or compose responses needed — lock check happens first
    # Pre-acquire lock with current PID
    from flow_deploy import lock

    lock.acquire()
    try:
        result = deploy(tag="abc123", cmd=COMPOSE_CMD)
        assert result == 2
    finally:
        lock.release()


def test_deploy_missing_healthcheck(mock_process, monkeypatch, tmp_path):
    _chdir(monkeypatch, tmp_path)
    config_no_hc = """\
services:
  web:
    image: app:latest
    labels:
      deploy.role: app
"""
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(config_no_hc),
            _ok(),  # git restore
        ]
    )
    result = deploy(tag="abc123", cmd=COMPOSE_CMD)
    assert result == 1


def test_deploy_no_services(mock_process, monkeypatch, tmp_path):
    _chdir(monkeypatch, tmp_path)
    config_empty = "services:\n  redis:\n    image: redis:7\n"
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(config_empty),
            _ok(),  # git restore
        ]
    )
    result = deploy(tag="abc123", cmd=COMPOSE_CMD)
    assert result == 1


def test_deploy_compose_config_failure(mock_process, monkeypatch, tmp_path):
    _chdir(monkeypatch, tmp_path)
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _err("compose error"),
            _ok(),  # git restore
        ]
    )
    result = deploy(tag="abc123", cmd=COMPOSE_CMD)
    assert result == 1


def test_deploy_container_count_mismatch(mock_process, monkeypatch, tmp_path):
    _chdir(monkeypatch, tmp_path)
    single_svc_config = """\
services:
  web:
    image: app:latest
    labels:
      deploy.role: app
    healthcheck:
      test: ["CMD", "true"]
"""
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(single_svc_config),
            _ok(),  # pull
            _ok(),  # scale to 2
            _ok(WEB_CONTAINER_OLD + "\n"),  # only 1 container returned
            _ok(),  # scale back to 1
            _ok(),  # git restore
        ]
    )
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


def test_deploy_restore_failure_retains_lock(mock_process, monkeypatch, tmp_path, capsys):
    """If git restore fails after a deploy failure, the lock should be retained."""
    _chdir(monkeypatch, tmp_path)
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(COMPOSE_CONFIG_YAML),
            # web: pull fails
            _err("pull failed"),
            # git restore fails
            _err("checkout error"),
        ]
    )
    result = deploy(tag="abc123", cmd=COMPOSE_CMD)
    assert result == 1
    # Lock should still be held
    from flow_deploy import lock

    assert not lock.acquire(), "Lock should still be held after restore failure"
    err = capsys.readouterr().err
    assert "Lock retained" in err
    # Clean up for other tests
    lock.release()


def test_deploy_dirty_tree_fails(mock_process, monkeypatch, tmp_path, capsys):
    _chdir(monkeypatch, tmp_path)
    mock_process.responses.extend(
        [
            # git status --porcelain returns dirty (before compose config)
            _ok(" M somefile.py\n"),
        ]
    )
    result = deploy(tag="abc123", cmd=COMPOSE_CMD)
    assert result == 1
    err = capsys.readouterr().err
    assert "dirty" in err
