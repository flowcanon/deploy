"""Shared test fixtures."""

import pytest


@pytest.fixture
def mock_process(monkeypatch):
    """Mock process.run and process.run_streaming for tests."""
    from flow_deploy import process

    calls = []
    responses = []

    def fake_run(args, env=None, cwd=None):
        calls.append(("run", args, env, cwd))
        if responses:
            return responses.pop(0)
        return process.Result(returncode=0, stdout="", stderr="")

    def fake_run_streaming(args, env=None, cwd=None):
        calls.append(("run_streaming", args, env, cwd))
        return 0

    monkeypatch.setattr(process, "run", fake_run)
    monkeypatch.setattr(process, "run_streaming", fake_run_streaming)

    return type("MockProcess", (), {"calls": calls, "responses": responses})()
