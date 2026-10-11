"""Git operations for detached-HEAD deploy strategy."""

from flow_deploy import log, process


def dirty_paths() -> list[str]:
    """What makes the working tree dirty: `git status --porcelain` lines."""
    result = process.run(["git", "status", "--porcelain"])
    return [line for line in result.stdout.splitlines() if line.strip()]


def is_dirty() -> bool:
    """Return True if the working tree has uncommitted changes."""
    return bool(dirty_paths())


def fetch() -> process.Result:
    """Fetch from origin, including every tag.

    --tags fetches release tags even when they aren't on a fetched branch;
    --force updates a tag that was moved and re-pushed instead of refusing.
    """
    return process.run(["git", "fetch", "--tags", "--force", "origin"])


def current_sha() -> str:
    """Return the current HEAD SHA."""
    result = process.run(["git", "rev-parse", "HEAD"])
    return result.stdout.strip()


def is_ref(ref: str) -> bool:
    """True if ``ref`` names a commit in this repo: a tag, a branch, or a SHA."""
    result = process.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    return result.returncode == 0


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
    # 1. Dirty check. Name what's dirty, so a remote deploy that stops here
    # can be fixed from its CI log alone.
    dirty = dirty_paths()
    if dirty:
        log.error("working tree is dirty — deploy aborted; uncommitted or untracked:")
        for line in dirty[:10]:
            log.step(line)
        if len(dirty) > 10:
            log.step(f"... and {len(dirty) - 10} more (git status)")
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
