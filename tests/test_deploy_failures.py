"""Failure paths on a FakeHost: signals, crashes, cleanup failures, exit codes."""

import signal

import pytest

from flow_deploy import deploy as deploy_mod
from flow_deploy.deploy import EXIT_DEGRADED, EXIT_FAILED, _wait_for_healthy, deploy
from tests.fake_host import COMPOSE_CMD, FakeHost
from tests.helpers import DRAIN, ROLLING, app, chdir, stack


def send(signum):
    """Deliver ``signum`` to the current handler, as the kernel would mid-command."""
    return lambda: signal.getsignal(signum)(signum, None)


def lock_released(tmp_path):
    return not (tmp_path / ".git" / "deploy-lock").exists()


@pytest.fixture
def drain_host(monkeypatch, tmp_path):
    chdir(monkeypatch, tmp_path)
    host = FakeHost(DRAIN, running={"web": ["v1"], "scheduler": ["v1"], "worker": ["v1"]})
    return host.install(monkeypatch)


@pytest.fixture
def rolling_host(monkeypatch, tmp_path):
    chdir(monkeypatch, tmp_path)
    return FakeHost(ROLLING, running={"web": ["v1"], "worker": ["v1"]}).install(monkeypatch)


def v1(host, *services):
    return {s: [host.images["v1"]] for s in services}


# ---------------------------------------------------------------------------
# Signals and unexpected exceptions revert like any other failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_signal_mid_roll_reverts_everything(drain_host, tmp_path, signum):
    original = signal.getsignal(signum)
    drain_host.before("--scale web=2", send(signum))
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == EXIT_FAILED
    assert drain_host.running_images() == v1(drain_host, "web", "scheduler", "worker")
    assert drain_host.sha == "prev123abc"
    assert lock_released(tmp_path)
    assert signal.getsignal(signum) is original


def test_signal_while_a_drain_is_stopping_waits_for_it_then_reverts(drain_host, capsys):
    """Held until the container has stopped and been removed, then reverted."""
    drain_host.before("docker stop --time 3660 worker", send(signal.SIGINT))
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == EXIT_FAILED
    assert "finishing the drain of worker0" in capsys.readouterr().out
    cmds = drain_host.commands()
    stop = drain_host.index("docker stop --time 3660 worker")
    assert cmds[stop + 1].startswith("docker rm worker")  # the stop and rm finished
    assert drain_host.running_images() == v1(drain_host, "web", "scheduler", "worker")


def test_a_second_signal_during_a_held_drain_is_also_held(drain_host):
    def two():
        send(signal.SIGINT)()
        send(signal.SIGTERM)()

    drain_host.before("docker stop --time 3660 worker", two)
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == EXIT_FAILED


def test_signals_are_ignored_while_reverting(drain_host):
    seen = []
    drain_host.unpullable.add("v2")
    drain_host.before(
        "git checkout --detach prev123abc", lambda: seen.append(signal.getsignal(signal.SIGTERM))
    )
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == EXIT_FAILED
    assert seen == [signal.SIG_IGN]


def test_signal_while_taking_the_lock_changes_nothing(monkeypatch, tmp_path):
    """Handlers are installed before the lock, so even this signal is handled."""
    chdir(monkeypatch, tmp_path)
    host = FakeHost(ROLLING).install(monkeypatch)
    released = []
    monkeypatch.setattr(deploy_mod.lock, "acquire", send(signal.SIGTERM))
    monkeypatch.setattr(deploy_mod.lock, "release", lambda: released.append(True))
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == EXIT_FAILED
    assert host.calls == []
    assert released == []  # never held, so never released


def test_finally_ignores_signals_while_releasing(rolling_host, monkeypatch):
    original = signal.getsignal(signal.SIGHUP)
    during_release = []
    real_release = deploy_mod.lock.release

    def release():
        during_release.append(signal.getsignal(signal.SIGHUP))
        real_release()

    monkeypatch.setattr(deploy_mod.lock, "release", release)
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 0
    assert during_release == [signal.SIG_IGN]
    assert signal.getsignal(signal.SIGHUP) is original


def test_unexpected_exception_reverts(drain_host, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise KeyError("Image")

    monkeypatch.setattr(deploy_mod, "_deploy_service", boom)
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == EXIT_FAILED
    assert "unexpected error: KeyError('Image'); reverting" in capsys.readouterr().err
    assert drain_host.running_images() == v1(drain_host, "web", "scheduler", "worker")


def test_exception_before_checkout_skips_git_restore(monkeypatch, tmp_path):
    chdir(monkeypatch, tmp_path)
    host = FakeHost(ROLLING).install(monkeypatch)

    def broken(_tag):
        raise OSError("git not found")

    monkeypatch.setattr(deploy_mod.git, "preflight_and_checkout", broken)
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == EXIT_FAILED
    assert host.calls == []
    assert lock_released(tmp_path)


def test_interrupt_during_start_reverts_services_already_started(drain_host):
    drain_host.before("--scale worker=1 worker", send(signal.SIGINT))
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == EXIT_FAILED
    # The scheduler was already healthy on v2; it goes back with the rest.
    assert drain_host.running_images() == v1(drain_host, "web", "scheduler", "worker")


# ---------------------------------------------------------------------------
# Cleanup failures turn a clean failure (1) into a degraded one (3)
# ---------------------------------------------------------------------------


def test_rolling_abort_cleanup_failure_is_degraded(rolling_host, capsys):
    rolling_host.unhealthy_services.add(("web", "v2"))
    rolling_host.fail("docker stop --time 30 web003")  # the new container won't stop
    result = deploy(tag="v2", services_filter=["web"], cmd=COMPOSE_CMD)
    assert result == EXIT_DEGRADED
    captured = capsys.readouterr()
    assert "abort incomplete: new container web003" in captured.out
    assert "cleanup failed for web" in captured.err


def test_rolling_cutover_cleanup_failure_still_succeeds(rolling_host, capsys):
    """As on master: the new container is healthy and serving, so it's a success."""
    rolling_host.fail("docker stop --time 30 web001")  # e.g. the old container already exited
    assert deploy(tag="v2", services_filter=["web"], cmd=COMPOSE_CMD) == 0
    captured = capsys.readouterr()
    assert "warning: cleanup of old container web001 did not complete" in captured.out
    assert "::error::" not in captured.out


def test_rolling_scale_up_failure_issues_no_further_commands(rolling_host):
    rolling_host.fail("--scale web=2 web", times=1)
    assert deploy(tag="v2", services_filter=["web"], cmd=COMPOSE_CMD) == EXIT_FAILED
    assert not any("--scale web=1" in c for c in rolling_host.commands())


@pytest.mark.parametrize("scale_back_fails, expected", [(False, 1), (True, 3)])
def test_rolling_unidentifiable_containers_scale_back(
    monkeypatch, tmp_path, scale_back_fails, expected
):
    chdir(monkeypatch, tmp_path)
    host = FakeHost(stack(web=app()), running={"web": ["v2"]}).install(monkeypatch)
    # Same tag on both, so old and new are told apart by CreatedAt: make them equal.
    monkeypatch.setattr(deploy_mod.containers, "identify_old_new", lambda ctrs, tag: (None, None))
    if scale_back_fails:
        host.fail("--scale web=1 web")
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == expected


def test_start_cannot_list_containers_is_degraded(drain_host):
    """Couldn't see what started, so couldn't remove it: exit 3, not a clean revert."""
    drain_host.fail("--filter label=com.docker.compose.service=worker", skip=2, times=1)
    assert deploy(tag="v2", services_filter=["worker"], cmd=COMPOSE_CMD) == EXIT_DEGRADED


@pytest.mark.parametrize("command", ["docker stop --time 3660 worker", "docker rm worker"])
def test_failed_start_that_cannot_be_removed_is_degraded(drain_host, command):
    drain_host.unhealthy_services.add(("worker", "v2"))
    drain_host.fail(command, skip=1)  # past the drain's own stop/rm
    assert deploy(tag="v2", services_filter=["worker"], cmd=COMPOSE_CMD) == EXIT_DEGRADED


def test_start_scales_to_the_drained_replica_count(monkeypatch, tmp_path):
    chdir(monkeypatch, tmp_path)
    host = FakeHost(DRAIN, running={"worker": ["v1", "v1"]}).install(monkeypatch)
    assert deploy(tag="v2", services_filter=["worker"], cmd=COMPOSE_CMD) == 0
    assert host.running() == {"worker": ["v2", "v2"]}


# ---------------------------------------------------------------------------
# Dry run and helpers
# ---------------------------------------------------------------------------


def test_dry_run_compose_failure(rolling_host):
    rolling_host.fail("docker compose config")
    assert deploy(tag="v2", dry_run=True, cmd=COMPOSE_CMD) == 1


def test_dry_run_unknown_service(rolling_host):
    assert deploy(tag="v2", dry_run=True, services_filter=["nope"], cmd=COMPOSE_CMD) == 1


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
