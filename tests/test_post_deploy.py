"""deploy.post-deploy: a command run once in a service's new container after a deploy."""

import signal

import pytest

from flow_deploy import config
from flow_deploy.deploy import deploy
from tests.fake_host import COMPOSE_CMD, FakeHost
from tests.helpers import app, chdir, record, stack

SETUP = "python manage.py setup_pages"


def hooked(order=100, hook=SETUP, **kwargs):
    svc = app(order, **kwargs)
    svc["labels"]["deploy.post-deploy"] = hook
    return svc


@pytest.fixture
def host(monkeypatch, tmp_path):
    """web (hooked) then worker on v1."""
    chdir(monkeypatch, tmp_path)
    compose = stack(web=hooked(100), worker=app(200))
    return FakeHost(compose, running={"web": ["v1"], "worker": ["v1"]}).install(monkeypatch)


def test_parse_post_deploy():
    compose = stack(
        web=hooked(100), worker=app(200), blank=hooked(300, hook=""), env=hooked(400, hook="$$X")
    )
    hooks = {s.name: s.post_deploy for s in config.parse_services(compose)}
    # $$ is how compose config prints a literal $.
    assert hooks == {"web": SETUP, "worker": None, "blank": None, "env": "$X"}


def test_hook_runs_once_in_the_service_after_every_service_is_healthy(host, tmp_path, capsys):
    host.exec_output = "Setting up CMS pages...\nDone.\n"
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 0
    hook = f"exec -T web sh -c {SETUP}"
    assert [c for c in host.commands() if "exec" in c] == [f"docker compose {hook}"]
    assert host.index(hook) > host.index("--scale worker=1 worker")
    assert host.envs[host.index(hook)] == {"DEPLOY_TAG": "v2"}
    out = capsys.readouterr().out
    assert "running: python manage.py setup_pages" in out
    assert "    Setting up CMS pages..." in out
    assert "✓ web post-deploy done" in out
    assert record(tmp_path)["tag"] == "v2"


def test_hooks_run_in_deploy_order(monkeypatch, tmp_path):
    chdir(monkeypatch, tmp_path)
    compose = stack(worker=hooked(200, hook="warm"), web=hooked(100, hook="seed"))
    host = FakeHost(compose, running={"web": ["v1"], "worker": ["v1"]}).install(monkeypatch)
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 0
    assert host.index("exec -T web sh -c seed") < host.index("exec -T worker sh -c warm")


def test_failing_hook_warns_and_the_deploy_still_succeeds(host, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    host.fail("exec -T web")
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 0
    assert host.running() == {"web": ["v2"], "worker": ["v2"]}
    captured = capsys.readouterr()
    assert "::warning::web's post-deploy hook exited 1; the deploy itself succeeded" in captured.out
    assert "injected failure" in captured.out


def test_interrupted_hook_warns_without_reverting(host, capsys):
    host.before("exec -T web", lambda: signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None))
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 0
    assert host.running() == {"web": ["v2"], "worker": ["v2"]}
    assert host.sha == "v2"
    assert "post-deploy hooks interrupted by signal 15" in capsys.readouterr().err


def test_hooks_do_not_run_when_the_deploy_fails(host):
    host.unhealthy_services.add(("worker", "v2"))
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1
    assert not any("exec" in c for c in host.commands())


def test_service_filter_runs_only_selected_services_hooks(host):
    assert deploy(tag="v2", services_filter=["worker"], cmd=COMPOSE_CMD) == 0
    assert not any("exec" in c for c in host.commands())


def test_dry_run_shows_the_hook(host, capsys):
    assert deploy(tag="v2", dry_run=True, cmd=COMPOSE_CMD) == 0
    assert "would run: python manage.py setup_pages" in capsys.readouterr().out
