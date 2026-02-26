"""Tests for lock.py — deploy locking."""

import json
import os

from flow_deploy.lock import acquire, read_lock, release


def test_acquire_and_release(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert acquire() is True
    lock = read_lock()
    assert lock is not None
    assert lock["pid"] == os.getpid()
    assert "timestamp" in lock
    release()
    assert read_lock() is None


def test_acquire_fails_when_held(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Write a lock with current PID (which is running)
    assert acquire() is True
    assert acquire() is False
    release()


def test_stale_lock_broken(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Write a lock with a PID that doesn't exist
    with open(tmp_path / ".deploy-lock", "w") as f:
        json.dump({"pid": 999999999, "timestamp": 0}, f)
    assert acquire() is True
    lock = read_lock()
    assert lock["pid"] == os.getpid()
    release()


def test_corrupt_lock_overwritten(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with open(tmp_path / ".deploy-lock", "w") as f:
        f.write("not json")
    assert acquire() is True
    release()


def test_release_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    release()  # Should not raise


def test_read_lock_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert read_lock() is None
