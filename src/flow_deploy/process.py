"""Subprocess wrapper — the single mock seam for all tests."""

import subprocess
from dataclasses import dataclass


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str


def run(args: list[str], env: dict[str, str] | None = None, cwd: str | None = None) -> Result:
    """Run a command and capture output. Raises on non-zero exit."""
    merged_env = None
    if env is not None:
        import os

        merged_env = {**os.environ, **env}

    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        env=merged_env,
        cwd=cwd,
    )
    return Result(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def run_streaming(
    args: list[str], env: dict[str, str] | None = None, cwd: str | None = None
) -> int:
    """Run a command with passthrough stdout/stderr. Returns exit code."""
    merged_env = None
    if env is not None:
        import os

        merged_env = {**os.environ, **env}

    proc = subprocess.run(
        args,
        env=merged_env,
        cwd=cwd,
    )
    return proc.returncode
