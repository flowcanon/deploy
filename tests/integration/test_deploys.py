"""flow-deploy against real containers: drain-first, rolling, and every revert path."""

import os
import time

import pytest

from tests.integration.conftest import SIGNALS, service, standard

ALL_V1 = {"web": ["v1"], "scheduler": ["v1"], "worker": ["v1"]}
SERVICES = ("web", "scheduler", "worker")


def assert_reverted(stack, expected_running, code, out, expected_code=1, previous="v1"):
    assert code == expected_code, out
    assert stack.running() == expected_running, out
    assert stack.head() == stack.sha(previous), "repo not restored to the previous commit"
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


BAD_WORKER = service(300, "drain-first", env={"STUB_HEALTH": "fail"}, health_timeout=20)


def test_drain_first_service_fails_its_healthcheck_in_phase_3(stack):
    """Everything goes back to v1: web (shipped) and the scheduler (started
    healthy) too, and the worker at its replica count."""
    stack.commit("v1", standard())
    stack.commit("v3", standard(worker=BAD_WORKER))
    stack.up("v1", scale={"worker": 2})

    result = stack.deploy("v3")

    assert_reverted(
        stack,
        {"web": ["v1"], "scheduler": ["v1"], "worker": ["v1", "v1"]},
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


def test_digest_pinned_worker_is_restored_to_the_same_image(stack):
    """A worker started from a digest (no tag at all) is restored by image ID."""
    pinned_worker = service(
        300, "drain-first", image="${WORKER_IMAGE:-${REGISTRY}/stub:${DEPLOY_TAG:-latest}}"
    )
    stack.commit("v1", standard(worker=pinned_worker))
    stack.commit("unhealthy", standard(worker=pinned_worker))
    stack.up("v1", extra={"WORKER_IMAGE": stack.digest("v1")})

    result = stack.deploy("unhealthy")

    assert_reverted(stack, ALL_V1, result.returncode, result.stdout)
    assert stack.image_refs("worker")[0].split(":")[-1].startswith("flow-deploy-")


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


# ---------------------------------------------------------------------------
# Image tags: versions, shas, restores, :latest, and manual compose
# ---------------------------------------------------------------------------


def test_annotated_version_tag_deploy(stack):
    """Releases deploy by version tag; an annotated tag checks out its commit."""
    stack.commit("v1", standard())
    stack.commit("v2", standard(), annotated=True)
    stack.up("v1")

    result = stack.deploy("v2")

    assert result.returncode == 0, result.stdout + result.stderr
    assert stack.running() == {"web": ["v2"], "scheduler": ["v2"], "worker": ["v2"]}
    assert stack.head() == stack.sha("v2")
    assert stack.image_refs("web") == [f"{stack.registry}/stub:v2"]
    assert stack.record()["tag"] == "v2"
    assert stack.flow("tag").stdout == "v2\n"


def test_sha_deploy(stack):
    """A commit SHA still works as the tag: CI pushes the image under it."""
    stack.commit("v1", standard())
    sha = stack.commit(None, standard())
    stack.push_image("v2", sha)
    stack.up("v1")

    result = stack.deploy(sha)

    assert result.returncode == 0, result.stdout + result.stderr
    assert stack.running() == {"web": ["v2"], "scheduler": ["v2"], "worker": ["v2"]}
    assert stack.head() == sha
    assert stack.flow("tag").stdout == f"{sha}\n"


def test_failed_deploy_restores_the_version_that_was_running(stack):
    """v1 -> v2 succeeds; v3 then fails in phase 3 and everything returns to
    v2 (not v1, not :latest), shipped web included."""
    stack.commit("v1", standard())
    stack.commit("v2", standard())
    stack.commit("v3", standard(worker=BAD_WORKER))
    stack.up("v1")
    assert stack.deploy("v2").returncode == 0

    result = stack.deploy("v3")

    assert_reverted(
        stack,
        {"web": ["v2"], "scheduler": ["v2"], "worker": ["v2"]},
        result.returncode,
        result.stdout,
        previous="v2",
    )
    # Restored by an alias of the exact image; the record follows it. The
    # worker kept its v2 container, web was recreated on the alias: one image.
    tag = stack.record()["tag"]
    assert tag.startswith("flow-deploy-")
    assert stack.flow("tag").stdout == f"{tag}\n"
    assert stack.flow("compose", "up", "-d", "--wait").returncode == 0
    assert stack.running() == {"web": ["v2"], "scheduler": ["v2"], "worker": ["v2"]}


def test_first_deploy_from_latest_restores_that_image_after_latest_moves(stack):
    """A host brought up by hand runs :latest. If :latest is re-pushed and
    pulled before a failed deploy, the revert still restores the image that
    was running, not what :latest names now."""
    latest = f"latest-{os.getpid()}-{int(time.time())}"  # don't move the shared :latest
    stack.push_image("v1", latest)
    image = f"${{REGISTRY}}/stub:${{DEPLOY_TAG:-{latest}}}"
    by_hand = standard(**{s: {**standard()["services"][s], "image": image} for s in SERVICES})
    stack.commit("v1", by_hand)
    bad = {**BAD_WORKER, "image": image}
    stack.commit("v3", {"services": {**by_hand["services"], "worker": bad}})
    stack.up(None, ref="v1")
    assert stack.image_refs("web") == [f"{stack.registry}/stub:{latest}"]
    stack.push_image("v2", latest)
    assert stack.compose("pull", "web").returncode == 0  # the host's tag now names v2

    result = stack.deploy("v3")

    assert_reverted(stack, ALL_V1, result.returncode, result.stdout)


def test_manual_compose_keeps_the_deployed_version(stack):
    """`flow-deploy compose up -d` on the host changes nothing after a deploy;
    plain compose without DEPLOY_TAG would move every service to :latest."""
    stack.commit("v1", standard())
    stack.commit("v2", standard())
    stack.up("v1")
    assert stack.deploy("v2").returncode == 0
    before = {svc: stack.container_ids(svc) for svc in SERVICES}

    result = stack.flow("compose", "up", "-d", "--wait")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "DEPLOY_TAG=v2 (from running containers)" in result.stderr
    assert {svc: stack.container_ids(svc) for svc in SERVICES} == before
    # The hazard it prevents: compose's ${DEPLOY_TAG:-latest} default.
    images = stack.compose("config", "--images").stdout.split()
    assert images == [f"{stack.registry}/stub:latest"] * 3


def test_manual_compose_refuses_when_nothing_tells_the_tag(stack):
    stack.commit("v1", standard())
    stack.git("checkout", "-q", "--detach", "v1")

    result = stack.flow("compose", "up", "-d")

    assert result.returncode == 1
    assert "nothing is running and no deploy is recorded" in result.stderr
    assert stack.running() == {}


def test_moved_tag_deploys_its_new_commit_and_image(stack):
    """A re-pushed release tag is fetched (--force) and its image re-pulled."""
    tag = f"moved-{os.getpid()}-{int(time.time())}"
    stack.commit("v1", standard())
    first = stack.commit(tag, standard())
    stack.push_image("v1", tag)
    stack.up("v1")
    assert stack.deploy(tag).returncode == 0
    # Re-release: the tag moves to a new commit and a new image.
    second = stack.commit(tag, standard(), force=True)
    stack.push_image("v2", tag)
    assert first != second

    result = stack.deploy(tag)

    assert result.returncode == 0, result.stdout + result.stderr
    assert stack.head() == second
    assert stack.running() == {"web": ["v2"], "scheduler": ["v2"], "worker": ["v2"]}


def test_containers_of_another_project_name_here_are_refused(stack):
    """A release that renamed the compose project would start a second stack
    beside the first and have nothing to revert to: refused, nothing changed."""
    stack.commit("v1", standard())
    stack.commit("v2", standard())
    stack.up("v1")
    other = f"{stack.project}old"
    stack.compose("-p", other, "up", "-d", "--wait", "web", tag="v1")
    try:
        before = {svc: stack.container_ids(svc) for svc in SERVICES}

        result = stack.deploy("v2")

        assert result.returncode == 1, result.stdout + result.stderr
        assert f"compose project(s) {other} are running" in result.stderr
        assert {svc: stack.container_ids(svc) for svc in SERVICES} == before
        assert stack.head() == stack.sha("v1")
    finally:
        stack.compose("-p", other, "down", "--timeout", "1", check=False)
