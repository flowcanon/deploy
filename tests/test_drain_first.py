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
    # Only the drain's stop is out of the process group; rolling's isn't.
    assert mock_process.new_sessions == [["docker", "stop", "--time", "3660", "old_wrk_333"]]
    start = _index(cmds, "--scale worker=1 worker")
    assert drain < roll < start
    # The worker never scales to 2: no overlap of old and new.
    assert not any("--scale worker=2" in c for c in cmds)


def test_drain_first_service_not_running_is_started_at_one_replica(
    mock_process, monkeypatch, tmp_path
):
    """Deliberate: a service with no containers (newly added, or crashed) comes
    up with the deploy rather than staying at zero."""
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
    assert any("--scale worker=1 worker" in c for c in _commands(mock_process))


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
    assert "up -d --no-deps --scale worker=1 worker" in " ".join(restore[1])
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
    sched_old = json.dumps({"ID": "old_sch_666", "Image": "ghcr.io/myorg/myapp:v1.1.0"})
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(two_drainers),
            # Drains run in reverse deploy order: scheduler (300) before worker (100).
            _ok(),  # scheduler: pull
            _ok(sched_old + "\n"),  # scheduler: ps
            _ok(),  # scheduler: stop
            _ok(),  # scheduler: rm
            _err("pull failed"),  # worker: pull
            _ok(),  # git restore
            _err("no such image"),  # scheduler: restore fails
        ]
    )
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == 3
    assert mock_process.calls[-1][2] == {"DEPLOY_TAG": "v1.1.0"}
    assert not (tmp_path / ".git" / "deploy-lock").exists()


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
    assert not any("--scale worker=1 worker" in c for c in _commands(mock_process))


def test_start_failure_restores_previous_container(mock_process, monkeypatch, tmp_path, capsys):
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
            _ok(),  # worker: stop unhealthy new
            _ok(),  # worker: rm unhealthy new
            _ok(),  # git restore
            _ok(),  # worker: restore on old tag
        ]
    )
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == 1
    assert "worker failed to start" in capsys.readouterr().out
    # The unhealthy container is gone, not left consuming...
    cmds = _commands(mock_process)
    assert any("docker stop --time 3660 new_wrk_444" in c for c in cmds)
    assert any("docker rm new_wrk_444" in c for c in cmds)
    # ...and the previous one is back, even though web shipped.
    assert mock_process.calls[-1][2] == {"DEPLOY_TAG": "v1.2.0"}
    assert not (tmp_path / ".git" / "deploy-lock").exists()


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


def test_drain_stop_failure_aborts_before_rolling(mock_process, monkeypatch, tmp_path):
    _chdir(monkeypatch, tmp_path)
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(COMPOSE_CONFIG_YAML),
            _ok(),  # worker: pull
            _ok(WORKER_OLD + "\n"),  # worker: docker ps
            _err("daemon unreachable"),  # worker: docker stop fails
            _ok(),  # git restore
            _ok(),  # worker: restore on old tag
        ]
    )
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == 1
    cmds = _commands(mock_process)
    assert not any("--scale web=2" in c for c in cmds)
    assert "--scale worker=1 worker" in cmds[-1]


def test_drain_remove_failure_aborts_before_rolling(mock_process, monkeypatch, tmp_path):
    _chdir(monkeypatch, tmp_path)
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(COMPOSE_CONFIG_YAML),
            _ok(),  # worker: pull
            _ok(WORKER_OLD + "\n"),  # worker: docker ps
            _ok(),  # worker: docker stop
            _err("removal in progress"),  # worker: docker rm fails
            _ok(),  # git restore
            _ok(),  # worker: restore on old tag
        ]
    )
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == 1
    assert not any("--scale web=2" in c for c in _commands(mock_process))


def test_restore_brings_back_every_replica(mock_process, monkeypatch, tmp_path):
    _chdir(monkeypatch, tmp_path)
    second = json.dumps({"ID": "old_wrk_555", "Image": "ghcr.io/myorg/myapp:v1.2.0"})
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(COMPOSE_CONFIG_YAML),
            _ok(),  # worker: pull
            _ok(WORKER_OLD + "\n" + second + "\n"),  # worker: docker ps (2 replicas)
            _ok(),  # stop 1
            _ok(),  # rm 1
            _ok(),  # stop 2
            _ok(),  # rm 2
            _err("pull failed"),  # web: pull
            _ok(),  # git restore
            _ok(),  # worker: restore
        ]
    )
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == 1
    assert "--scale worker=2 worker" in _commands(mock_process)[-1]


def test_mixed_old_images_are_flagged(mock_process, monkeypatch, tmp_path, capsys):
    _chdir(monkeypatch, tmp_path)
    other = json.dumps({"ID": "old_wrk_555", "Image": "ghcr.io/myorg/myapp:v1.1.9"})
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(COMPOSE_CONFIG_YAML),
            _ok(),  # worker: pull
            _ok(WORKER_OLD + "\n" + other + "\n"),  # worker: docker ps
            _ok(),  # stop 1
            _ok(),  # rm 1
            _ok(),  # stop 2
            _ok(),  # rm 2
            _err("pull failed"),  # web: pull
            _ok(),  # git restore
            _ok(),  # worker: restore
        ]
    )
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == 1
    assert "was running mixed images" in capsys.readouterr().err
    assert mock_process.calls[-1][2] == {"DEPLOY_TAG": "v1.2.0"}


def test_failed_restore_exits_degraded_and_releases_the_lock(
    mock_process, monkeypatch, tmp_path, capsys
):
    _chdir(monkeypatch, tmp_path)
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(COMPOSE_CONFIG_YAML),
            *WORKER_DRAIN,
            _err("pull failed"),  # web: pull
            _ok(),  # git restore
            _err("no such image"),  # worker: restore fails
        ]
    )
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == 3
    assert "drained services could not be restored" in capsys.readouterr().err
    assert not (tmp_path / ".git" / "deploy-lock").exists()


def test_untagged_old_image_cannot_be_restored(mock_process, monkeypatch, tmp_path, capsys):
    _chdir(monkeypatch, tmp_path)
    digest = json.dumps({"ID": "old_wrk_333", "Image": "ghcr.io/myorg/myapp@sha256:abc"})
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(COMPOSE_CONFIG_YAML),
            _ok(),  # worker: pull
            _ok(digest + "\n"),  # worker: docker ps
            _ok(),  # stop
            _ok(),  # rm
            _err("pull failed"),  # web: pull
            _ok(),  # git restore
        ]
    )
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == 3
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "cannot restore worker: no tag in its previous image" in output
    assert "ghcr.io/myorg/myapp@sha256:abc" in output


def test_service_filter_skipping_drain_first_warns(mock_process, monkeypatch, tmp_path, capsys):
    _chdir(monkeypatch, tmp_path)
    mock_process.responses.extend([*_git_preflight(), _ok(COMPOSE_CONFIG_YAML), *WEB_ROLL])
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert deploy(tag="abc123", services_filter=["web"], cmd=COMPOSE_CMD) == 0
    captured = capsys.readouterr()
    assert "warning: --service leaves drain-first worker running old code" in captured.out
    # A warning on a deploy that succeeds: no error annotation.
    assert "::error::" not in captured.out
    assert captured.err == ""


def test_start_failure_with_nothing_rolled_restores_old_tag(
    mock_process, monkeypatch, tmp_path, capsys
):
    _chdir(monkeypatch, tmp_path)
    monkeypatch.setattr("flow_deploy.deploy._wait_for_healthy", lambda *a, **kw: False)
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(COMPOSE_CONFIG_YAML),
            *WORKER_DRAIN,
            _ok(),  # worker: compose up (new tag)
            _ok(WORKER_NEW + "\n"),  # worker: docker ps
            _ok(),  # worker: stop unhealthy new
            _ok(),  # worker: rm unhealthy new
            _ok(),  # git restore
            _ok(),  # worker: restore on old tag
        ]
    )
    assert deploy(tag="abc123", services_filter=["worker"], cmd=COMPOSE_CMD) == 1
    assert mock_process.calls[-1][2] == {"DEPLOY_TAG": "v1.2.0"}
    out = capsys.readouterr().out
    assert "restoring worker on v1.2.0" in out
    assert "worker failed to start" in out


def test_drains_in_reverse_order_and_starts_in_order(mock_process, monkeypatch, tmp_path):
    """worker (100) then scheduler (300) in deploy order: drain scheduler first,
    start worker first, roll web in between."""
    _chdir(monkeypatch, tmp_path)
    ordered = COMPOSE_CONFIG_YAML + (
        "  scheduler:\n"
        "    image: ghcr.io/myorg/myapp:latest\n"
        "    labels:\n"
        "      deploy.role: app\n"
        "      deploy.strategy: drain-first\n"
        '      deploy.order: "300"\n'
        "    healthcheck:\n"
        '      test: ["CMD", "true"]\n'
    )
    sched_old = json.dumps({"ID": "old_sch_666", "Image": "ghcr.io/myorg/myapp:v1.2.0"})
    sched_new = json.dumps({"ID": "new_sch_777", "Image": "ghcr.io/myorg/myapp:abc123"})
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(ordered),
            _ok(),  # scheduler: pull
            _ok(sched_old + "\n"),  # scheduler: ps
            _ok(),  # scheduler: stop
            _ok(),  # scheduler: rm
            *WORKER_DRAIN,
            *WEB_ROLL,
            _ok(),  # worker: compose up
            _ok(WORKER_NEW + "\n"),  # worker: ps
            _ok("healthy\n"),  # worker: health
            _ok(),  # scheduler: compose up
            _ok(sched_new + "\n"),  # scheduler: ps
            _ok("healthy\n"),  # scheduler: health
        ]
    )
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == 0
    cmds = _commands(mock_process)
    assert (
        _index(cmds, "docker stop", "old_sch_666")
        < _index(cmds, "docker stop", "old_wrk_333")
        < _index(cmds, "--scale web=2")
        < _index(cmds, "--scale worker=1 worker")
        < _index(cmds, "--scale scheduler=1 scheduler")
    )


def test_one_start_failure_does_not_strand_the_others(mock_process, monkeypatch, tmp_path):
    _chdir(monkeypatch, tmp_path)
    two = COMPOSE_CONFIG_YAML + (
        "  scheduler:\n"
        "    image: ghcr.io/myorg/myapp:latest\n"
        "    labels:\n"
        "      deploy.role: app\n"
        "      deploy.strategy: drain-first\n"
        "    healthcheck:\n"
        '      test: ["CMD", "true"]\n'
    )
    sched_old = json.dumps({"ID": "old_sch_666", "Image": "ghcr.io/myorg/myapp:v1.2.0"})
    sched_new = json.dumps({"ID": "new_sch_777", "Image": "ghcr.io/myorg/myapp:abc123"})
    monkeypatch.setattr(
        "flow_deploy.deploy._wait_for_healthy", lambda ctr_id, *a: ctr_id != "new_wrk_444"
    )
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(two),
            # Same order, so file order breaks the tie: scheduler drains first.
            _ok(),  # scheduler: pull
            _ok(sched_old + "\n"),  # scheduler: ps
            _ok(),  # scheduler: stop
            _ok(),  # scheduler: rm
            *WORKER_DRAIN,
            _ok(),  # web: pull
            _ok(),  # web: scale to 2
            _ok(WEB_CONTAINER_OLD + "\n" + WEB_CONTAINER_NEW + "\n"),  # web: ps
            _ok(),  # web: stop old
            _ok(),  # web: rm old
            _ok(),  # web: scale back
            _ok(),  # worker: compose up
            _ok(WORKER_NEW + "\n"),  # worker: ps (then unhealthy)
            _ok(),  # worker: stop unhealthy new
            _ok(),  # worker: rm unhealthy new
            _ok(),  # scheduler: compose up
            _ok(sched_new + "\n"),  # scheduler: ps (healthy)
            _ok(),  # git restore
            _ok(),  # worker: restore on old tag
        ]
    )
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == 1
    cmds = _commands(mock_process)
    assert any("--scale scheduler=1 scheduler" in c for c in cmds)
    # Only the failed worker goes back; the healthy new scheduler stays.
    assert "--scale worker=1 worker" in cmds[-1]
    assert mock_process.calls[-1][2] == {"DEPLOY_TAG": "v1.2.0"}


def test_rolling_failure_after_another_shipped_still_restores_drained(
    mock_process, monkeypatch, tmp_path
):
    _chdir(monkeypatch, tmp_path)
    with_api = COMPOSE_CONFIG_YAML + (
        "  api:\n"
        "    image: ghcr.io/myorg/myapp:latest\n"
        "    labels:\n"
        "      deploy.role: app\n"
        "    healthcheck:\n"
        '      test: ["CMD", "true"]\n'
    )
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(with_api),
            *WORKER_DRAIN,
            *WEB_ROLL,  # web ships
            _err("pull failed"),  # api: pull
            _ok(),  # git restore
            _ok(),  # worker: restore on old tag
        ]
    )
    # Expand-then-contract keeps the old worker compatible with whatever web
    # migrated, so it comes back rather than leaving the queue unworked.
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == 1
    assert mock_process.calls[-1][2] == {"DEPLOY_TAG": "v1.2.0"}
    assert not (tmp_path / ".git" / "deploy-lock").exists()


def test_start_compose_failure_removes_partial_containers(mock_process, monkeypatch, tmp_path):
    _chdir(monkeypatch, tmp_path)
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(COMPOSE_CONFIG_YAML),
            *WORKER_DRAIN,
            *WEB_ROLL,
            _err("partial"),  # worker: compose up
            _ok(WORKER_NEW + "\n"),  # worker: docker ps (one started anyway)
            _ok(),  # worker: stop
            _ok(),  # worker: rm
            _ok(),  # git restore
        ]
    )
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == 1
    assert any("docker rm new_wrk_444" in c for c in _commands(mock_process))


def test_parse_strategy_defaults_to_rolling():
    services = config.parse_services({"services": {"web": {"labels": {"deploy.role": "app"}}}})
    assert services[0].strategy == "rolling"
    assert not services[0].drains_first


def test_image_tag():
    assert _image_tag("ghcr.io/org/app:v1.2.0") == "v1.2.0"
    assert _image_tag("localhost:5000/app:abc") == "abc"
    assert _image_tag("localhost:5000/app") is None
    assert _image_tag("app") is None
    assert _image_tag("ghcr.io/org/app@sha256:abc") is None
    assert _image_tag("ghcr.io/org/app:v1.2.0@sha256:abc") == "v1.2.0"
