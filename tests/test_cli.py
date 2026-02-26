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
    result = runner.invoke(main, ["deploy", "--tag", "v1", "--service", "web", "--service", "worker"])
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


@patch("flow_deploy.deploy.rollback")
def test_rollback(mock_rollback):
    mock_rollback.return_value = 0
    runner = CliRunner()
    result = runner.invoke(main, ["rollback"])
    assert result.exit_code == 0
    mock_rollback.assert_called_once_with(services_filter=None)


@patch("flow_deploy.deploy.rollback")
def test_rollback_with_service(mock_rollback):
    mock_rollback.return_value = 0
    runner = CliRunner()
    result = runner.invoke(main, ["rollback", "--service", "web"])
    assert result.exit_code == 0
    mock_rollback.assert_called_once_with(services_filter=["web"])


@patch("flow_deploy.deploy.rollback")
def test_rollback_failure(mock_rollback):
    mock_rollback.return_value = 1
    runner = CliRunner()
    result = runner.invoke(main, ["rollback"])
    assert result.exit_code == 1


def test_version():
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "flow-deploy" in result.output
    assert "0.1.0" in result.output


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


def test_help():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "deploy" in result.output
    assert "rollback" in result.output
    assert "status" in result.output
    assert "exec" in result.output
    assert "logs" in result.output
