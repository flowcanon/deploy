"""A rolling service with no container to roll from (just added, or crashed)."""

from flow_deploy.deploy import deploy
from tests.fake_host import COMPOSE_CMD, FakeHost
from tests.helpers import app, chdir, record, stack

# A release that adds a scheduler beside the web already running.
ADDED = stack(web=app(100), scheduler=app(200))


def host_with(monkeypatch, tmp_path, running):
    chdir(monkeypatch, tmp_path)
    return FakeHost(ADDED, running=running).install(monkeypatch)


def test_new_rolling_service_starts_on_the_new_tag(monkeypatch, tmp_path):
    host = host_with(monkeypatch, tmp_path, {"web": ["v1"]})
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 0
    assert host.running() == {"web": ["v2"], "scheduler": ["v2"]}
    assert record(tmp_path)["tag"] == "v2"
    # One container, not two new ones with no old to tell apart.
    assert not any("--scale scheduler=2" in c for c in host.commands())
    assert host.index("pull scheduler") < host.index("--scale scheduler=1 scheduler")


def test_crashed_rolling_service_comes_back_on_the_new_tag(monkeypatch, tmp_path):
    host = host_with(monkeypatch, tmp_path, {"web": ["v1"]})
    host.add("scheduler", "v1", state="exited")
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 0
    assert host.running() == {"web": ["v2"], "scheduler": ["v2"]}


def test_unhealthy_new_service_is_removed_and_the_rest_reverted(monkeypatch, tmp_path, capsys):
    host = host_with(monkeypatch, tmp_path, {"web": ["v1"]})
    host.unhealthy_services.add(("scheduler", "v2"))
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1
    assert host.running_images() == {"web": [host.images["v1"]]}
    assert host.sha == "prev123abc"
    assert "scheduler unhealthy; stopping and removing it" in capsys.readouterr().out


def test_unpullable_new_service_fails_before_starting(monkeypatch, tmp_path):
    host = host_with(monkeypatch, tmp_path, {"web": ["v1"]})
    host.fail("pull scheduler")
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1
    assert not any("--scale scheduler" in c for c in host.commands())
    assert host.running_images() == {"web": [host.images["v1"]]}
