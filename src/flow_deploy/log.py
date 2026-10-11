"""Timestamped output + GitHub Actions formatting."""

import os
import sys
from datetime import datetime


def _timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _is_github_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


RUN_LOG = ".git/flow-deploy.log"
RUN_MARK = "===== flow-deploy run "
_run_log = None  # the open run log while a deploy runs


def _write(text: str, stream=None) -> None:
    """Print, but never raise.

    When a remote deploy's ssh session drops, its stdout and stderr go with
    it. Logging must not then crash the revert that's putting things back.
    Every line also goes to the run log, if one is open, so a deploy whose
    CI job was cancelled (and so has no one watching) still leaves a record.
    """
    try:
        print(text, file=stream or sys.stdout, flush=True)
    except (OSError, ValueError):
        pass
    if _run_log is not None and not text.startswith("::"):
        try:
            _run_log.write(text + "\n")
            _run_log.flush()
        except (OSError, ValueError):
            pass


def open_run(title: str, path: str = RUN_LOG, keep: int = 20) -> None:
    """Start a run in the host-side log, keeping the last ``keep`` runs.

    Lives in .git, so it never dirties the tree. Never raises: a deploy
    goes ahead without its log rather than fail over it.
    """
    global _run_log
    close_run(None)
    try:
        if not os.path.isdir(os.path.dirname(path) or "."):
            return
        runs = _runs(path)[-(keep - 1) :] if keep > 1 else []
        stamp = datetime.now().isoformat(timespec="seconds")
        with open(path, "w") as f:
            f.writelines(runs)
            f.write(f"{RUN_MARK}{stamp} pid {os.getpid()}: {title} =====\n")
        _run_log = open(path, "a")
    except OSError:
        _run_log = None


def close_run(code: int | None) -> None:
    """End the current run in the log with its exit code."""
    global _run_log
    if _run_log is None:
        return
    try:
        if code is not None:
            _run_log.write(f"===== exit {code} =====\n")
        _run_log.close()
    except (OSError, ValueError):
        pass
    _run_log = None


def _runs(path: str) -> list[str]:
    """The log's runs, oldest first, each as one string."""
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return []
    runs: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith(RUN_MARK) or not runs:
            runs.append("")
        runs[-1] += line
    return runs


def last_run(path: str = RUN_LOG) -> tuple[str, int | None] | None:
    """(header, exit code or None if it never finished) of the last run, or None."""
    runs = _runs(path)
    if not runs or not runs[-1].startswith(RUN_MARK):
        return None
    lines = runs[-1].splitlines()
    header = lines[0][len(RUN_MARK) :].removesuffix(" =====")
    end = lines[-1]
    if end.startswith("===== exit ") and end.endswith(" ====="):
        try:
            return header, int(end[len("===== exit ") : -len(" =====")])
        except ValueError:
            pass  # a damaged line: treat it like no exit recorded
    return header, None


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


def warning(msg: str, stdout_clean: bool = False) -> None:
    """``stdout_clean`` keeps even the GitHub annotation off stdout, for
    commands whose stdout is data (flow-deploy config, flow-deploy tag)."""
    if _is_github_actions():
        _write(f"::warning::{msg}", stream=sys.stderr if stdout_clean else None)
    _write(f"[{_timestamp()}] WARNING: {msg}", stream=sys.stderr)


def error(msg: str) -> None:
    if _is_github_actions():
        _write(f"::error::{msg}")
    _write(f"[{_timestamp()}] ERROR: {msg}", stream=sys.stderr)
