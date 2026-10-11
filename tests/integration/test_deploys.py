"""flow-deploy against real containers: drain-first, rolling, and every revert path."""

import os
import time

import pytest

from tests.integration.conftest import SIGNALS, service, standard

ALL_V1 = {"web": ["v1"], "scheduler": ["v1"], "worker": ["v1"]}


def assert_reverted(stack, expected_running, code, out, expected_code=1):
    assert code == expected_code, out
    assert stack.running() == expected_running, out
    assert stack.head() == stack.sha("v1"), "repo not restored to the previous commit"
    assert not stack.lock_exists(), "lock left behind"


def test_happy_path(stack):
    stack.commit("v1", standard())
    stack.commit("v2", standard())
    stack.up("v1", scale={"worker": 2})

    result = stack.deploy("v2")

    assert result.returncode == 0, result.stdout + result.stderr
    # The worker keeps its replica count.
    assert stack.running() == {"web": ["v2"], "scheduler": ["v2"], "worker": ["v2", "v2"]}
    assert stack.head() == stack.sha("v2")
    assert not stack.lock_exists()
    out = result.stdout
    # Drain in reverse deploy.order, roll, then start in deploy.order.
    order = [
        out.index("▸ worker"),
        out.index("▸ scheduler"),
        out.index("▸ web"),
        out.rindex("▸ scheduler"),
        out.rindex("▸ worker"),
    ]
    assert order == sorted(order)


def test_first_deploy_of_a_new_drain_first_service(stack):
    stack.commit("v1", standard(scheduler=None))
    stack.commit("v2", standard())
    stack.up("v1")
    assert "scheduler" not in stack.running()

    result = stack.deploy("v2")

    assert result.returncode == 0, result.stdout + result.stderr
    assert stack.running() == {"web": ["v2"], "scheduler": ["v2"], "worker": ["v2"]}


def test_rolling_service_fails_its_healthcheck(stack):
    """Drained services come back on v1 at their replica count, and web keeps
    its old container. (On master, scale-back recreated web's old container
    on the failing image.)"""
    stack.commit("v1", standard())
    stack.commit("unhealthy", standard())
    stack.up("v1", scale={"worker": 2})

    result = stack.deploy("unhealthy")

    assert_reverted(
        stack,
        {"web": ["v1"], "scheduler": ["v1"], "worker": ["v1", "v1"]},
        result.returncode,
        result.stdout,
    )


def test_rolling_only_project_keeps_its_old_container_on_a_failed_healthcheck(stack):
    """No drain-first anywhere: the fix to scale-back applies to every project."""
    stack.commit("v1", standard(scheduler=None, worker=None))
    stack.commit("unhealthy", standard(scheduler=None, worker=None))
    stack.up("v1")
    old = stack.container_ids("web")

    result = stack.deploy("unhealthy")

    assert_reverted(stack, {"web": ["v1"]}, result.returncode, result.stdout)
    assert stack.container_ids("web") == old, "web's old container was replaced"


def test_drain_first_service_fails_its_healthcheck_in_phase_3(stack):
    """The failed worker goes back to v1 at its replica count. Web and the
    scheduler, healthy on v3, stay: shipped services aren't rolled back."""
    stack.commit("v1", standard())
    bad_worker = service(300, "drain-first", env={"STUB_HEALTH": "fail"}, health_timeout=20)
    stack.commit("v3", standard(worker=bad_worker))
    stack.up("v1", scale={"worker": 2})

    result = stack.deploy("v3")

    assert_reverted(
        stack,
        {"web": ["v3"], "scheduler": ["v3"], "worker": ["v1", "v1"]},
        result.returncode,
        result.stdout,
    )
    assert "worker failed to start" in result.stdout


def test_bad_tag_fails_at_pull_before_anything_stops(stack):
    stack.commit("v1", standard())
    stack.commit("bad", standard())  # no such image in the registry
    stack.up("v1")
    before = {svc: stack.container_ids(svc) for svc in ("web", "scheduler", "worker")}

    result = stack.deploy("bad")

    assert_reverted(stack, ALL_V1, result.returncode, result.stdout)
    after = {svc: stack.container_ids(svc) for svc in ("web", "scheduler", "worker")}
    assert after == before, "a container was replaced despite the failed pull"


def test_drain_that_hits_its_timeout_is_killed_and_the_deploy_continues(stack):
    stubborn = service(300, "drain-first", drain=3, env={"STUB_IGNORE_SIGTERM": "1"})
    stack.commit("v1", standard(worker=stubborn))
    stack.commit("v2", standard(worker=stubborn))
    stack.up("v1")

    started = time.time()
    result = stack.deploy("v2")

    assert result.returncode == 0, result.stdout + result.stderr
    assert stack.running()["worker"] == ["v2"]
    assert "(3s timeout)" in result.stdout
    assert time.time() - started >= 3


@pytest.mark.parametrize(
    ("signame", "to_group"),
    [("SIGINT", False), ("SIGTERM", False), ("SIGINT", True)],
    ids=["SIGINT", "SIGTERM", "SIGINT-to-process-group"],
)
def test_signal_mid_drain_reverts(stack, signame, to_group):
    """to_group sends the signal to flow-deploy's whole process group, as a
    terminal's Ctrl-C does, so the docker stop child would get it too if it
    weren't in its own session."""
    slow = service(300, "drain-first", drain=30, env={"STUB_SIGTERM_DELAY": "10"})
    stack.commit("v1", standard(worker=slow))
    stack.commit("v2", standard(worker=slow))
    stack.up("v1")

    running = stack.start_deploy("v2")
    running.wait_for("draining")
    time.sleep(1)
    if to_group:
        os.killpg(running.pid, SIGNALS[signame])
    else:
        os.kill(running.pid, SIGNALS[signame])
    code, out = running.finish()

    assert "finishing the drain" in out
    # Outlast the old worker's SIGTERM delay: a restore that only "saw" a
    # still-stopping container would leave nothing running once it exits.
    time.sleep(12)
    assert_reverted(stack, ALL_V1, code, out)


def test_lock_held_by_a_live_process_exits_2_and_changes_nothing(stack):
    stack.commit("v1", standard())
    stack.commit("v2", standard())
    stack.up("v1")
    lock = stack.repo / ".git" / "deploy-lock"
    lock.write_text(f'{{"pid": {os.getpid()}, "timestamp": 0}}')
    before = {svc: stack.container_ids(svc) for svc in ("web", "scheduler", "worker")}

    result = stack.deploy("v2")

    assert result.returncode == 2
    assert "Deploy lock held by PID" in result.stderr
    assert stack.head() == stack.sha("v1")
    assert {svc: stack.container_ids(svc) for svc in ("web", "scheduler", "worker")} == before
    assert lock.exists(), "another process's lock was released"


def test_old_image_without_a_tag_exits_3(stack):
    """A worker started from a digest-pinned image can't be restored by tag."""
    pinned_worker = service(
        300, "drain-first", image="${WORKER_IMAGE:-${REGISTRY}/stub:${DEPLOY_TAG}}"
    )
    stack.commit("v1", standard(worker=pinned_worker))
    stack.commit("unhealthy", standard(worker=pinned_worker))
    stack.up("v1", extra={"WORKER_IMAGE": stack.digest("v1")})

    result = stack.deploy("unhealthy")

    assert result.returncode == 3, result.stdout + result.stderr
    assert "cannot restore worker: no tag in its previous image" in result.stdout
    assert "could not be fully reverted" in result.stderr
    assert stack.running() == {"web": ["v1"], "scheduler": ["v1"]}
    assert stack.head() == stack.sha("v1")
    assert not stack.lock_exists()


def test_crash_looping_drain_first_worker_is_drained(stack):
    """A restarting container is drained too, so it can't come back on old code."""
    looping = service(300, "drain-first", env={"STUB_CRASH": "1"})
    looping["restart"] = "unless-stopped"
    stack.commit("v1", standard(worker=looping))
    stack.commit("v2", standard())
    stack.up("v1", wait=False)
    time.sleep(3)  # let it start crash-looping

    result = stack.deploy("v2")

    assert result.returncode == 0, result.stdout + result.stderr
    assert stack.state()["worker"] == [("v2", "running")], "an old worker container remains"


def test_rolling_healthcheck_that_passes_late_still_deploys(stack):
    """A slow starter needs a start_period: without one Docker marks it
    unhealthy after `retries` failed checks, and flow-deploy aborts on that."""
    stack.commit("v1", standard())
    slow_web = service(100, env={"STUB_HEALTH": "after:4"}, start_period="15s")
    stack.commit("v2", standard(web=slow_web))
    stack.up("v1")

    result = stack.deploy("v2")

    assert result.returncode == 0, result.stdout + result.stderr
    assert stack.running()["web"] == ["v2"]
