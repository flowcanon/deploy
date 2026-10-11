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


def _verify(path: str) -> str | None:
    """The downloaded binary's ``--version`` line, or None if it doesn't run.

    Checked before it replaces the working binary, so a wrong-libc build or
    a bad download can't leave the host without a flow-deploy that runs.
    """
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = result.stdout.strip()
    return out if result.returncode == 0 and "flow-deploy" in out else None


def _latest_version() -> str | None:
    """Fetch the latest release tag from GitHub."""
    import json

    url = f"https://api.github.com/repos/{REPO}/releases/latest"
    try:
        fd, tmp = tempfile.mkstemp()
        os.close(fd)
        _download(url, tmp)
        with open(tmp) as f:
            data = json.load(f)
        os.unlink(tmp)
        tag = data.get("tag_name", "")
        return tag.lstrip("v") if tag else None
    except Exception:
        return None


def upgrade() -> int:
    """Download and replace the current binary with the latest release.

    Returns 0 on success, 1 on failure. Skips if already up to date.
    """
    from flow_deploy import __version__, log

    log.info(f"Current version: {__version__}")

    latest = _latest_version()
    if latest and latest == __version__:
        log.success(f"Already up to date ({__version__}).")
        return 0

    libc = _detect_libc()
    url = f"https://github.com/{REPO}/releases/latest/download/flow-deploy-linux-{libc}"

    try:
        current_path = _binary_path()
    except RuntimeError as e:
        log.error(str(e))
        return 1

    if latest:
        log.info(f"Latest version: {latest}")
    log.info(f"Binary: {current_path}")
    log.info(f"Detected libc: {libc}")
    log.info(f"Downloading latest from {url}...")

    # Download to a temp file in the same directory, check it runs, then
    # rename it over the old one: atomic, so the binary is never half-written.
    target_dir = os.path.dirname(current_path)
    if not os.access(target_dir, os.W_OK):
        log.error(
            f"Can't upgrade {current_path}: {target_dir} isn't writable by this user. "
            "Install flow-deploy where this user can write (the installer uses "
            "~/.local/bin), or upgrade as the binary's owner."
        )
        return 1
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix=".flow-deploy-")
        os.close(fd)
        _download(url, tmp_path)
        os.chmod(tmp_path, os.stat(tmp_path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        new_version = _verify(tmp_path)
        if new_version is None:
            raise RuntimeError("the downloaded binary doesn't run (wrong libc, or a bad download)")
        os.replace(tmp_path, current_path)
    except Exception as e:
        # The old binary is untouched; just remove the temp file.
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        log.error(f"Upgrade failed: {e}")
        return 1

    log.success(f"Upgraded: {new_version}")
    return 0
