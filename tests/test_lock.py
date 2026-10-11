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


def test_read_lock_corrupt_returns_none():
    with open(".git/deploy-lock", "w") as f:
        f.write("{not json")
    assert read_lock() is None


def _contend(path, barrier, results, hold):
    os.chdir(path)
    barrier.wait()
    results.put(acquire())
    hold.wait(5)  # stay alive, so a held lock isn't mistaken for a stale one


def test_concurrent_acquires_have_exactly_one_winner(tmp_path):
    """Deploys started together: only one may get the lock. (With the guard's
    flock stubbed out, the old check-then-write race let several through.)"""
    import multiprocessing

    (tmp_path / ".git").mkdir(exist_ok=True)
    ctx = multiprocessing.get_context("fork")
    for _ in range(5):
        lock_file = tmp_path / ".git" / "deploy-lock"
        if lock_file.exists():
            lock_file.unlink()
        barrier, results, hold = ctx.Barrier(8), ctx.Queue(), ctx.Event()
        procs = [
            ctx.Process(target=_contend, args=(str(tmp_path), barrier, results, hold))
            for _ in range(8)
        ]
        for proc in procs:
            proc.start()
        wins = [results.get(timeout=10) for _ in procs]
        hold.set()
        for proc in procs:
            proc.join(10)
        assert wins.count(True) == 1
