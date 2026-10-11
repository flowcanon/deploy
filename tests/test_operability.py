"""What a host operator sees: SIGKILL warnings, accessory drift, the run log,
the deploy record, health after a restore, and discovery without app env."""

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from flow_deploy import compose
from flow_deploy import deploy as deploy_mod
from flow_deploy import log
from flow_deploy.cli import main
from flow_deploy.deploy import EXIT_DEGRADED, deploy
from tests.fake_host import COMPOSE_CMD, FakeHost
from tests.helpers import DRAIN, ROLLING, app, chdir, record, stack

POSTGRES = {"image": "postgres:16", "labels": {"deploy.role": "accessory"}}
WITH_DB = stack(web=app(100), worker=app(200), postgres=POSTGRES)


@pytest.fixture
def host(monkeypatch, tmp_path):
    chdir(monkeypatch, tmp_path)
    monkeypatch.setattr(compose, "resolve_command", lambda: list(COMPOSE_CMD))
    running = {"web": ["v1"], "worker": ["v1"], "postgres": ["16"]}
    return FakeHost(WITH_DB, running=running).install(monkeypatch)


# ---------------------------------------------------------------------------
# 2. A container that had to be killed is reported
# ---------------------------------------------------------------------------


def test_a_drain_that_had_to_sigkill_warns(monkeypatch, tmp_path, capsys):
    chdir(monkeypatch, tmp_path)
    host = FakeHost(DRAIN, running={"web": ["v1"], "worker": ["v1"]}).install(monkeypatch)
    host.ignores_sigterm.add("worker")
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 0  # a warning, not a failure
    err = capsys.readouterr().err
    assert "worker (worker0" in err and "didn't exit on SIGTERM within 3660s" in err
    assert "exec-form CMD" in err


def test_a_rolling_cutover_that_had_to_sigkill_warns(host, capsys):
    host.ignores_sigterm.add("web")
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 0
    err = capsys.readouterr().err
    assert "web (web001) didn't exit on SIGTERM within 30s and was killed" in err
    assert "worker (" not in err


def test_a_stop_that_took_the_whole_timeout_warns(host, monkeypatch, capsys):
    """Exit code aside, using up the whole drain means it was killed."""
    clock = iter(range(0, 10_000, 40))
    monkeypatch.setattr(deploy_mod.time, "time", lambda: next(clock))
    assert deploy(tag="v2", services_filter=["web"], cmd=COMPOSE_CMD) == 0
    assert "web (web001) didn't exit on SIGTERM within 30s" in capsys.readouterr().err


def test_a_clean_stop_warns_about_nothing(host, capsys):
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 0
    assert "SIGTERM" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 3. Accessory config drift is reported, never acted on
# ---------------------------------------------------------------------------


def test_accessory_config_drift_warns_and_leaves_it_alone(host, capsys):
    db = next(c for c in host.ctrs if c.service == "postgres")
    host.hashes["postgres"] = "hash-after-env-change"
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 0
    assert "accessory postgres's config changed since it started" in capsys.readouterr().err
    assert db in host.ctrs and db.state == "running"
    assert not any("postgres" in c and "up" in c for c in host.commands())


def test_unchanged_accessory_warns_about_nothing(host, capsys):
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 0
    assert "accessory" not in capsys.readouterr().err


def test_drift_check_failures_are_ignored(host, capsys):
    host.fail("--hash=")
    host.fail('{{.Label "com.docker.compose.config-hash"}}')
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 0
    assert "accessory" not in capsys.readouterr().err


def test_status_shows_accessory_drift(host):
    host.hashes["postgres"] = "changed"
    result = CliRunner().invoke(main, ["status"])
    assert "Accessory postgres: config changed since it started" in result.stdout


# ---------------------------------------------------------------------------
# 6. The record holds the deployed commit
# ---------------------------------------------------------------------------


def test_record_holds_the_deployed_commit(host, tmp_path):
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 0
    assert record(tmp_path)["sha"] == host.sha == "v2"


# ---------------------------------------------------------------------------
# 7. The host-side run log
# ---------------------------------------------------------------------------


def run_log(tmp_path):
    return (tmp_path / ".git" / "flow-deploy.log").read_text()


def test_every_deploy_is_logged_on_the_host(host, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert deploy(tag="v2", services_filter=["web"], cmd=COMPOSE_CMD) == 0
    text = run_log(tmp_path)
    assert text.startswith(f"{log.RUN_MARK}")
    assert ": deploy --tag v2 --service web =====" in text.splitlines()[0]
    assert "✓ web deployed" in text
    assert "::group::" not in text  # annotations are for CI, not the log
    assert text.rstrip().endswith("===== exit 0 =====")


def test_a_failed_deploy_logs_its_exit_code(host, tmp_path):
    host.unhealthy_services.add(("worker", "v2"))
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1
    assert run_log(tmp_path).rstrip().endswith("===== exit 1 =====")


def test_the_log_keeps_the_last_20_runs(host, tmp_path):
    old = "".join(f"{log.RUN_MARK}old{i} =====\nline\n===== exit 0 =====\n" for i in range(25))
    (tmp_path / ".git" / "flow-deploy.log").write_text(old)
    deploy(tag="v2", cmd=COMPOSE_CMD)
    text = run_log(tmp_path)
    assert text.count(log.RUN_MARK) == 20
    assert f"{log.RUN_MARK}old6 " in text and f"{log.RUN_MARK}old5 " not in text
    assert ": deploy --tag v2 =====" in text


def test_a_dry_run_isnt_logged(host, tmp_path):
    deploy(tag="v2", dry_run=True, cmd=COMPOSE_CMD)
    assert not (tmp_path / ".git" / "flow-deploy.log").exists()


def test_a_locked_out_deploy_leaves_the_log_alone(host, tmp_path):
    from flow_deploy import lock

    deploy(tag="v2", cmd=COMPOSE_CMD)
    before = run_log(tmp_path)
    lock.acquire()
    try:
        assert deploy(tag="v3", cmd=COMPOSE_CMD) == 2
    finally:
        lock.release()
    assert run_log(tmp_path) == before


def test_a_signal_mid_deploy_logs_the_reverts_exit(monkeypatch, tmp_path):
    import signal

    chdir(monkeypatch, tmp_path)
    host = FakeHost(ROLLING, running={"web": ["v1"], "worker": ["v1"]}).install(monkeypatch)
    host.before("--scale web=2", lambda: signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None))
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1
    text = run_log(tmp_path)
    assert "interrupted by signal" in text and text.rstrip().endswith("===== exit 1 =====")


def test_status_shows_the_last_deploy(host):
    deploy(tag="v2", cmd=COMPOSE_CMD)
    out = CliRunner().invoke(main, ["status"]).stdout
    assert "Last deploy: " in out and ": deploy --tag v2: exit 0 (log: .git/flow-deploy.log)" in out


def test_status_shows_a_deploy_that_never_finished(host, tmp_path):
    (tmp_path / ".git" / "flow-deploy.log").write_text(f"{log.RUN_MARK}x pid 1: deploy =====\n")
    out = CliRunner().invoke(main, ["status"]).stdout
    assert "Last deploy: x pid 1: deploy: no exit recorded (running, or killed)" in out


def test_no_git_dir_means_no_log(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    log.open_run("deploy")
    log.info("hello")
    log.close_run(0)
    assert not (tmp_path / ".git").exists()


def test_a_log_that_cannot_be_written_is_skipped(host, tmp_path):
    (tmp_path / ".git" / "flow-deploy.log").mkdir()  # can't open a directory
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 0


def test_log_write_and_close_errors_never_raise(host, tmp_path):
    log.open_run("deploy")
    log._run_log.close()  # the file goes away under us
    log.info("still fine")
    log.close_run(0)
    assert log._run_log is None


@pytest.mark.parametrize(
    "content, expected",
    [
        ("", None),
        ("garbage\n", None),
        (f"{log.RUN_MARK}a =====\n===== exit 3 =====\n", ("a", 3)),
        (f"{log.RUN_MARK}a =====\nline\n", ("a", None)),
        (f"{log.RUN_MARK}a =====\n===== exit ?? =====\n", ("a", None)),
    ],
)
def test_last_run(tmp_path, content, expected):
    path = tmp_path / "log"
    path.write_text(content)
    assert log.last_run(str(path)) == expected


def test_last_run_without_a_file(tmp_path):
    assert log.last_run(str(tmp_path / "missing")) is None


# ---------------------------------------------------------------------------
# 8. A revert waits for what it restored to be healthy
# ---------------------------------------------------------------------------


def test_a_restored_service_that_isnt_healthy_exits_3(monkeypatch, tmp_path, capsys):
    chdir(monkeypatch, tmp_path)
    host = FakeHost(DRAIN, running={"web": ["v1"], "worker": ["v1"]}).install(monkeypatch)
    host.unhealthy_services |= {("web", "v2"), ("worker", "v1")}
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == EXIT_DEGRADED
    out = capsys.readouterr().out
    assert "waiting for restored worker to be healthy" in out
    assert "restored worker but it isn't healthy" in out


def test_a_healthy_restore_still_exits_1(monkeypatch, tmp_path, capsys):
    chdir(monkeypatch, tmp_path)
    host = FakeHost(DRAIN, running={"web": ["v1"], "worker": ["v1"]}).install(monkeypatch)
    host.unhealthy_services.add(("web", "v2"))
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1
    assert "waiting for restored worker to be healthy" in capsys.readouterr().out


def test_a_restored_service_that_skips_healthchecks_isnt_waited_on(monkeypatch, tmp_path):
    chdir(monkeypatch, tmp_path)
    beat = app(200, "drain-first", skip=True, healthcheck=False)
    host = FakeHost(stack(web=app(), beat=beat), running={"web": ["v1"], "beat": ["v1"]})
    host.install(monkeypatch)
    host.unhealthy_services |= {("web", "v2"), ("beat", "v1")}
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1


def test_restore_health_check_that_cannot_list_containers(monkeypatch):
    svc = deploy_mod.config.parse_services({"services": {"worker": app()}})[0]
    monkeypatch.setattr(deploy_mod.containers, "list_containers_for_service", lambda *a, **k: None)
    assert deploy_mod._restored_healthy(svc, "proj", 1) is False


def test_a_restored_container_that_exits_straight_away_exits_3(monkeypatch, capsys):
    """compose up succeeded, but nothing stayed up: not restored."""
    svc = deploy_mod.config.parse_services({"services": {"worker": app(skip=True)}})[0]
    monkeypatch.setattr(deploy_mod.containers, "list_containers_for_service", lambda *a, **k: [])
    assert deploy_mod._restored_healthy(svc, "proj", 2) is False
    assert "restored worker but only 0 of 2 containers are up" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 1. Discovery doesn't need the app's env
# ---------------------------------------------------------------------------

UNSET = (
    'compose config failed: level=warning msg="The \\"DOCKER_IMAGE\\" variable is not set. '
    'Defaulting to a blank string."\nservice "worker" has neither an image nor a build context'
)
DISCOVERED = {
    "x-deploy": {"host": "h1", "user": "deploy", "dir": "/srv"},
    "services": {"worker": {"image": "$DOCKER_IMAGE", "labels": {"deploy.role": "app"}}},
}


@patch("flow_deploy.compose.compose_config")
def test_discovery_falls_back_to_the_uninterpolated_config(mock_config, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    mock_config.side_effect = [RuntimeError(UNSET), DISCOVERED]  # as compose escapes it
    result = CliRunner().invoke(main, ["config"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)[0]["host"] == "h1"  # stdout stays pure JSON
    assert "::warning::" in result.stderr and "(unset: DOCKER_IMAGE)" in result.stderr
    assert mock_config.call_args_list[1].kwargs == {
        "cmd": None,
        "env": {"DEPLOY_TAG": "flow-deploy-lookup"},
        "no_interpolate": True,
    }


@patch("flow_deploy.compose.compose_config")
def test_discovery_warning_without_named_vars(mock_config):
    mock_config.side_effect = [RuntimeError("compose config failed: other"), DISCOVERED]
    result = CliRunner().invoke(main, ["config"])
    assert result.exit_code == 0
    assert "compose config failed with this environment; discovered" in result.stderr


@patch("flow_deploy.compose.compose_config")
def test_discovery_fails_if_even_uninterpolated_config_fails(mock_config):
    mock_config.side_effect = [RuntimeError("compose config failed: bad"), RuntimeError("worse")]
    result = CliRunner().invoke(main, ["config"])
    assert result.exit_code == 1
    assert "compose config failed: bad" in result.stderr
