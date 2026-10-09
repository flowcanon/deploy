"""Tests for deploy.strategy=drain-first: drain before rolling, start after."""

import json

from flow_deploy import config
from flow_deploy.deploy import _image_tag, deploy
from tests.test_deploy import (
    COMPOSE_CMD,
    WEB_CONTAINER_NEW,
    WEB_CONTAINER_OLD,
    _chdir,
    _err,
    _git_preflight,
    _ok,
)

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
      deploy.strategy: drain-first
      deploy.drain: "3660"
    healthcheck:
      test: ["CMD", "celery", "inspect", "ping"]
"""

WORKER_OLD = json.dumps({"ID": "old_wrk_333", "Image": "ghcr.io/myorg/myapp:v1.2.0"})
WORKER_NEW = json.dumps({"ID": "new_wrk_444", "Image": "ghcr.io/myorg/myapp:abc123"})

WEB_ROLL = [
    _ok(),  # web: pull
    _ok(),  # web: scale to 2
    _ok(WEB_CONTAINER_OLD + "\n" + WEB_CONTAINER_NEW + "\n"),  # web: docker ps
    _ok("healthy\n"),  # web: health
    _ok(),  # web: docker stop old
    _ok(),  # web: docker rm old
    _ok(),  # web: scale back to 1
]
WORKER_DRAIN = [
    _ok(),  # worker: pull
    _ok(WORKER_OLD + "\n"),  # worker: docker ps
    _ok(),  # worker: docker stop old
    _ok(),  # worker: docker rm old
]


def _commands(mock_process):
    return [" ".join(args) for _, args, _, _ in mock_process.calls]


def _index(commands, *needles):
    return next(i for i, c in enumerate(commands) if all(n in c for n in needles))


def test_drain_first_runs_drain_then_rolling_then_start(mock_process, monkeypatch, tmp_path):
    _chdir(monkeypatch, tmp_path)
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(COMPOSE_CONFIG_YAML),
            *WORKER_DRAIN,
            *WEB_ROLL,
            _ok(),  # worker: compose up
            _ok(WORKER_NEW + "\n"),  # worker: docker ps
            _ok("healthy\n"),  # worker: health
        ]
    )
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == 0

    cmds = _commands(mock_process)
    drain = _index(cmds, "docker stop", "--time 3660", "old_wrk_333")
    roll = _index(cmds, "--scale web=2")
    start = _index(cmds, "up -d --no-deps worker")
    assert drain < roll < start
    # The worker never scales to 2: no overlap of old and new.
    assert not any("--scale worker=2" in c for c in cmds)


def test_drain_first_service_not_running_is_started_after(mock_process, monkeypatch, tmp_path):
    _chdir(monkeypatch, tmp_path)
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(COMPOSE_CONFIG_YAML),
            _ok(),  # worker: pull
            _ok(""),  # worker: docker ps (none running)
            *WEB_ROLL,
            _ok(),  # worker: compose up
            _ok(WORKER_NEW + "\n"),  # worker: docker ps
            _ok("healthy\n"),  # worker: health
        ]
    )
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == 0


def test_rolling_failure_restores_drained_service_on_its_old_tag(
    mock_process, monkeypatch, tmp_path
):
    _chdir(monkeypatch, tmp_path)
    monkeypatch.setattr("flow_deploy.deploy._wait_for_healthy", lambda *a, **kw: False)
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(COMPOSE_CONFIG_YAML),
            *WORKER_DRAIN,
            _ok(),  # web: pull
            _ok(),  # web: scale to 2
            _ok(WEB_CONTAINER_OLD + "\n" + WEB_CONTAINER_NEW + "\n"),  # web: ps
            _ok(),  # web: stop new (abort)
            _ok(),  # web: rm new
            _ok(),  # web: scale back to 1
            _ok(),  # git restore
            _ok(),  # worker: compose up on the old tag
        ]
    )
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == 1

    restore = mock_process.calls[-1]
    assert "up -d --no-deps worker" in " ".join(restore[1])
    assert restore[2] == {"DEPLOY_TAG": "v1.2.0"}


def test_drain_pull_failure_stops_nothing(mock_process, monkeypatch, tmp_path):
    _chdir(monkeypatch, tmp_path)
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(COMPOSE_CONFIG_YAML),
            _err("manifest unknown"),  # worker: pull
            _ok(),  # git restore
        ]
    )
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == 1
    assert not any("docker stop" in c for c in _commands(mock_process))


def test_second_drain_failure_restores_the_first(mock_process, monkeypatch, tmp_path):
    _chdir(monkeypatch, tmp_path)
    two_drainers = COMPOSE_CONFIG_YAML.replace("  web:", "  scheduler:", 1).replace(
        '      deploy.role: app\n    healthcheck:\n      test: ["CMD", "curl"',
        "      deploy.role: app\n      deploy.strategy: drain-first\n"
        '      deploy.order: "300"\n    healthcheck:\n      test: ["CMD", "curl"',
        1,
    )
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(two_drainers),
            *WORKER_DRAIN,  # worker drains first (order 100)
            _err("pull failed"),  # scheduler: pull
            _ok(),  # git restore
            _err("no such image"),  # worker: restore fails, logged
        ]
    )
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == 1
    assert mock_process.calls[-1][2] == {"DEPLOY_TAG": "v1.2.0"}


def test_drained_service_that_was_not_running_stays_stopped_on_failure(
    mock_process, monkeypatch, tmp_path
):
    _chdir(monkeypatch, tmp_path)
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(COMPOSE_CONFIG_YAML),
            _ok(),  # worker: pull
            _ok(""),  # worker: docker ps (none running)
            _err("pull failed"),  # web: pull
            _ok(),  # git restore
        ]
    )
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == 1
    assert not any("up -d --no-deps worker" in c for c in _commands(mock_process))


def test_start_failure_leaves_service_down(mock_process, monkeypatch, tmp_path, capsys):
    _chdir(monkeypatch, tmp_path)
    # Web comes up healthy; the new worker never does.
    monkeypatch.setattr(
        "flow_deploy.deploy._wait_for_healthy", lambda ctr_id, *a: ctr_id != "new_wrk_444"
    )
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(COMPOSE_CONFIG_YAML),
            *WORKER_DRAIN,
            _ok(),  # web: pull
            _ok(),  # web: scale to 2
            _ok(WEB_CONTAINER_OLD + "\n" + WEB_CONTAINER_NEW + "\n"),  # web: ps
            _ok(),  # web: docker stop old
            _ok(),  # web: docker rm old
            _ok(),  # web: scale back to 1
            _ok(),  # worker: compose up
            _ok(WORKER_NEW + "\n"),  # worker: docker ps
            _ok(),  # git restore
        ]
    )
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == 1
    out = capsys.readouterr().out
    assert "worker is down" in out
    # No rollback to the old tag once the rolling services shipped.
    assert not any("v1.2.0" in str(call[2]) for call in mock_process.calls)


def test_start_compose_failure(mock_process, monkeypatch, tmp_path):
    _chdir(monkeypatch, tmp_path)
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(COMPOSE_CONFIG_YAML),
            *WORKER_DRAIN,
            *WEB_ROLL,
            _err("port in use"),  # worker: compose up
            _ok(""),  # worker: docker ps
            _ok(),  # git restore
        ]
    )
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == 1


def test_start_healthcheck_skip(mock_process, monkeypatch, tmp_path):
    _chdir(monkeypatch, tmp_path)
    skip = COMPOSE_CONFIG_YAML.replace(
        "deploy.strategy: drain-first",
        'deploy.strategy: drain-first\n      deploy.healthcheck.skip: "true"',
    )
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(skip),
            *WORKER_DRAIN,
            *WEB_ROLL,
            _ok(),  # worker: compose up
            _ok(WORKER_NEW + "\n"),  # worker: docker ps
        ]
    )
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == 0


def test_unknown_strategy_is_rejected(mock_process, monkeypatch, tmp_path, capsys):
    _chdir(monkeypatch, tmp_path)
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(COMPOSE_CONFIG_YAML.replace("drain-first", "blue-green")),
            _ok(),  # git restore
        ]
    )
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == 1
    assert "Unknown deploy.strategy: worker=blue-green" in capsys.readouterr().err


def test_dry_run_shows_the_three_phases(mock_process, monkeypatch, tmp_path, capsys):
    _chdir(monkeypatch, tmp_path)
    mock_process.responses.append(_ok(COMPOSE_CONFIG_YAML))
    assert deploy(tag="abc123", dry_run=True, cmd=COMPOSE_CMD) == 0
    out = capsys.readouterr().out
    first = out.index("would drain running containers first (3660s timeout)")
    rolling = out.index("would scale to 2")
    last = out.index("would start on abc123")
    assert first < rolling < last


def test_parse_strategy_defaults_to_rolling():
    services = config.parse_services({"services": {"web": {"labels": {"deploy.role": "app"}}}})
    assert services[0].strategy == "rolling"
    assert not services[0].drains_first


def test_image_tag():
    assert _image_tag("ghcr.io/org/app:v1.2.0") == "v1.2.0"
    assert _image_tag("localhost:5000/app:abc") == "abc"
    assert _image_tag("localhost:5000/app") is None
    assert _image_tag("app") is None
