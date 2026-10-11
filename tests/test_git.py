"""Tests for git.py — dirty check, fetch, detached checkout, restore."""

from flow_deploy import process
from flow_deploy.git import (
    checkout_detached,
    current_sha,
    fetch,
    is_dirty,
    is_ref,
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
    # Version tags are fetched, and a re-pushed tag moves locally.
    assert mock_process.calls[0][1] == ["git", "fetch", "--tags", "--force", "origin"]


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
    mock_process.responses.append(_ok(" M dirty.py\n?? .deploy-tag\n"))
    code, prev = preflight_and_checkout("abc123")
    assert code == 1
    assert prev is None
    captured = capsys.readouterr()
    assert "working tree is dirty — deploy aborted" in captured.err
    # What's dirty is named, one per line, so a CI log shows it.
    assert " M dirty.py" in captured.out and "?? .deploy-tag" in captured.out
    assert len(mock_process.calls) == 1  # nothing fetched or checked out


def test_preflight_dirty_repo_lists_at_most_ten(mock_process, capsys):
    mock_process.responses.append(_ok("".join(f"?? file{i}\n" for i in range(13))))
    assert preflight_and_checkout("abc123") == (1, None)
    out = capsys.readouterr().out
    assert "?? file9" in out and "?? file10" not in out
    assert "... and 3 more (git status)" in out


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


def test_preflight_no_tag_skips_checkout(mock_process):
    """When tag is None, preflight runs dirty check + fetch but skips checkout."""
    mock_process.responses.extend(
        [
            _ok(""),  # git status --porcelain (clean)
            _ok(),  # git fetch origin
            _ok("head123\n"),  # git rev-parse HEAD
        ]
    )
    code, prev = preflight_and_checkout(None)
    assert code == 0
    assert prev == "head123"
    assert len(mock_process.calls) == 3  # no checkout call


def test_restore_success(mock_process, capsys):
    mock_process.responses.append(_ok())
    assert restore("prev123") is True
    out = capsys.readouterr().out
    assert "restoring repo to prev123" in out


def test_restore_failure(mock_process):
    mock_process.responses.append(_err("checkout error"))
    assert restore("prev123") is False


def test_is_ref(mock_process):
    mock_process.responses.append(_ok())
    mock_process.responses.append(process.Result(1, "", ""))
    assert is_ref("v1.4.0") is True
    assert mock_process.calls[0][1] == [
        "git",
        "rev-parse",
        "--verify",
        "--quiet",
        "v1.4.0^{commit}",
    ]
    assert is_ref("latest") is False
