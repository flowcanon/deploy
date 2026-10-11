"""deploy.strategy=drain-first on a FakeHost: ordering, replicas, and full reverts."""

import pytest

from flow_deploy import config
from flow_deploy.deploy import deploy
from tests.fake_host import COMPOSE_CMD, FakeHost
from tests.helpers import DRAIN, app, chdir, record, stack

V1 = {"web": ["v1"], "scheduler": ["v1"], "worker": ["v1", "v1"]}


@pytest.fixture
def host(monkeypatch, tmp_path):
    """web, scheduler, and 2 worker replicas on v1."""
    chdir(monkeypatch, tmp_path)
    return FakeHost(DRAIN, running=dict(V1)).install(monkeypatch)


def v1_images(host):
    """What V1 looks like by image: a revert restores images (by alias), not tag names."""
    image = host.images["v1"]
    return {svc: [image] * len(tags) for svc, tags in V1.items()}


def test_happy_path_drains_in_reverse_rolls_then_starts_in_order(host, tmp_path):
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 0
    assert host.running() == {"web": ["v2"], "scheduler": ["v2"], "worker": ["v2", "v2"]}
    assert record(tmp_path)["tag"] == "v2"
    assert (
        host.index("docker stop", "worker")
        < host.index("docker stop", "scheduler")
        < host.index("--scale web=2")
        < host.index("--scale scheduler=1 scheduler")
        < host.index("--scale worker=2 worker")
    )
    # Drains use the service's drain timeout, out of the process group.
    assert any("docker stop --time 3660 worker" in c for c in host.commands())
    assert all(args[1] == "stop" for args in host.new_sessions) and len(host.new_sessions) == 3
    # The worker never overlaps old and new.
    assert not any("--scale worker=3" in c or "--scale worker=4" in c for c in host.commands())


def test_new_drain_first_service_starts_at_one_replica(monkeypatch, tmp_path):
    """Deliberate: a service with no containers (newly added, or crashed) comes
    up with the deploy rather than staying at zero."""
    chdir(monkeypatch, tmp_path)
    host = FakeHost(DRAIN, running={"web": ["v1"], "worker": ["v1"]}).install(monkeypatch)
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 0
    assert host.running() == {"web": ["v2"], "scheduler": ["v2"], "worker": ["v2"]}


def test_bad_tag_fails_at_pull_before_anything_stops(host, tmp_path):
    host.unpullable.add("bad")
    assert deploy(tag="bad", cmd=COMPOSE_CMD) == 1
    assert not any(c.startswith("docker stop") for c in host.commands())
    assert host.running_images() == v1_images(host)
    assert host.sha == "prev123abc"
    assert not (tmp_path / ".git" / "deploy-lock").exists()


@pytest.mark.parametrize("command", ["docker stop", "docker rm"])
def test_drain_cleanup_failure_aborts_before_rolling(host, command):
    host.fail(command)
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1
    assert not any("--scale web=2" in c for c in host.commands())
    assert host.running_images() == v1_images(host)


def test_drain_aborts_when_docker_ps_fails(host, capsys):
    """Couldn't list != nothing running: never roll past an unknown drain."""
    host.fail("--filter label=com.docker.compose.service=worker", skip=1, times=1)
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1
    assert not any("--scale web=2" in c for c in host.commands())
    assert "not draining blind" in capsys.readouterr().out
    assert host.running_images() == v1_images(host)


def test_restarting_containers_are_drained_too(monkeypatch, tmp_path):
    chdir(monkeypatch, tmp_path)
    host = FakeHost(DRAIN, running={"web": ["v1"], "scheduler": ["v1"]}).install(monkeypatch)
    host.add("worker", "v1", state="restarting")
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 0
    assert [(c.tag, c.state) for c in host.ctrs if c.service == "worker"] == [("v2", "running")]


def test_rolling_failure_restores_drained_services(host):
    host.unhealthy_services.add(("web", "v2"))
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1
    assert host.running_images() == v1_images(host)
    restores = [c for c in host.commands() if "--no-recreate --scale worker=2 worker" in c]
    assert len(restores) == 1


def test_phase_3_failure_reverts_everything(host, tmp_path):
    """The worker fails to start: web (shipped) and the scheduler (started
    healthy) go back too, so nothing new runs beside old."""
    host.unhealthy_services.add(("worker", "v2"))
    before = host.running_images()

    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1

    assert host.running_images() == before
    assert host.sha == "prev123abc"
    assert record(tmp_path)["tag"] == "v1"
    # Undo order: web rolled back before the drain-first services come back.
    aliased = [
        i
        for i, env in enumerate(host.envs)
        if (env or {}).get("DEPLOY_TAG") == "v1" and host.calls[i][2] == "up"
    ]
    rollback = next(i for i in aliased if "web=2" in host.calls[i])
    restore = next(i for i in aliased if "scheduler=1" in host.calls[i])
    assert rollback < restore


def test_one_start_failure_does_not_strand_the_others(host):
    host.unhealthy_services.add(("scheduler", "v2"))
    deploy(tag="v2", cmd=COMPOSE_CMD)
    # The worker was still started (and then reverted with everything else).
    assert any("--no-recreate" not in c and "--scale worker=2 worker" in c for c in host.commands())


def test_failed_start_that_cannot_be_removed_exits_3(host):
    host.unhealthy_services.add(("worker", "v2"))
    host.fail("docker rm worker", skip=2)  # the drain's two removals succeed
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 3


def test_start_that_cannot_list_containers_exits_3(host):
    # Snapshot and drain list worker containers first; the third lookup is the start's.
    host.fail("--filter label=com.docker.compose.service=worker", skip=2, times=1)
    assert deploy(tag="v2", services_filter=["worker"], cmd=COMPOSE_CMD) == 3


def test_start_compose_failure_reverts(host):
    host.fail("--no-deps --scale worker=2 worker")
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1
    assert host.running_images() == v1_images(host)


def test_restore_failure_exits_3_and_releases_the_lock(host, tmp_path, capsys):
    host.unhealthy_services.add(("web", "v2"))
    host.fail("--no-recreate --scale worker=2 worker")
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 3
    assert "drained services could not be restored" in capsys.readouterr().err
    assert not (tmp_path / ".git" / "deploy-lock").exists()


def test_restore_removes_a_stopped_new_container_first(host):
    """The worker's failed start was stopped but not removed; the restore must
    not let compose start that new-image leftover again."""
    host.unhealthy_services.add(("worker", "v2"))
    host.fail("docker rm worker", skip=2, times=1)  # past the drain's two removals
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 3  # the failed start's cleanup failed
    assert host.running_images() == v1_images(host)
    assert not any(c.state == "exited" for c in host.ctrs)


def test_restore_that_cannot_list_stopped_containers_exits_3(host):
    host.unhealthy_services.add(("web", "v2"))
    host.fail("--filter status=created")
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 3


def test_restore_cannot_inspect_exits_3(host):
    """The scheduler started healthy, so its restore must inspect what's running."""
    host.unhealthy_services.add(("worker", "v2"))
    host.fail("docker inspect --format {{.Id}} {{.Image}} scheduler", skip=1)  # past the snapshot
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 3


def test_restore_from_latest_uses_the_previous_commit(monkeypatch, tmp_path):
    chdir(monkeypatch, tmp_path)
    host = FakeHost(DRAIN, running={"web": ["latest"], "worker": ["latest"]})
    host.install(monkeypatch)
    host.registry["prev123abc"] = host.images["latest"]
    before = host.running_images()
    host.unhealthy_services.add(("web", "v2"))
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1
    assert host.running_images() == before
    assert host.running()["worker"] == ["prev123abc"]


def test_new_containers_that_cannot_be_drained_on_revert_exit_3(host):
    host.unhealthy_services.add(("worker", "v2"))
    # Phase 3 starts the scheduler healthy; reverting must drain it first.
    host.fail("docker stop --time 30 scheduler", skip=1)  # past phase 1's drain
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 3


def test_service_not_running_before_stays_stopped_after_revert(monkeypatch, tmp_path, capsys):
    chdir(monkeypatch, tmp_path)
    host = FakeHost(DRAIN, running={"web": ["v1"], "worker": ["v1"]}).install(monkeypatch)
    host.unhealthy_services.add(("worker", "v2"))
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1
    v1 = host.images["v1"]
    # The scheduler phase 3 started is drained away again: it wasn't running before.
    assert host.running_images() == {"web": [v1], "worker": [v1]}
    assert "scheduler was not running before the deploy" in capsys.readouterr().out


def test_service_filter_skipping_drain_first_warns_without_an_error(host, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert deploy(tag="v2", services_filter=["web"], cmd=COMPOSE_CMD) == 0
    out = capsys.readouterr().out
    assert "warning: --service leaves drain-first scheduler, worker running old code" in out
    assert "::error::" not in out


def test_service_filter_drain_first_only_failure_restores(host):
    host.unhealthy_services.add(("worker", "v2"))
    assert deploy(tag="v2", services_filter=["worker"], cmd=COMPOSE_CMD) == 1
    assert host.running_images() == v1_images(host)


def test_unknown_strategy_is_rejected(monkeypatch, tmp_path, capsys):
    chdir(monkeypatch, tmp_path)
    FakeHost(stack(web=app(strategy="blue-green"))).install(monkeypatch)
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1
    assert "Unknown deploy.strategy: web=blue-green" in capsys.readouterr().err


def test_dry_run_shows_the_three_phases(host, capsys):
    assert deploy(tag="v2", dry_run=True, cmd=COMPOSE_CMD) == 0
    out = capsys.readouterr().out
    assert (
        out.index("would drain running containers first (3660s timeout)")
        < out.index("would scale to 2")
        < out.index("would start on v2")
    )
    assert out.index("▸ worker") < out.index("▸ scheduler")


def test_ties_in_deploy_order_follow_file_order(monkeypatch, tmp_path):
    chdir(monkeypatch, tmp_path)
    tied = stack(web=app(100), a=app(300, "drain-first"), b=app(300, "drain-first"))
    host = FakeHost(tied, running={"web": ["v1"], "a": ["v1"], "b": ["v1"]})
    host.install(monkeypatch)
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 0
    assert host.index("docker stop", " b0") < host.index("docker stop", " a0")
    assert host.index("--scale a=1 a") < host.index("--scale b=1 b")


def test_drain_first_service_can_skip_its_healthcheck(monkeypatch, tmp_path):
    chdir(monkeypatch, tmp_path)
    beat = app(200, "drain-first", skip=True, healthcheck=False)
    host = FakeHost(stack(web=app(), beat=beat), running={"web": ["v1"], "beat": ["v1"]})
    host.install(monkeypatch)
    host.unhealthy_services.add(("beat", "v2"))  # never consulted
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 0
    assert host.running() == {"web": ["v2"], "beat": ["v2"]}


def test_parse_strategy_defaults_to_rolling():
    services = config.parse_services({"services": {"web": {"labels": {"deploy.role": "app"}}}})
    assert services[0].strategy == "rolling"
    assert not services[0].drains_first
