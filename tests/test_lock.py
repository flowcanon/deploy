"""Tests for lock.py — deploy locking."""

import json
import os

import pytest

from flow_deploy.lock import acquire, read_lock, release


@pytest.fixture(autouse=True)
def _git_dir(tmp_path, monkeypatch):
    """Create .git/ and chdir so the lock file has a home."""
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)


def test_acquire_and_release():
    assert acquire() is True
    lock = read_lock()
    assert lock is not None
    assert lock["pid"] == os.getpid()
    assert "timestamp" in lock
    release()
    assert read_lock() is None


def test_acquire_fails_when_held():
    assert acquire() is True
    assert acquire() is False
    release()


def test_stale_lock_broken(tmp_path):
    with open(tmp_path / ".git" / "deploy-lock", "w") as f:
        json.dump({"pid": 999999999, "timestamp": 0}, f)
    assert acquire() is True
    lock = read_lock()
    assert lock["pid"] == os.getpid()
    release()


def test_corrupt_lock_overwritten(tmp_path):
    with open(tmp_path / ".git" / "deploy-lock", "w") as f:
        f.write("not json")
    assert acquire() is True
    release()


def test_release_no_file():
    release()  # Should not raise


def test_read_lock_no_file():
    assert read_lock() is None
