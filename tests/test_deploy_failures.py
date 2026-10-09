"""Failure paths: signals, crashes, cleanup failures, and their exit codes."""

import signal

import pytest

from flow_deploy import deploy as deploy_mod
from flow_deploy.deploy import EXIT_DEGRADED, EXIT_FAILED, _image_tag, _wait_for_healthy, deploy
from tests.test_deploy import (
    COMPOSE_CMD,
    COMPOSE_CONFIG_YAML,
    WEB_CONTAINER_NEW,
    WEB_CONTAINER_OLD,
    _chdir,
    _err,
    _git_preflight,
    _ok,
)
from tests.test_drain_first import COMPOSE_CONFIG_YAML as DRAIN_CONFIG
from tests.test_drain_first import WORKER_DRAIN, WORKER_NEW, _commands

WEB_ONLY = ["web"]


def _lock_released(tmp_path):
    return not (tmp_path / ".git" / "deploy-lock").exists()


# ---------------------------------------------------------------------------
# Signals and unexpected exceptions revert like any other failure
# ---------------------------------------------------------------------------


def test_sigterm_mid_deploy_restores_drained_and_releases(mock_process, monkeypatch, tmp_path):
    _chdir(monkeypatch, tmp_path)
    original = signal.getsignal(signal.SIGTERM)

    def cancelled(*args, **kwargs):
        # What a cancelled CI job delivers while web is rolling.
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

    monkeypatch.setattr(deploy_mod, "_deploy_service", cancelled)
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(DRAIN_CONFIG),
            *WORKER_DRAIN,
            _ok(),  # git restore
            _ok(),  # worker: restore on old tag
        ]
    )
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == EXIT_FAILED
    assert mock_process.calls[-1][2] == {"DEPLOY_TAG": "v1.2.0"}
    assert _lock_released(tmp_path)
    assert signal.getsignal(signal.SIGTERM) is original


def test_sighup_from_a_dropped_ssh_session_reverts(mock_process, monkeypatch, tmp_path):
    _chdir(monkeypatch, tmp_path)

    def hung_up(*args, **kwargs):
        signal.getsignal(signal.SIGHUP)(signal.SIGHUP, None)

    monkeypatch.setattr(deploy_mod, "_deploy_service", hung_up)
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(DRAIN_CONFIG),
            *WORKER_DRAIN,
            _ok(),  # git restore
            _ok(),  # worker: restore
        ]
    )
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == EXIT_FAILED
    assert mock_process.calls[-1][2] == {"DEPLOY_TAG": "v1.2.0"}


def test_signal_while_a_drain_is_stopping_still_restores(mock_process, monkeypatch, tmp_path):
    """A drain can take an hour: a cancel mid-stop must still know what to restore."""
    _chdir(monkeypatch, tmp_path)

    def stop_interrupted(container_id, timeout=30):
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

    monkeypatch.setattr(deploy_mod.containers, "stop_container", stop_interrupted)
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(DRAIN_CONFIG),
            _ok(),  # worker: pull
            WORKER_DRAIN[1],  # worker: docker ps
            _ok(),  # git restore
            _ok(),  # worker: restore
        ]
    )
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == EXIT_FAILED
    assert "--scale worker=1 worker" in _commands(mock_process)[-1]
    assert mock_process.calls[-1][2] == {"DEPLOY_TAG": "v1.2.0"}


def test_signals_are_ignored_while_reverting(mock_process, monkeypatch, tmp_path):
    """A second signal mid-revert neither cuts it short nor starts another."""
    _chdir(monkeypatch, tmp_path)
    seen = []

    def restore(drained, compose_cmd):
        seen.append(signal.getsignal(signal.SIGTERM))
        return True

    monkeypatch.setattr(deploy_mod, "_restore_drained", restore)
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(DRAIN_CONFIG),
            *WORKER_DRAIN,
            _err("pull failed"),  # web: pull
            _ok(),  # git restore
        ]
    )
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == EXIT_FAILED
    assert seen == [signal.SIG_IGN]


def test_unexpected_exception_reverts(mock_process, monkeypatch, tmp_path, capsys):
    _chdir(monkeypatch, tmp_path)

    def boom(*args, **kwargs):
        raise KeyError("Image")

    monkeypatch.setattr(deploy_mod, "_deploy_service", boom)
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(DRAIN_CONFIG),
            *WORKER_DRAIN,
            _ok(),  # git restore
            _ok(),  # worker: restore
        ]
    )
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == EXIT_FAILED
    assert "unexpected error: KeyError('Image'); reverting" in capsys.readouterr().err
    assert mock_process.calls[-1][2] == {"DEPLOY_TAG": "v1.2.0"}
    assert _lock_released(tmp_path)


def test_exception_before_checkout_skips_git_restore(mock_process, monkeypatch, tmp_path):
    _chdir(monkeypatch, tmp_path)

    def broken(_tag):
        raise OSError("git not found")

    monkeypatch.setattr(deploy_mod.git, "preflight_and_checkout", broken)
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == EXIT_FAILED
    assert mock_process.calls == []  # nothing to restore
    assert _lock_released(tmp_path)


def test_interrupt_during_start_keeps_already_started_services(mock_process, monkeypatch, tmp_path):
    """Services healthy on the new tag stay; only unstarted ones are restored."""
    _chdir(monkeypatch, tmp_path)
    two = DRAIN_CONFIG + (
        "  scheduler:\n"
        "    image: ghcr.io/myorg/myapp:latest\n"
        "    labels:\n"
        "      deploy.role: app\n"
        "      deploy.strategy: drain-first\n"
        '      deploy.order: "300"\n'
        "    healthcheck:\n"
        '      test: ["CMD", "true"]\n'
    )
    real_start = deploy_mod._start_service

    def start(svc, *args, **kwargs):
        if svc.name == "scheduler":
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        return real_start(svc, *args, **kwargs)

    monkeypatch.setattr(deploy_mod, "_start_service", start)
    monkeypatch.setattr(deploy_mod, "_deploy_service", lambda *a, **kw: 0)
    sched_old = '{"ID": "old_sch_666", "Image": "ghcr.io/myorg/myapp:v1.1.0"}'
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(two),
            _ok(),  # scheduler: pull
            _ok(sched_old + "\n"),  # scheduler: ps
            _ok(),  # scheduler: stop
            _ok(),  # scheduler: rm
            *WORKER_DRAIN,
            _ok(),  # worker: compose up
            _ok(WORKER_NEW + "\n"),  # worker: ps
            _ok("healthy\n"),  # worker: health
            _ok(),  # git restore
            _ok(),  # scheduler: restore
        ]
    )
    assert deploy(tag="abc123", cmd=COMPOSE_CMD) == EXIT_FAILED
    restores = [c for c in mock_process.calls if c[2] and c[2]["DEPLOY_TAG"] != "abc123"]
    assert [c[2]["DEPLOY_TAG"] for c in restores] == ["v1.1.0"]  # scheduler only


# ---------------------------------------------------------------------------
# Cleanup failures turn a clean failure (1) into a degraded one (3)
# ---------------------------------------------------------------------------


def test_rolling_abort_cleanup_failure_is_degraded(mock_process, monkeypatch, tmp_path, capsys):
    _chdir(monkeypatch, tmp_path)
    monkeypatch.setattr(deploy_mod, "_wait_for_healthy", lambda *a, **kw: False)
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(COMPOSE_CONFIG_YAML),
            _ok(),  # web: pull
            _ok(),  # web: scale to 2
            _ok(WEB_CONTAINER_OLD + "\n" + WEB_CONTAINER_NEW + "\n"),  # web: ps
            _err("stop failed"),  # web: stop new
            _ok(),  # web: rm new
            _ok(),  # web: scale back
            _ok(),  # git restore
        ]
    )
    result = deploy(tag="abc123", services_filter=WEB_ONLY, cmd=COMPOSE_CMD)
    assert result == EXIT_DEGRADED
    captured = capsys.readouterr()
    assert "abort incomplete: new container new_web" in captured.out
    assert "cleanup failed for web" in captured.err


def test_rolling_cutover_cleanup_failure_is_degraded(mock_process, monkeypatch, tmp_path):
    _chdir(monkeypatch, tmp_path)
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(COMPOSE_CONFIG_YAML),
            _ok(),  # web: pull
            _ok(),  # web: scale to 2
            _ok(WEB_CONTAINER_OLD + "\n" + WEB_CONTAINER_NEW + "\n"),  # web: ps
            _ok("healthy\n"),  # web: health
            _err("stop failed"),  # web: stop old
            _ok(),  # web: rm old
            _ok(),  # web: scale back
            _ok(),  # git restore
        ]
    )
    assert deploy(tag="abc123", services_filter=WEB_ONLY, cmd=COMPOSE_CMD) == EXIT_DEGRADED


@pytest.mark.parametrize(("scale_back", "expected"), [(_ok(), 1), (_err("busy"), 3)])
def test_rolling_scale_up_failure(mock_process, monkeypatch, tmp_path, scale_back, expected):
    _chdir(monkeypatch, tmp_path)
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(COMPOSE_CONFIG_YAML),
            _ok(),  # web: pull
            _err("port in use"),  # web: scale to 2
            scale_back,  # web: scale back
            _ok(),  # git restore
        ]
    )
    assert deploy(tag="abc123", services_filter=WEB_ONLY, cmd=COMPOSE_CMD) == expected


def test_rolling_unidentifiable_containers_scale_back(mock_process, monkeypatch, tmp_path):
    _chdir(monkeypatch, tmp_path)
    twin = '{"ID": "%s", "Image": "ghcr.io/myorg/myapp:abc123", "CreatedAt": "2024-01-01"}'
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(COMPOSE_CONFIG_YAML),
            _ok(),  # web: pull
            _ok(),  # web: scale to 2
            _ok(twin % "a1" + "\n" + twin % "b2" + "\n"),  # web: ps (same tag, same time)
            _ok(),  # web: scale back
            _ok(),  # git restore
        ]
    )
    assert deploy(tag="abc123", services_filter=WEB_ONLY, cmd=COMPOSE_CMD) == EXIT_FAILED


@pytest.mark.parametrize(
    ("stop", "rm"), [(_err("stuck"), _ok()), (_ok(), _err("in use"))], ids=["stop", "rm"]
)
def test_failed_start_that_cannot_be_removed_is_degraded(
    mock_process, monkeypatch, tmp_path, stop, rm
):
    _chdir(monkeypatch, tmp_path)
    monkeypatch.setattr(deploy_mod, "_wait_for_healthy", lambda *a, **kw: False)
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(DRAIN_CONFIG),
            *WORKER_DRAIN,
            _ok(),  # worker: compose up
            _ok(WORKER_NEW + "\n"),  # worker: ps
            stop,  # worker: stop unhealthy new
            rm,  # worker: rm unhealthy new
            _ok(),  # git restore
            _ok(),  # worker: restore
        ]
    )
    result = deploy(tag="abc123", services_filter=["worker"], cmd=COMPOSE_CMD)
    assert result == EXIT_DEGRADED


def test_start_scales_to_the_drained_replica_count(mock_process, monkeypatch, tmp_path):
    _chdir(monkeypatch, tmp_path)
    old2 = '{"ID": "old_wrk_555", "Image": "ghcr.io/myorg/myapp:v1.2.0"}'
    new2 = '{"ID": "new_wrk_888", "Image": "ghcr.io/myorg/myapp:abc123"}'
    mock_process.responses.extend(
        [
            *_git_preflight(),
            _ok(DRAIN_CONFIG),
            _ok(),  # worker: pull
            _ok(WORKER_DRAIN[1].stdout.strip() + "\n" + old2 + "\n"),  # worker: ps (2)
            _ok(),  # stop 1
            _ok(),  # rm 1
            _ok(),  # stop 2
            _ok(),  # rm 2
            _ok(),  # worker: compose up --scale worker=2
            _ok(WORKER_NEW + "\n" + new2 + "\n"),  # worker: ps (2)
            _ok("healthy\n"),  # worker 1: health
            _ok("healthy\n"),  # worker 2: health
        ]
    )
    assert deploy(tag="abc123", services_filter=["worker"], cmd=COMPOSE_CMD) == 0
    assert any("--scale worker=2 worker" in c for c in _commands(mock_process))


# ---------------------------------------------------------------------------
# Dry run and helpers
# ---------------------------------------------------------------------------


def test_dry_run_compose_failure(mock_process, monkeypatch, tmp_path):
    _chdir(monkeypatch, tmp_path)
    mock_process.responses.append(_err("bad compose file"))
    assert deploy(tag="abc123", dry_run=True, cmd=COMPOSE_CMD) == 1


def test_dry_run_unknown_service(mock_process, monkeypatch, tmp_path):
    _chdir(monkeypatch, tmp_path)
    mock_process.responses.append(_ok(COMPOSE_CONFIG_YAML))
    assert deploy(tag="abc123", dry_run=True, services_filter=["nope"], cmd=COMPOSE_CMD) == 1


def test_image_tag_bare_id_has_no_tag():
    assert _image_tag("sha256:0123abcd") is None


def test_wait_for_healthy_returns_false_when_unhealthy(monkeypatch):
    statuses = iter(["starting", "unhealthy"])
    monkeypatch.setattr(deploy_mod.containers, "get_container_health", lambda _: next(statuses))
    monkeypatch.setattr(deploy_mod.time, "sleep", lambda _: None)
    assert _wait_for_healthy("abc", timeout=60, poll_interval=1) is False


def test_wait_for_healthy_times_out(monkeypatch):
    clock = iter([0, 0, 61])
    monkeypatch.setattr(deploy_mod.time, "time", lambda: next(clock))
    monkeypatch.setattr(deploy_mod.containers, "get_container_health", lambda _: "starting")
    monkeypatch.setattr(deploy_mod.time, "sleep", lambda _: None)
    assert _wait_for_healthy("abc", timeout=60, poll_interval=1) is False
