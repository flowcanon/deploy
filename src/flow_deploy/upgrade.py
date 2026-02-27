"""Upgrade flow-deploy to the latest release."""

import os
import shutil
import stat
import subprocess
import sys
import tempfile

REPO = "flowcanon/deploy"


def _detect_libc() -> str:
    """Detect whether the system uses musl or glibc."""
    try:
        result = subprocess.run(["ldd", "/bin/ls"], capture_output=True, text=True, timeout=5)
        if "musl" in result.stdout.lower() or "musl" in result.stderr.lower():
            return "musl"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "glibc"


def _binary_path() -> str:
    """Return the path to the currently running flow-deploy binary."""
    # PyInstaller sets sys._MEIPASS; sys.executable is the binary itself
    if getattr(sys, "_MEIPASS", None):
        return sys.executable
    # Editable / pip install — find on PATH
    which = shutil.which("flow-deploy")
    if which:
        return which
    raise RuntimeError("Cannot determine flow-deploy install location")


def _download(url: str, dest: str) -> None:
    """Download a URL to a local path using curl or wget."""
    if shutil.which("curl"):
        subprocess.run(["curl", "-fsSL", "-o", dest, url], check=True, timeout=120)
    elif shutil.which("wget"):
        subprocess.run(["wget", "-qO", dest, url], check=True, timeout=120)
    else:
        raise RuntimeError("curl or wget required")


def upgrade() -> int:
    """Download and replace the current binary with the latest release.

    Returns 0 on success, 1 on failure.
    """
    from flow_deploy import __version__, log

    libc = _detect_libc()
    url = f"https://github.com/{REPO}/releases/latest/download/flow-deploy-linux-{libc}"

    try:
        current_path = _binary_path()
    except RuntimeError as e:
        log.error(str(e))
        return 1

    log.info(f"Current version: {__version__}")
    log.info(f"Binary: {current_path}")
    log.info(f"Detected libc: {libc}")
    log.info(f"Downloading latest from {url}...")

    # Download to temp file in the same directory, then atomic rename
    target_dir = os.path.dirname(current_path)
    try:
        fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix=".flow-deploy-")
        os.close(fd)
        _download(url, tmp_path)
        os.chmod(tmp_path, os.stat(tmp_path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(tmp_path, current_path)
    except Exception as e:
        # Clean up temp file on failure
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        log.error(f"Upgrade failed: {e}")
        return 1

    log.success("Upgraded successfully. Run 'flow-deploy --version' to verify.")
    return 0
