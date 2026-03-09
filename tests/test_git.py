"""Tests for git.py — dirty check, fetch, detached checkout, restore."""

from flow_deploy import process
from flow_deploy.git import (
    checkout_detached,
    current_sha,
    fetch,
    is_dirty,
    preflight_and_checkout,
    restore,
)


def _ok(stdout=""):
    return process.Result(0, stdout, "")


def _err(stderr="error"):
    return process.Result(1, "", stderr)


def test_is_dirty_clean(mock_process):
    mock_process.responses.append(_ok(""))
    assert is_dirty() is False


def test_is_dirty_dirty(mock_process):
    mock_process.responses.append(_ok(" M somefile.py\n"))
    assert is_dirty() is True


def test_fetch_success(mock_process):
    mock_process.responses.append(_ok())
    result = fetch()
    assert result.returncode == 0


def test_current_sha(mock_process):
    mock_process.responses.append(_ok("abc123def456\n"))
    assert current_sha() == "abc123def456"


def test_checkout_detached_success(mock_process):
    mock_process.responses.append(_ok())
    result = checkout_detached("abc123")
    assert result.returncode == 0
    assert mock_process.calls[-1][1] == ["git", "checkout", "--detach", "abc123"]


def test_preflight_clean_repo(mock_process):
    mock_process.responses.extend(
        [
            _ok(""),  # git status --porcelain (clean)
            _ok(),  # git fetch origin
            _ok("prev123\n"),  # git rev-parse HEAD
            _ok(),  # git checkout --detach <sha>
        ]
    )
    code, prev = preflight_and_checkout("abc123")
    assert code == 0
    assert prev == "prev123"


def test_preflight_dirty_repo(mock_process, capsys):
    mock_process.responses.append(_ok(" M dirty.py\n"))
    code, prev = preflight_and_checkout("abc123")
    assert code == 1
    assert prev is None
    err = capsys.readouterr().err
    assert "dirty" in err


def test_preflight_fetch_failure(mock_process):
    mock_process.responses.extend(
        [
            _ok(""),  # clean
            _err("fetch error"),  # fetch fails
        ]
    )
    code, prev = preflight_and_checkout("abc123")
    assert code == 1
    assert prev is None


def test_preflight_checkout_failure(mock_process):
    mock_process.responses.extend(
        [
            _ok(""),  # clean
            _ok(),  # fetch ok
            _ok("prev123\n"),  # rev-parse
            _err("bad revision"),  # checkout fails
        ]
    )
    code, prev = preflight_and_checkout("abc123")
    assert code == 1
    assert prev is None


def test_restore_success(mock_process, capsys):
    mock_process.responses.append(_ok())
    assert restore("prev123") is True
    out = capsys.readouterr().out
    assert "restoring repo to prev123" in out


def test_restore_failure(mock_process):
    mock_process.responses.append(_err("checkout error"))
    assert restore("prev123") is False
