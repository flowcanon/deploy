"""Shared test fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _not_on_github_actions(monkeypatch):
    """Plain log output, as on a host; tests that need annotations set it."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


@pytest.fixture
def mock_process(monkeypatch):
    """Mock process.run and process.run_streaming for tests."""
    from flow_deploy import process

    calls = []
    responses = []
    new_sessions = []  # commands run in their own session (process.run new_session=True)

    def fake_run(args, env=None, cwd=None, new_session=False):
        calls.append(("run", args, env, cwd))
        if new_session:
            new_sessions.append(args)
        if responses:
            return responses.pop(0)
        return process.Result(returncode=0, stdout="", stderr="")

    def fake_run_streaming(args, env=None, cwd=None):
        calls.append(("run_streaming", args, env, cwd))
        return 0

    monkeypatch.setattr(process, "run", fake_run)
    monkeypatch.setattr(process, "run_streaming", fake_run_streaming)

    return type(
        "MockProcess",
        (),
        {"calls": calls, "responses": responses, "new_sessions": new_sessions},
    )()
