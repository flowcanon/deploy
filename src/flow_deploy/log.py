"""Timestamped output + GitHub Actions formatting."""

import os
import sys
from datetime import datetime


def _timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _is_github_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def info(msg: str) -> None:
    print(f"[{_timestamp()}] {msg}", flush=True)


def header(title: str) -> None:
    line = f"── {title} " + "─" * max(0, 45 - len(title))
    if _is_github_actions():
        print(f"::group::{title}", flush=True)
    info(line)


def footer(title: str) -> None:
    line = f"── {title} " + "─" * max(0, 45 - len(title))
    info(line)
    if _is_github_actions():
        print("::endgroup::", flush=True)


def service_start(name: str) -> None:
    if _is_github_actions():
        print(f"::group::{name}", flush=True)
    info(f"▸ {name}")


def service_end() -> None:
    if _is_github_actions():
        print("::endgroup::", flush=True)


def step(msg: str) -> None:
    info(f"  {msg}")


def success(msg: str) -> None:
    info(f"  ✓ {msg}")


def failure(msg: str) -> None:
    if _is_github_actions():
        print(f"::error::{msg}", flush=True)
    info(f"  ✗ {msg}")


def error(msg: str) -> None:
    if _is_github_actions():
        print(f"::error::{msg}", flush=True)
    print(f"[{_timestamp()}] ERROR: {msg}", file=sys.stderr, flush=True)
