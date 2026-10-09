"""Timestamped output + GitHub Actions formatting."""

import os
import sys
from datetime import datetime


def _timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _is_github_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def _write(text: str, stream=None) -> None:
    """Print, but never raise.

    When a remote deploy's ssh session drops, its stdout and stderr go with
    it. Logging must not then crash the revert that's putting things back.
    """
    try:
        print(text, file=stream or sys.stdout, flush=True)
    except (OSError, ValueError):
        pass


def info(msg: str) -> None:
    _write(f"[{_timestamp()}] {msg}")


def header(title: str) -> None:
    line = f"── {title} " + "─" * max(0, 45 - len(title))
    if _is_github_actions():
        _write(f"::group::{title}")
    info(line)


def footer(title: str) -> None:
    line = f"── {title} " + "─" * max(0, 45 - len(title))
    info(line)
    if _is_github_actions():
        _write("::endgroup::")


def service_start(name: str) -> None:
    if _is_github_actions():
        _write(f"::group::{name}")
    info(f"▸ {name}")


def service_end() -> None:
    if _is_github_actions():
        _write("::endgroup::")


def step(msg: str) -> None:
    info(f"  {msg}")


def success(msg: str) -> None:
    info(f"  ✓ {msg}")


def failure(msg: str) -> None:
    if _is_github_actions():
        _write(f"::error::{msg}")
    info(f"  ✗ {msg}")


def error(msg: str) -> None:
    if _is_github_actions():
        _write(f"::error::{msg}")
    _write(f"[{_timestamp()}] ERROR: {msg}", stream=sys.stderr)
