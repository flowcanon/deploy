"""Tests for process.py — subprocess wrapper."""

from flow_deploy.process import Result, run, run_streaming


def test_result_dataclass():
    r = Result(returncode=0, stdout="hello", stderr="")
    assert r.returncode == 0
    assert r.stdout == "hello"
    assert r.stderr == ""


def test_run_captures_output():
    result = run(["echo", "hello"])
    assert result.returncode == 0
    assert result.stdout.strip() == "hello"
    assert result.stderr == ""


def test_run_captures_stderr():
    result = run(["sh", "-c", "echo err >&2"])
    assert result.stderr.strip() == "err"


def test_run_returns_nonzero():
    result = run(["sh", "-c", "exit 42"])
    assert result.returncode == 42


def test_run_with_env():
    result = run(["sh", "-c", "echo $TEST_VAR"], env={"TEST_VAR": "works"})
    assert result.stdout.strip() == "works"


def test_run_streaming_returns_exit_code():
    code = run_streaming(["true"])
    assert code == 0


def test_run_streaming_nonzero():
    code = run_streaming(["false"])
    assert code != 0
