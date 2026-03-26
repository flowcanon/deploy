"""Git operations for detached-HEAD deploy strategy."""

from flow_deploy import log, process


def is_dirty() -> bool:
    """Return True if the working tree has uncommitted changes."""
    result = process.run(["git", "status", "--porcelain"])
    return bool(result.stdout.strip())


def fetch() -> process.Result:
    """Fetch from origin."""
    return process.run(["git", "fetch", "origin"])


def current_sha() -> str:
    """Return the current HEAD SHA."""
    result = process.run(["git", "rev-parse", "HEAD"])
    return result.stdout.strip()


def checkout_detached(sha: str) -> process.Result:
    """Checkout a specific SHA in detached HEAD mode."""
    return process.run(["git", "checkout", "--detach", sha])


def preflight_and_checkout(tag: str | None = None) -> tuple[int, str | None]:
    """Run git pre-flight checks and optionally checkout the deploy SHA.

    When tag is provided, checks out that ref in detached HEAD mode.
    When tag is None, runs preflight only (dirty check + fetch) without checkout.

    Returns (exit_code, previous_sha).
    - (0, previous_sha) on success.
    - (1, None) on error — git operation failed or working tree is dirty.
    """
    # 1. Dirty check
    if is_dirty():
        log.error("working tree is dirty — deploy aborted")
        return 1, None

    # 2. Fetch
    result = fetch()
    if result.returncode != 0:
        log.error(f"git fetch failed: {result.stderr.strip()}")
        return 1, None

    # 3. Record previous SHA
    previous_sha = current_sha()

    # 4. Checkout new SHA (detached HEAD) — skip when deploying current HEAD
    if tag is not None:
        result = checkout_detached(tag)
        if result.returncode != 0:
            log.error(f"git checkout failed: {result.stderr.strip()}")
            return 1, None

    return 0, previous_sha


def restore(previous_sha: str) -> bool:
    """Restore repo to a previous SHA after a failed deploy."""
    log.step(f"restoring repo to {previous_sha[:7]}...")
    result = checkout_detached(previous_sha)
    if result.returncode != 0:
        log.error(f"git restore failed: {result.stderr.strip()}")
        return False
    return True
