"""Tests for cli.py — Click CLI commands."""

from unittest.mock import patch

from click.testing import CliRunner

from flow_deploy.cli import main


@patch("flow_deploy.deploy.deploy")
def test_deploy_defaults(mock_deploy):
    mock_deploy.return_value = 0
    runner = CliRunner()
    result = runner.invoke(main, ["deploy", "--tag", "abc123"])
    assert result.exit_code == 0
    mock_deploy.assert_called_once_with(tag="abc123", services_filter=None, dry_run=False)


@patch("flow_deploy.deploy.deploy")
def test_deploy_with_services(mock_deploy):
    mock_deploy.return_value = 0
    runner = CliRunner()
    result = runner.invoke(
        main, ["deploy", "--tag", "v1", "--service", "web", "--service", "worker"]
    )
    assert result.exit_code == 0
    mock_deploy.assert_called_once_with(tag="v1", services_filter=["web", "worker"], dry_run=False)


@patch("flow_deploy.deploy.deploy")
def test_deploy_dry_run(mock_deploy):
    mock_deploy.return_value = 0
    runner = CliRunner()
    result = runner.invoke(main, ["deploy", "--dry-run"])
    assert result.exit_code == 0
    mock_deploy.assert_called_once_with(tag=None, services_filter=None, dry_run=True)


@patch("flow_deploy.deploy.deploy")
def test_deploy_exit_code_propagated(mock_deploy):
    mock_deploy.return_value = 2
    runner = CliRunner()
    result = runner.invoke(main, ["deploy"])
    assert result.exit_code == 2


def test_version():
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "flow-deploy" in result.output


@patch("flow_deploy.process.run_streaming")
@patch("flow_deploy.compose.resolve_command")
def test_exec(mock_resolve, mock_streaming):
    mock_resolve.return_value = ["docker", "compose"]
    mock_streaming.return_value = 0
    runner = CliRunner()
    result = runner.invoke(main, ["exec", "web", "bash", "-c", "echo hi"])
    assert result.exit_code == 0
    mock_streaming.assert_called_once_with(
        ["docker", "compose", "exec", "web", "bash", "-c", "echo hi"]
    )


@patch("flow_deploy.process.run_streaming")
@patch("flow_deploy.compose.resolve_command")
def test_exec_no_command(mock_resolve, mock_streaming):
    mock_resolve.return_value = ["docker", "compose"]
    runner = CliRunner()
    result = runner.invoke(main, ["exec", "web"])
    assert result.exit_code == 1


@patch("flow_deploy.process.run_streaming")
@patch("flow_deploy.compose.resolve_command")
def test_logs(mock_resolve, mock_streaming):
    mock_resolve.return_value = ["docker", "compose"]
    mock_streaming.return_value = 0
    runner = CliRunner()
    result = runner.invoke(main, ["logs", "web", "--follow", "--tail", "100"])
    assert result.exit_code == 0
    mock_streaming.assert_called_once_with(
        ["docker", "compose", "logs", "--follow", "--tail", "100", "web"]
    )


@patch("flow_deploy.process.run_streaming")
@patch("flow_deploy.compose.resolve_command")
def test_logs_basic(mock_resolve, mock_streaming):
    mock_resolve.return_value = ["docker", "compose"]
    mock_streaming.return_value = 0
    runner = CliRunner()
    result = runner.invoke(main, ["logs", "web"])
    assert result.exit_code == 0
    mock_streaming.assert_called_once_with(["docker", "compose", "logs", "web"])


@patch("flow_deploy.discovery.discover_hosts")
@patch("flow_deploy.discovery.env_overrides")
@patch("flow_deploy.compose.compose_config")
def test_config_happy_path(mock_config, mock_env, mock_discover):
    mock_config.return_value = {"services": {}}
    mock_env.return_value = {}
    mock_discover.return_value = [
        {"host": "h1", "user": "deploy", "dir": "/srv", "services": ["web"]}
    ]
    runner = CliRunner()
    result = runner.invoke(main, ["config"])
    assert result.exit_code == 0
    import json

    data = json.loads(result.output)
    assert data[0]["host"] == "h1"


@patch("flow_deploy.compose.compose_config")
def test_config_compose_failure(mock_config):
    mock_config.side_effect = RuntimeError("compose config failed: bad")
    runner = CliRunner()
    result = runner.invoke(main, ["config"])
    assert result.exit_code == 1


@patch("flow_deploy.discovery.discover_hosts")
@patch("flow_deploy.discovery.env_overrides")
@patch("flow_deploy.compose.compose_config")
def test_config_missing_host(mock_config, mock_env, mock_discover):
    mock_config.return_value = {"services": {}}
    mock_env.return_value = {}
    mock_discover.side_effect = ValueError("services missing deploy host: web")
    runner = CliRunner()
    result = runner.invoke(main, ["config"])
    assert result.exit_code == 1


@patch("flow_deploy.discovery.discover_hosts")
@patch("flow_deploy.discovery.env_overrides")
@patch("flow_deploy.compose.compose_config")
def test_config_with_command(mock_config, mock_env, mock_discover):
    mock_config.return_value = {"services": {}}
    mock_env.return_value = {}
    mock_discover.return_value = []
    runner = CliRunner()
    result = runner.invoke(main, ["config", "--command", "docker compose"])
    assert result.exit_code == 0
    mock_config.assert_called_once_with(cmd=["docker", "compose"])


STATUS_COMPOSE = {
    "services": {
        "web": {"labels": {"deploy.role": "app"}},
        "postgres": {"labels": {"deploy.role": "accessory"}},
    }
}


@patch("flow_deploy.containers.get_container_health")
@patch("flow_deploy.containers.get_containers_for_service")
@patch("flow_deploy.git.current_sha")
@patch("flow_deploy.compose.compose_config")
def test_status_lists_each_service(mock_config, mock_sha, mock_ctrs, mock_health, capsys):
    mock_config.return_value = STATUS_COMPOSE
    mock_sha.return_value = "abc1234def"
    mock_ctrs.side_effect = lambda name: (
        [{"ID": "0123456789abcdef", "Image": "app:v1", "State": "running"}] if name == "web" else []
    )
    mock_health.return_value = None
    result = CliRunner().invoke(main, ["status"])
    assert result.exit_code == 0
    out = capsys.readouterr().out + result.output
    assert "Current SHA: abc1234" in out
    assert "web (app)  0123456789ab  app:v1  running/none" in out
    assert "postgres (accessory)  no containers" in out


@patch("flow_deploy.git.current_sha")
@patch("flow_deploy.compose.compose_config")
def test_status_without_a_sha(mock_config, mock_sha, capsys):
    mock_config.return_value = {"services": {}}
    mock_sha.return_value = ""
    result = CliRunner().invoke(main, ["status"])
    assert result.exit_code == 0
    assert "Current SHA: (none)" in capsys.readouterr().out + result.output


@patch("flow_deploy.compose.compose_config")
def test_status_compose_failure(mock_config):
    mock_config.side_effect = RuntimeError("compose config failed: bad")
    assert CliRunner().invoke(main, ["status"]).exit_code == 1


def test_help():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "deploy" in result.output
    assert "status" in result.output
    assert "exec" in result.output
    assert "logs" in result.output
    assert "upgrade" in result.output
    assert "config" in result.output
