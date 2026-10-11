"""What's deployed on this host: deployed.current(), and the tag/compose/status commands."""

import json

import pytest
from click.testing import CliRunner

from flow_deploy import compose, deployed, process
from flow_deploy.cli import main
from tests.fake_host import COMPOSE_CMD, FakeHost
from tests.helpers import ROLLING, chdir


@pytest.fixture
def host(monkeypatch, tmp_path):
    chdir(monkeypatch, tmp_path)
    monkeypatch.delenv("DEPLOY_TAG", raising=False)
    monkeypatch.setattr(compose, "resolve_command", lambda: list(COMPOSE_CMD))
    return FakeHost(ROLLING).install(monkeypatch)


@pytest.fixture
def streamed(monkeypatch):
    """What `flow-deploy compose` streams: [(args, env)]."""
    calls = []

    def run_streaming(args, env=None, cwd=None):
        calls.append((args, env))
        return 0

    monkeypatch.setattr(process, "run_streaming", run_streaming)
    return calls


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


def test_write_read_clear(host, tmp_path):
    deployed.write("v2", sha="abc")
    assert deployed.read()["tag"] == "v2"
    assert json.loads((tmp_path / ".git" / "deploy-tag").read_text())["sha"] == "abc"
    deployed.clear()
    assert deployed.read() is None
    deployed.clear()  # already gone: fine


@pytest.mark.parametrize("content", ["not json", "[]", '{"tag": ""}'])
def test_unreadable_record_is_no_record(host, tmp_path, content):
    (tmp_path / ".git" / "deploy-tag").write_text(content)
    assert deployed.read() is None


# ---------------------------------------------------------------------------
# current(): containers first, then the record, never a guess
# ---------------------------------------------------------------------------


def test_current_from_running_containers(host):
    host.add("web", "v1.4.0")
    host.add("worker", "v1.4.0")
    deployed.write("v1.3.0")  # the containers win over a stale record
    assert deployed.current() == ("v1.4.0", "running containers")


def test_current_counts_restarting_containers(host):
    host.add("web", "v1", state="restarting")
    assert deployed.current() == ("v1", "running containers")


def test_current_renders_config_with_a_placeholder_tag(host):
    """So a wrapper that defaults DEPLOY_TAG from `flow-deploy tag` can't recurse."""
    deployed.current()
    config_call = host.calls.index(["docker", "compose", "config"])
    assert host.envs[config_call] == {"DEPLOY_TAG": deployed.LOOKUP_PLACEHOLDER}


def test_current_refuses_mixed_tags(host):
    host.add("web", "v2")
    host.add("worker", "v1")
    tag, why = deployed.current()
    assert tag is None
    assert "different or untagged images" in why and "v2 (web)" in why and "v1 (worker)" in why


def test_current_after_a_revert_from_latest_names_the_sha(host):
    """A revert from :latest recreates services on the previous commit's SHA,
    while a kept container still says :latest: one image, the SHA names it."""
    web = host.add("web", "abc123sha")
    worker = host.add("worker", "latest")
    web.image_id = worker.image_id
    assert deployed.current() == ("abc123sha", "running containers (one image)")


def test_current_refuses_two_real_tags_for_one_image(host):
    """Two release tags on one image: no single answer, so no guess."""
    web = host.add("web", "v2")
    worker = host.add("worker", "v2-again")
    worker.image_id = web.image_id
    tag, why = deployed.current()
    assert tag is None and "different or untagged images" in why


def test_current_raises_when_docker_inspect_fails(host):
    host.add("web", "abc123sha")
    host.add("worker", "latest")
    host.fail("docker inspect")
    with pytest.raises(RuntimeError, match="could not inspect the app containers"):
        deployed.current()


def test_current_refuses_untagged_images(host):
    host.add("web", "v1", ref="sha256:" + "a" * 64)
    tag, why = deployed.current()
    assert tag is None and "untagged (web)" in why


def test_current_falls_back_to_the_record_when_nothing_runs(host):
    deployed.write("v3")
    assert deployed.current() == ("v3", "last deploy record")


def test_current_with_nothing_to_go_on(host):
    tag, why = deployed.current()
    assert tag is None and "nothing is running and no deploy is recorded" in why


def test_current_raises_when_docker_ps_fails(host):
    """Couldn't look is not "nothing running": no fallback to a stale record."""
    deployed.write("v3")
    host.fail("docker ps")
    with pytest.raises(RuntimeError, match="could not list web containers"):
        deployed.current()


# ---------------------------------------------------------------------------
# flow-deploy tag
# ---------------------------------------------------------------------------


def test_tag_prints_only_the_tag(host):
    host.add("web", "v2.0.1")
    result = CliRunner().invoke(main, ["tag"])
    assert result.exit_code == 0
    assert result.stdout == "v2.0.1\n"


def test_tag_unknown_exits_1(host):
    result = CliRunner().invoke(main, ["tag"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "can't tell which tag is deployed" in result.stderr


def test_tag_failure_keeps_stdout_clean_on_github_actions(host, monkeypatch):
    """No ::error:: annotation on stdout: a script would capture it as the tag."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    host.fail("docker compose config")
    result = CliRunner().invoke(main, ["tag"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "can't tell which tag is deployed" in result.stderr


def test_tag_compose_failure_exits_1(host):
    host.fail("docker compose config")
    assert CliRunner().invoke(main, ["tag"]).exit_code == 1


# ---------------------------------------------------------------------------
# flow-deploy compose
# ---------------------------------------------------------------------------


def test_compose_runs_with_the_deployed_tag(host, streamed):
    host.add("web", "v2")
    result = CliRunner().invoke(main, ["compose", "up", "-d", "--no-deps", "web"])
    assert result.exit_code == 0
    assert streamed == [(COMPOSE_CMD + ["up", "-d", "--no-deps", "web"], {"DEPLOY_TAG": "v2"})]
    assert "DEPLOY_TAG=v2 (from running containers)" in result.stderr


def test_compose_explicit_deploy_tag_wins(host, streamed, monkeypatch):
    monkeypatch.setenv("DEPLOY_TAG", "v9")
    host.add("web", "v2")
    assert CliRunner().invoke(main, ["compose", "pull"]).exit_code == 0
    assert streamed == [(COMPOSE_CMD + ["pull"], {"DEPLOY_TAG": "v9"})]
    assert host.calls == []  # no lookup


def test_compose_refuses_when_the_tag_is_unknown(host, streamed):
    host.add("web", "v2")
    host.add("worker", "v1")
    result = CliRunner().invoke(main, ["compose", "up", "-d"])
    assert result.exit_code == 1
    assert streamed == []


def test_compose_lookup_failure_exits_1(host, streamed):
    host.fail("docker compose config")
    assert CliRunner().invoke(main, ["compose", "ps"]).exit_code == 1
    assert streamed == []


def test_compose_propagates_the_exit_code(host, monkeypatch):
    host.add("web", "v2")
    monkeypatch.setattr(process, "run_streaming", lambda args, env=None, cwd=None: 17)
    assert CliRunner().invoke(main, ["compose", "logs"]).exit_code == 17


# ---------------------------------------------------------------------------
# flow-deploy status
# ---------------------------------------------------------------------------


def test_status_shows_the_deployed_tag_and_each_service(host):
    host.add("web", "v2")
    result = CliRunner().invoke(main, ["status"])
    assert result.exit_code == 0
    out = result.stdout
    assert "Current SHA: prev123" in out
    assert "Deployed tag: v2 (from running containers)" in out
    assert "web (app)  web001  ghcr.io/myorg/myapp:v2  running/healthy" in out
    assert "worker (app)  no containers" in out


def test_status_unknown_tag(host):
    result = CliRunner().invoke(main, ["status"])
    assert result.exit_code == 0
    assert "Deployed tag: unknown (nothing is running" in result.stdout
