"""Rolling deploys on a FakeHost: success, failures, rollback, and the tag record."""

import pytest

from flow_deploy import lock
from flow_deploy.deploy import deploy
from tests.fake_host import COMPOSE_CMD, FakeHost
from tests.helpers import ROLLING, app, chdir, record, stack


@pytest.fixture
def host(monkeypatch, tmp_path):
    """A rolling-only host with web and worker on v1."""
    chdir(monkeypatch, tmp_path)
    return FakeHost(ROLLING, running={"web": ["v1"], "worker": ["v1"]}).install(monkeypatch)


def test_deploy_happy_path(host, tmp_path):
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 0
    assert host.running() == {"web": ["v2"], "worker": ["v2"]}
    assert host.sha == "v2"
    assert record(tmp_path)["tag"] == "v2"
    assert not (tmp_path / ".git" / "deploy-lock").exists()


def test_deploy_order_follows_deploy_order(host, capsys):
    deploy(tag="v2", cmd=COMPOSE_CMD)
    out = capsys.readouterr().out
    assert out.index("▸ web") < out.index("▸ worker")


def test_deploy_service_filter_leaves_others_and_clears_the_record(host, tmp_path, capsys):
    (tmp_path / ".git" / "deploy-tag").write_text('{"tag": "v1"}')
    assert deploy(tag="v2", services_filter=["web"], cmd=COMPOSE_CMD) == 0
    assert host.running() == {"web": ["v2"], "worker": ["v1"]}
    # Services now run different tags: no single tag describes the host.
    assert record(tmp_path) is None
    assert "manual compose commands on this host need DEPLOY_TAG" in capsys.readouterr().err


def test_deploy_dry_run_changes_nothing(host, tmp_path, capsys):
    assert deploy(tag="v2", dry_run=True, cmd=COMPOSE_CMD) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out and "web" in out and "worker" in out
    assert host.running() == {"web": ["v1"], "worker": ["v1"]}
    assert not (tmp_path / ".git" / "deploy-lock").exists()


def test_deploy_health_check_failure_keeps_the_old_container(host):
    before = [c.id for c in host.ctrs if c.service == "web"]
    host.unhealthy_services.add(("web", "v2"))
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1
    assert host.running() == {"web": ["v1"], "worker": ["v1"]}
    assert [c.id for c in host.ctrs if c.service == "web"] == before
    assert host.sha == "prev123abc"


def test_deploy_pull_failure_changes_nothing(host):
    host.unpullable.add("v2")
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1
    assert host.running() == {"web": ["v1"], "worker": ["v1"]}


def test_deploy_lock_held(host):
    lock.acquire()
    try:
        assert deploy(tag="v2", cmd=COMPOSE_CMD) == 2
    finally:
        lock.release()
    assert host.calls == []


def test_deploy_missing_healthcheck(monkeypatch, tmp_path):
    chdir(monkeypatch, tmp_path)
    FakeHost(stack(web=app(healthcheck=False))).install(monkeypatch)
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1


def test_deploy_no_app_services(monkeypatch, tmp_path):
    chdir(monkeypatch, tmp_path)
    FakeHost(stack(redis={"image": "redis:7"})).install(monkeypatch)
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1


def test_deploy_compose_config_failure(host):
    host.fail("docker compose config")
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1
    assert host.sha == "prev123abc"


def test_deploy_container_count_mismatch_scales_back(host):
    host.stuck.add("web")
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1
    assert host.running() == {"web": ["v1"], "worker": ["v1"]}


def test_deploy_restore_failure_exits_degraded_and_releases_lock(host, tmp_path, capsys):
    """A failed git restore exits 3 but still releases the lock: no manual cleanup."""
    host.unpullable.add("v2")
    host.fail("git checkout --detach prev123abc")
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 3
    assert not (tmp_path / ".git" / "deploy-lock").exists()
    assert "could not be fully reverted (git restore failed)" in capsys.readouterr().err


def test_deploy_dirty_tree_fails(host, capsys):
    host.dirty = True
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1
    assert "dirty" in capsys.readouterr().err


def test_deploy_healthcheck_skip(monkeypatch, tmp_path):
    chdir(monkeypatch, tmp_path)
    host = FakeHost(stack(beat=app(skip=True, healthcheck=False)), running={"beat": ["v1"]})
    host.install(monkeypatch)
    host.unhealthy.add("v2")  # never consulted
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 0
    assert host.running() == {"beat": ["v2"]}


def test_deploy_no_tag_uses_head(host):
    """Without --tag, deploy the current HEAD: no checkout, its SHA as the tag."""
    assert deploy(cmd=COMPOSE_CMD) == 0
    assert host.running() == {"web": ["prev123abc"], "worker": ["prev123abc"]}
    assert not any("checkout" in c for c in host.commands())


def test_deploy_no_tag_dry_run(host, capsys):
    assert deploy(dry_run=True, cmd=COMPOSE_CMD) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out and "prev123abc" in out


def test_fetches_every_tag_and_updates_moved_ones(host):
    deploy(tag="v2", cmd=COMPOSE_CMD)
    assert "git fetch --tags --force origin" in host.commands()


def test_config_is_rendered_with_the_deploy_tag(host):
    deploy(tag="v2", cmd=COMPOSE_CMD)
    config_call = host.calls.index(["docker", "compose", "config"])
    assert host.envs[config_call] == {"DEPLOY_TAG": "v2"}


# ---------------------------------------------------------------------------
# A failure reverts everything, shipped rolling services included
# ---------------------------------------------------------------------------


def test_second_service_failing_rolls_the_first_back(host, tmp_path):
    host.unhealthy_services.add(("worker", "v2"))
    before = host.running_images()

    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1

    # web had shipped v2; it's rolled back by the release tag it ran.
    assert host.running_images() == before
    assert host.running() == {"web": ["v1"], "worker": ["v1"]}
    assert host.sha == "prev123abc"
    scale_ups = [i for i, c in enumerate(host.commands()) if "--scale web=2 web" in c]
    assert [host.envs[i] for i in scale_ups] == [{"DEPLOY_TAG": "v2"}, {"DEPLOY_TAG": "v1"}]
    assert host.commands().count("docker compose pull web") == 1
    assert record(tmp_path)["tag"] == "v1"


def test_rollback_failure_exits_3(host, capsys):
    host.unhealthy_services.add(("worker", "v2"))
    host.fail("docker compose up -d --no-deps --no-recreate --scale web=2 web", skip=1)
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 3
    assert "web could not be rolled back" in capsys.readouterr().err


def test_no_local_tags_are_created(host):
    """Restores go by git tags and SHAs: nothing is tagged on the host."""
    host.unhealthy_services.add(("worker", "v2"))
    deploy(tag="v2", cmd=COMPOSE_CMD)
    assert not any(c.startswith("docker tag") for c in host.commands())


def test_rollback_after_a_failed_cutover_cleanup_keeps_the_previous_container(host):
    """web's old container survived its cutover (stop failed); when worker then
    fails, the rollback removes web's new container and keeps the old one."""
    old = next(c.id for c in host.ctrs if c.service == "web")
    host.fail("docker stop --time 30 web001", times=1)
    host.unhealthy_services.add(("worker", "v2"))
    before = host.running_images()

    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1

    assert host.running_images() == before
    assert [c.id for c in host.ctrs if c.service == "web" and c.state == "running"] == [old]


def test_rollback_removes_a_stopped_leftover_first(host):
    """web's cutover stopped its old container but couldn't remove it. compose
    would restart that leftover during the rollback; it's removed instead."""
    host.fail("docker rm web001", times=1)
    host.unhealthy_services.add(("worker", "v2"))
    before = host.running_images()

    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1

    assert host.running_images() == before
    assert not any(c.id == "web001" for c in host.ctrs)
    assert not any(c.state == "exited" for c in host.ctrs)


def test_rollback_that_cannot_list_stopped_containers_exits_3(host, capsys):
    host.unhealthy_services.add(("worker", "v2"))
    host.fail("--filter status=created")
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 3
    assert "its stopped containers can't be inspected" in capsys.readouterr().out


def test_rollback_that_cannot_remove_a_stopped_leftover_exits_3(host, capsys):
    host.fail("docker rm web001")  # the cutover's, and the rollback's retry
    host.unhealthy_services.add(("worker", "v2"))
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 3
    assert "could not remove stopped web container web001" in capsys.readouterr().out


def test_failed_abort_leaves_no_stopped_container_for_the_next_deploy(host):
    """web's new container failed its health check and was stopped, but not
    removed. The next deploy's `compose up --scale` would restart it, so the
    revert removes it; the deploy still exits 3 (its own cleanup failed)."""
    host.unhealthy_services.add(("web", "v2"))
    host.fail("docker rm web003", times=1)
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 3
    assert not any(c.state == "exited" for c in host.ctrs)
    assert host.running() == {"web": ["v1"], "worker": ["v1"]}

    host.unhealthy_services.clear()
    assert deploy(tag="v3", cmd=COMPOSE_CMD) == 0
    assert host.running() == {"web": ["v3"], "worker": ["v3"]}


def test_stopped_new_container_that_cannot_be_removed_on_revert_exits_3(host, capsys):
    host.unhealthy_services.add(("web", "v2"))
    host.fail("docker rm web003")
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 3
    assert "web's stopped new container could not be removed" in capsys.readouterr().err


def test_rollback_that_cannot_inspect_exits_3(host, capsys):
    host.unhealthy_services.add(("worker", "v2"))
    host.fail("docker inspect --format {{.Id}} {{.Image}} web", skip=1)  # past the snapshot
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 3
    assert "cannot roll web back: its containers can't be inspected" in capsys.readouterr().out


def test_renamed_compose_project_is_refused(host, capsys):
    """The running stack belongs to another project name: deploying would
    start a second stack beside it, and snapshot nothing to revert to."""
    host.other_projects = ["oldname"]
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1
    assert "compose project(s) oldname run this deploy's services" in capsys.readouterr().err
    assert not any("compose pull" in c for c in host.commands())
    assert host.sha == "prev123abc"


def test_project_check_failure_changes_nothing(host, capsys):
    host.fail("label=com.docker.compose.project.working_dir=")
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1
    assert "could not list this directory's containers" in capsys.readouterr().err
    assert host.running() == {"web": ["v1"], "worker": ["v1"]}


def test_service_with_no_previous_container_is_removed_on_revert(monkeypatch, tmp_path):
    chdir(monkeypatch, tmp_path)
    host = FakeHost(ROLLING, running={}).install(monkeypatch)  # nothing running yet
    host.unhealthy_services.add(("worker", "v2"))
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1
    assert host.running() == {}
    assert record(tmp_path) is None


def test_first_deploy_onto_an_empty_host(monkeypatch, tmp_path):
    chdir(monkeypatch, tmp_path)
    host = FakeHost(stack(web=app()), running={}).install(monkeypatch)
    host.fail("docker compose up", times=0)  # never fires
    # Rolling with no old container: compose brings up two, one is cut over.
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 0
    assert host.running() == {"web": ["v2"]}


# ---------------------------------------------------------------------------
# What's running is read from the containers, whatever they're tagged
# ---------------------------------------------------------------------------


def test_revert_from_latest_restores_by_the_previous_commit(monkeypatch, tmp_path):
    """:latest isn't a git ref (and moves): the revert uses the SHA that was
    checked out, which CI pushes every image under."""
    chdir(monkeypatch, tmp_path)
    host = FakeHost(ROLLING, running={"web": ["latest"], "worker": ["latest"]})
    host.install(monkeypatch)
    host.registry["prev123abc"] = host.images["latest"]  # CI pushed it by SHA too
    before = host.running_images()
    host.images["latest"] = "sha256:" + "f" * 64  # something re-pulled :latest
    host.unhealthy_services.add(("worker", "v2"))

    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1
    assert host.running_images() == before
    assert host.running()["web"] == ["prev123abc"]


def test_digest_pinned_container_is_restored_by_the_previous_commit(monkeypatch, tmp_path):
    chdir(monkeypatch, tmp_path)
    host = FakeHost(ROLLING, running={"worker": ["v1"]}).install(monkeypatch)
    host.add("web", "v1", ref="ghcr.io/myorg/myapp@sha256:abc")
    host.registry["prev123abc"] = host.images["v1"]
    before = host.running_images()
    host.unhealthy_services.add(("worker", "v2"))
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1
    assert host.running_images() == before


def test_a_moved_release_tag_restores_by_the_previous_commit(host, capsys):
    """v1 now names a different image locally than the one running: trust
    the commit SHA, not the tag."""
    host.registry["prev123abc"] = host.images["v1"]
    before = host.running_images()
    host.images["v1"] = "sha256:" + "e" * 64  # v1 re-pushed and pulled
    host.unhealthy_services.add(("worker", "v2"))
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1
    assert host.running_images() == before
    assert "would restore it by the previous commit, prev123abc" in capsys.readouterr().out


def test_mixed_images_warn_and_restore_the_most_common(monkeypatch, tmp_path, capsys):
    chdir(monkeypatch, tmp_path)
    host = FakeHost(stack(web=app()), running={"web": ["v1", "v1", "v0"]}).install(monkeypatch)
    host.unhealthy_services.add(("web", "v2"))
    deploy(tag="v2", cmd=COMPOSE_CMD)
    assert "web is running 2 different images" in capsys.readouterr().err


def test_snapshot_failure_changes_nothing(host, capsys):
    host.fail("docker ps")
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1
    assert "nothing was changed" in capsys.readouterr().err
    assert host.running() == {"web": ["v1"], "worker": ["v1"]}
    assert host.sha == "prev123abc"


def test_snapshot_inspect_failure_changes_nothing(host):
    host.fail("docker inspect --format {{.Id}} {{.Image}}")
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1
    assert host.running() == {"web": ["v1"], "worker": ["v1"]}


def test_snapshot_failure_with_failed_git_restore_exits_3(host):
    host.fail("docker ps")
    host.fail("git checkout --detach prev123abc")
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 3


def test_another_project_sharing_the_directory_is_fine(host):
    """A different project in the same directory (other services) isn't a rename."""
    host.other_projects = []
    host.foreign_services = [("obs", "grafana")]
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 0


def test_tag_and_digest_ref_checks_the_tag_not_the_digest(monkeypatch, tmp_path):
    """repo:v1@sha256:... runs the digest's image even after repo:v1 moved; the
    revert renders repo:v1, so v1 only counts if it still names that image."""
    chdir(monkeypatch, tmp_path)
    host = FakeHost(stack(web=app()), running={}).install(monkeypatch)
    ctr = host.add("web", "v1", ref="ghcr.io/myorg/myapp:v1@sha256:abc")
    host.registry["prev123abc"] = ctr.image_id
    host.images["v1"] = "sha256:" + "e" * 64  # repo:v1 moved
    host.unhealthy_services.add(("web", "v2"))
    deploy(tag="v2", cmd=COMPOSE_CMD)
    assert "docker image inspect --format {{.Id}} ghcr.io/myorg/myapp:v1" in host.commands()
    assert host.running_images() == {"web": [ctr.image_id]}


def test_redeploying_the_running_tag_restores_by_the_previous_commit(host):
    """v1 was re-pushed and is deployed again: this deploy's pull moves local
    v1 to the new build, so a revert can't go by v1. It uses the SHA."""
    old = host.images["v1"]
    host.registry["prev123abc"] = old
    host.registry["v1"] = "sha256:" + "d" * 64  # the re-pushed build
    host.unhealthy_services.add(("worker", "v1"))
    assert deploy(tag="v1", cmd=COMPOSE_CMD) == 1
    assert host.running_images() == {"web": [old], "worker": [old]}
    assert host.running()["web"] == ["prev123abc"]


def test_redeploying_the_running_commit_warns_that_a_rebuild_cant_be_undone(host, capsys):
    """Deploying the SHA that's checked out: no other git name exists for the
    running build, so a revert can only restore that commit as pulled now."""
    host.sha = "v1"  # HEAD is the commit the containers run
    deploy(tag="v1", cmd=COMPOSE_CMD)
    assert "redeploying v1: if web's image under it was rebuilt" in capsys.readouterr().err


def test_container_shown_by_image_id_restores_by_the_previous_commit(monkeypatch, tmp_path):
    """docker ps shows a bare image ID once the container's tag has moved."""
    chdir(monkeypatch, tmp_path)
    host = FakeHost(stack(web=app()), running={}).install(monkeypatch)
    ctr = host.add("web", "v1")
    ctr.ref = ctr.image_id.removeprefix("sha256:")[:12]
    host.registry["prev123abc"] = ctr.image_id
    host.unhealthy_services.add(("web", "v2"))
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1
    assert host.running_images() == {"web": [ctr.image_id]}


def test_image_that_ignores_deploy_tag_warns(monkeypatch, tmp_path, capsys):
    chdir(monkeypatch, tmp_path)
    fixed = stack(web=app(image="ghcr.io/myorg/myapp"))
    FakeHost(fixed, running={"web": ["latest"]}).install(monkeypatch)
    deploy(tag="v2", cmd=COMPOSE_CMD)
    assert "web's image (ghcr.io/myorg/myapp) doesn't use DEPLOY_TAG" in capsys.readouterr().err
