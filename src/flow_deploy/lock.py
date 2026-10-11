"""Deploy lock — acquire/release/stale recovery.

Lock file lives in .git/ to avoid dirtying the working tree.
"""

import fcntl
import json
import os
import time

LOCK_FILE = ".git/deploy-lock"


def _lock_path() -> str:
    return LOCK_FILE


def _is_pid_running(pid: int) -> bool:
    """Check if a process is running via kill(pid, 0)."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire() -> bool:
    """Acquire the deploy lock. Returns True if acquired, False if held by another process.

    Automatically breaks stale locks (holding PID no longer running).

    Acquirers take an flock on a guard file while they check and write the
    lock, so two deploys starting together can't both see it free. The
    kernel drops an flock when its holder exits, so the guard can't go stale.
    """
    path = _lock_path()
    with open(path + ".guard", "a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        return _acquire(path)


def _acquire(path: str) -> bool:
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
            pid = data["pid"]
            if _is_pid_running(pid):
                return False
            # Stale lock — break it
        except (json.JSONDecodeError, KeyError, TypeError):
            pass  # Corrupt lock file — overwrite it

    with open(path, "w") as f:
        json.dump({"pid": os.getpid(), "timestamp": time.time()}, f)
    return True


def release() -> None:
    """Release the deploy lock."""
    path = _lock_path()
    if os.path.exists(path):
        os.remove(path)


def read_lock() -> dict | None:
    """Read current lock info, or None if not locked."""
    path = _lock_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, TypeError):
        return None
