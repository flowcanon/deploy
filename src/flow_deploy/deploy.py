"""Core deploy algorithm."""

import signal
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from flow_deploy import compose, config, containers, git, lock, log

EXIT_OK = 0
EXIT_FAILED = 1  # failed; everything it touched was put back
EXIT_LOCKED = 2
EXIT_DEGRADED = 3  # failed, and something couldn't be put back

# Signals that revert the deploy: a timeout or shutdown (SIGTERM), Ctrl-C, and
# a hung-up terminal (SIGHUP). A cancelled CI job running `ssh host
# flow-deploy ...` without a pty sends none of them: the remote deploy runs on
# (tests/integration/test_ci_cancel.py).
HANDLED_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)

# Per-service outcomes from _deploy_service and _start_service.
SVC_OK = 0
SVC_FAILED = 1  # failed, and its own cleanup succeeded
SVC_DIRTY = 2  # failed, and its own cleanup failed too


class Interrupted(BaseException):
    """Raised by the signal handler so the deploy reverts before exiting.

    A BaseException, so no ``except Exception`` along the way swallows it.
    """

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


@contextmanager
def _signals_deferred(why: str):
    """Hold handled signals until the block finishes, then act on the first one.

    The deploy's own handlers are swapped for one that only records; on exit
    they're put back and, if a signal arrived, Interrupted is raised then.
    """
    pending = []

    def hold(signum, frame):
        if not pending:
            log.step(f"received signal {signum}; {why}, then reverting")
        pending.append(signum)

    previous = {signum: signal.getsignal(signum) for signum in HANDLED_SIGNALS}
    for signum in HANDLED_SIGNALS:
        signal.signal(signum, hold)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    if pending:
        raise Interrupted(pending[0])


@dataclass
class Drained:
    """What a drain-first service was running before its drain."""

    svc: config.ServiceConfig
    replicas: int = 0
    old_tag: str | None = None
    old_image: str = ""


@dataclass
class _Run:
    """What a deploy has changed so far, so any failure can put it back."""

    compose_cmd: list[str]
    previous_sha: str | None = None
    drained: list[Drained] = field(default_factory=list)
    started: set[str] = field(default_factory=set)  # drain-first, healthy on the new tag

    def revert(self, reason: str, down: list[Drained] | None = None) -> int:
        """Put things back as they were before the deploy.

        Restores the repo and brings ``down`` (by default every drained
        service not yet healthy on the new tag) back on the tags they ran.
        Expand-then-contract migrations keep old code compatible with the new
        schema, as rolling deploys already assume. Exits 3 only when
        something can't be put back.
        """
        # Don't let a further signal cut the revert short (or start a second one).
        for signum in HANDLED_SIGNALS:
            signal.signal(signum, signal.SIG_IGN)
        if down is None:
            down = [r for r in self.drained if r.svc.name not in self.started]
        log.info("")
        problems = []
        if self.previous_sha is not None and not git.restore(self.previous_sha):
            problems.append("git restore failed")
        if not _restore_drained(down, self.compose_cmd):
            problems.append("drained services could not be restored")
        log.footer(f"FAILED ({reason})")
        return _degraded(", ".join(problems)) if problems else EXIT_FAILED


def deploy(
    tag: str | None = None,
    services_filter: list[str] | None = None,
    dry_run: bool = False,
    cmd: list[str] | None = None,
) -> int:
    """Perform a deploy. Returns an EXIT_* code: 0 ok, 1 failed and reverted,
    2 locked, 3 failed and not fully reverted."""
    compose_cmd = cmd or compose.resolve_command()

    # Determine tag: if not provided, deploy whatever is currently checked out
    tag_provided = tag is not None
    if not tag_provided:
        tag = git.current_sha()

    if dry_run:
        # Dry run: just parse config from current checkout, no git or lock
        try:
            compose_dict = compose.compose_config(cmd=compose_cmd)
        except RuntimeError as e:
            log.error(str(e))
            return EXIT_FAILED
        app_services = _resolve_app_services(compose_dict, services_filter)
        if app_services is None:
            return EXIT_FAILED
        _dry_run(tag, app_services)
        return EXIT_OK

    # A signal (Ctrl-C, a timeout, a hung-up terminal) reverts like any other
    # failure. Handlers go in before the lock is taken, so no
    # signal can land between the two and skip cleanup.
    originals = {signum: signal.getsignal(signum) for signum in HANDLED_SIGNALS}

    def _interrupt(signum, frame):
        raise Interrupted(signum)

    for signum in HANDLED_SIGNALS:
        signal.signal(signum, _interrupt)

    run = _Run(compose_cmd)
    locked = False
    try:
        # Acquire lock before any mutations (git checkout, container changes)
        locked = lock.acquire()
        if not locked:
            lock_info = lock.read_lock()
            pid = lock_info["pid"] if lock_info else "unknown"
            log.error(f"Deploy lock held by PID {pid}")
            return EXIT_LOCKED
        return _deploy_locked(run, tag, tag_provided, services_filter)
    except (Exception, Interrupted) as exc:
        if isinstance(exc, Interrupted):
            reason = f"interrupted by signal {exc.signum}"
        else:
            reason = f"unexpected error: {exc!r}"
        log.error(f"{reason}; reverting")
        return run.revert(reason)
    finally:
        # Ignore signals first, so one landing here can't skip the rest.
        for signum in HANDLED_SIGNALS:
            signal.signal(signum, signal.SIG_IGN)
        # Always released: a failed deploy puts things back rather than
        # leaving state for someone to clean up by hand.
        if locked:
            lock.release()
        for signum, handler in originals.items():
            signal.signal(signum, handler)


def _deploy_locked(
    run: _Run, tag: str, tag_provided: bool, services_filter: list[str] | None
) -> int:
    """The deploy proper, under the lock. Records what it changes on ``run``."""
    # Git pre-flight: dirty check, fetch, checkout detached.
    # Protected by the lock so concurrent deploys can't race on checkout.
    # Must happen before config parsing so we read the compose file
    # from the target commit, not whatever is currently checked out.
    git_code, run.previous_sha = git.preflight_and_checkout(tag if tag_provided else None)
    if git_code != 0:
        return EXIT_FAILED

    # Parse compose config (now reading from the target commit)
    try:
        compose_dict = compose.compose_config(cmd=run.compose_cmd)
    except RuntimeError as e:
        log.error(str(e))
        return EXIT_FAILED if git.restore(run.previous_sha) else _degraded("git restore failed")

    app_services = _resolve_app_services(compose_dict, services_filter)
    if app_services is None:
        return EXIT_FAILED if git.restore(run.previous_sha) else _degraded("git restore failed")

    service_names = ", ".join(s.name for s in app_services)
    log.header("deploy")
    log.info(f"tag: {tag}")
    log.info(f"services: {service_names}")
    log.info("")

    project = compose_dict.get("name", "")
    compose_cmd = run.compose_cmd
    start_time = time.time()

    # app_services is in deploy.order. Services start in that order and stop
    # in reverse, so a worker comes up after the web and API it depends on,
    # and drains before them.
    drain_first = [s for s in app_services if s.drains_first]
    rolling = [s for s in app_services if not s.drains_first]
    dirty = []  # services whose own failure cleanup also failed

    def failed(reason: str, down: list[Drained] | None = None) -> int:
        code = run.revert(reason, down)
        if dirty and code == EXIT_FAILED:
            return _degraded(f"cleanup failed for {', '.join(dirty)}")
        return code

    # Phase 1: drain-first services stop, in reverse deploy order, before
    # anything rolls, so none of their old code runs beside new code. What
    # each one was running is kept to bring it back on failure.
    for svc in reversed(drain_first):
        if _drain_service(svc, tag, compose_cmd, run.drained, project=project) != 0:
            return failed("deploy aborted")

    # Phase 2: rolling services, in deploy order, as before.
    for svc in rolling:
        result = _deploy_service(svc, tag, compose_cmd, project=project)
        if result == SVC_DIRTY:
            dirty.append(svc.name)
        if result != SVC_OK:
            return failed("deploy aborted")

    # Phase 3: drain-first services start in deploy order, at the replica
    # count they ran. One that wasn't running (just added in this release,
    # or crashed) starts at 1: a deploy brings its services up. Every one is
    # attempted, so one failure doesn't strand the rest; a failed one goes
    # back to what it ran before.
    by_name = {record.svc.name: record for record in run.drained}
    failures = []
    for svc in drain_first:
        record = by_name[svc.name]
        result = _start_service(svc, tag, compose_cmd, max(record.replicas, 1), project=project)
        if result == SVC_OK:
            run.started.add(svc.name)
            continue
        if result == SVC_DIRTY:
            dirty.append(svc.name)
        failures.append(record)
    if failures:
        names = ", ".join(r.svc.name for r in failures)
        return failed(f"{names} failed to start", failures)

    elapsed = time.time() - start_time

    log.info("")
    log.info(f"HEAD detached at {tag}")
    log.footer(f"complete ({elapsed:.1f}s)")
    return EXIT_OK


def _degraded(problem: str) -> int:
    log.error(
        f"Deploy failed and could not be fully reverted ({problem}). "
        "Check the repo checkout and service state on this host."
    )
    return EXIT_DEGRADED


def _resolve_app_services(
    compose_dict: dict, services_filter: list[str] | None
) -> list[config.ServiceConfig] | None:
    """Parse and validate app services from compose config. Returns None on error."""
    all_services = config.parse_services(compose_dict)
    app_services = [s for s in all_services if s.is_app]

    if services_filter:
        selected = [s for s in app_services if s.name in services_filter]
        skipped = [s.name for s in app_services if s.drains_first and s not in selected]
        if skipped and any(not s.drains_first for s in selected):
            # A warning, not an error: the deploy proceeds and can succeed.
            log.info(
                f"warning: --service leaves drain-first {', '.join(skipped)} running "
                "old code beside the new rolling services; include them unless "
                "that's intended"
            )
        app_services = selected

    if not app_services:
        log.error("No app services to deploy")
        return None

    missing = config.validate_healthchecks(app_services)
    if missing:
        log.error(f"Services missing healthcheck: {', '.join(missing)}")
        return None

    unknown = config.validate_strategies(app_services)
    if unknown:
        log.error(
            f"Unknown deploy.strategy: {', '.join(unknown)} "
            f"(expected one of: {', '.join(config.STRATEGIES)})"
        )
        return None

    return app_services


def _pull(svc: config.ServiceConfig, env: dict, compose_cmd: list[str]) -> bool:
    image_name = (svc.image or svc.name).rsplit(":", 1)[0]
    log.step(f"pulling {image_name}:{env['DEPLOY_TAG']}...")
    pull_start = time.time()
    result = compose.compose_run(["pull", svc.name], env=env, cmd=compose_cmd)
    if result.returncode != 0:
        log.failure(f"pull failed: {result.stderr.strip()}")
        return False
    log.step(f"pulled ({time.time() - pull_start:.1f}s)")
    return True


def _image_tag(image: str) -> str | None:
    """The tag of ``registry/name:tag[@digest]``, or None when there's no tag.

    A bare image ID (``sha256:...``) has no tag.
    """
    if image.startswith("sha256:"):
        return None
    name = image.rsplit("/", 1)[-1].split("@", 1)[0]
    return name.rsplit(":", 1)[1] if ":" in name else None


def _drain_service(
    svc: config.ServiceConfig,
    tag: str,
    compose_cmd: list[str],
    drained: list[Drained],
    project: str = "",
) -> int:
    """Pull the new image, then stop and remove the running containers.

    Pulls first so an unpullable tag fails before anything stops. Records
    what was running on ``drained`` *before* stopping anything, so a signal
    mid-drain (a drain can take as long as the service's longest task) still
    knows what to restore. Returns 1 if the pull failed or any container
    failed to stop or be removed: the caller must not roll anything while old
    code may still be running.
    """
    log.service_start(svc.name)
    svc_start = time.time()
    env = {"DEPLOY_TAG": tag}

    if not _pull(svc, env, compose_cmd):
        log.service_end()
        return 1

    # Restarting containers too: a crash-looping worker would otherwise come
    # back on old code mid-deploy.
    ctrs = containers.list_containers_for_service(
        svc.name, project=project, statuses=("running", "restarting")
    )
    if ctrs is None:
        # Not knowing what's running is not the same as nothing running.
        log.failure(f"could not list {svc.name} containers; not draining blind")
        log.service_end()
        return 1
    images = list(dict.fromkeys(c.get("Image", "") for c in ctrs))
    record = Drained(svc, replicas=len(ctrs))
    if images:
        record.old_image = images[0]
        record.old_tag = _image_tag(images[0])
    drained.append(record)
    if len(images) > 1:
        log.error(
            f"{svc.name} was running mixed images ({', '.join(images)}); "
            f"a rollback restores {images[0]}"
        )
    if not ctrs:
        log.step("not running; nothing to drain")

    for ctr in ctrs:
        ctr_id = ctr["ID"]
        log.step(f"draining {ctr_id[:7]} ({svc.drain}s timeout)...")
        # Finish this container's stop before acting on a signal. Interrupting
        # the docker CLI doesn't interrupt the daemon's stop, so a revert
        # would find the container still "running", leave it be, and then
        # watch it exit. Once it's stopped and removed, the revert recreates
        # it cleanly.
        # The docker stop runs in its own session, so a terminal Ctrl-C (sent
        # to the whole foreground group) can't cut it short either.
        with _signals_deferred(f"finishing the drain of {ctr_id[:7]} first"):
            stopped = containers.stop_container(ctr_id, timeout=svc.drain, isolate=True)
            removed = stopped and containers.remove_container(ctr_id)
        if not stopped:
            log.failure(f"could not stop {ctr_id[:7]}; it may still be running")
            log.service_end()
            return 1
        if not removed:
            log.failure(f"could not remove {ctr_id[:7]}")
            log.service_end()
            return 1

    log.success(f"{svc.name} drained ({time.time() - svc_start:.1f}s)")
    log.service_end()
    return 0


def _start_service(
    svc: config.ServiceConfig,
    tag: str,
    compose_cmd: list[str],
    replicas: int = 1,
    project: str = "",
) -> int:
    """Start a drained service on the new tag at ``replicas`` and wait for health.

    Returns SVC_OK, or SVC_FAILED / SVC_DIRTY after removing what it started
    (DIRTY when that removal itself failed).
    """
    log.service_start(svc.name)
    svc_start = time.time()
    env = {"DEPLOY_TAG": tag}

    def fail(msg: str, ctrs: list[dict]) -> int:
        # Remove what started: an unhealthy worker may still be consuming,
        # and a restore on an unchanged config would otherwise reuse it.
        log.failure(msg)
        clean = _remove_all(ctrs, timeout=svc.drain)
        log.service_end()
        return SVC_FAILED if clean else SVC_DIRTY

    log.step(f"starting {replicas} new container(s)...")
    result = compose.compose_run(
        ["up", "-d", "--no-deps", "--scale", f"{svc.name}={replicas}", svc.name],
        env=env,
        cmd=compose_cmd,
    )
    # Restarting containers count too, so a crash-looping one gets removed.
    ctrs = containers.list_containers_for_service(
        svc.name, project=project, statuses=("running", "restarting")
    )
    if ctrs is None:
        # Can't see what started, so can't remove it: not a clean failure.
        log.failure(f"could not list {svc.name} containers after starting them")
        log.service_end()
        return SVC_DIRTY
    if result.returncode != 0 or len(ctrs) != replicas:
        detail = result.stderr.strip() or f"{len(ctrs)} of {replicas} containers running"
        return fail(f"start failed: {detail}", ctrs)

    if svc.healthcheck_skip:
        log.step("healthcheck skipped (deploy.healthcheck.skip=true)")
    else:
        log.step(f"waiting for health check (timeout: {svc.healthcheck_timeout}s)...")
        for ctr in ctrs:
            if not _wait_for_healthy(ctr["ID"], svc.healthcheck_timeout, svc.healthcheck_poll):
                return fail(f"{svc.name} unhealthy; stopping and removing it", ctrs)

    log.success(f"{svc.name} started ({time.time() - svc_start:.1f}s)")
    log.service_end()
    return SVC_OK


def _remove_all(ctrs: list[dict], timeout: int) -> bool:
    """Stop and remove ``ctrs``. Returns False if any couldn't be."""
    ok = True
    for ctr in ctrs:
        if not containers.stop_container(ctr["ID"], timeout=timeout):
            log.failure(f"could not stop {ctr['ID'][:7]}")
            ok = False
        if not containers.remove_container(ctr["ID"]):
            log.failure(f"could not remove {ctr['ID'][:7]}")
            ok = False
    return ok


def _restore_drained(drained: list[Drained], compose_cmd: list[str]) -> bool:
    """Bring drained services back as they were, in deploy (start) order.

    Returns False if any couldn't be.
    """
    ok = True
    for record in sorted(drained, key=lambda r: (r.svc.order, r.svc.file_order)):
        name = record.svc.name
        if not record.replicas:
            log.step(f"{name} was not running before the deploy; leaving it stopped")
            continue
        if record.old_tag is None:
            log.failure(
                f"cannot restore {name}: no tag in its previous image "
                f"({record.old_image or 'unknown'}); start it manually"
            )
            ok = False
            continue
        log.step(f"restoring {name} on {record.old_tag} ({record.replicas} replica(s))...")
        result = compose.compose_run(
            ["up", "-d", "--no-deps", "--scale", f"{name}={record.replicas}", name],
            env={"DEPLOY_TAG": record.old_tag},
            cmd=compose_cmd,
        )
        if result.returncode != 0:
            log.failure(f"could not restore {name}: {result.stderr.strip()}")
            ok = False
    return ok


def _deploy_service(
    svc: config.ServiceConfig, tag: str, compose_cmd: list[str], project: str = ""
) -> int:
    """Deploy a single service with a rolling cutover.

    Returns SVC_OK, SVC_FAILED (old container still serving, nothing left
    behind), or SVC_DIRTY (failed, and cleaning up after itself failed too).
    """
    log.service_start(svc.name)
    svc_start = time.time()

    env = {"DEPLOY_TAG": tag}

    def end(code: int) -> int:
        log.service_end()
        return code

    # 1. Pull
    if not _pull(svc, env, compose_cmd):
        return end(SVC_FAILED)

    # 2. Scale to 2
    log.step("starting new container...")
    result = compose.compose_run(
        ["up", "-d", "--no-deps", "--no-recreate", "--scale", f"{svc.name}=2", svc.name],
        env=env,
        cmd=compose_cmd,
    )
    if result.returncode != 0:
        # As on master: fail without issuing further compose commands.
        log.failure(f"scale up failed: {result.stderr.strip()}")
        return end(SVC_FAILED)

    # 3. Get containers, identify old vs new
    ctrs = containers.get_containers_for_service(svc.name, project=project)
    if len(ctrs) != 2:
        ctr_details = ", ".join(f"{c.get('ID', '?')[:12]} ({c.get('Image', '?')})" for c in ctrs)
        log.failure(f"Expected 2 containers for {svc.name}, found {len(ctrs)}: [{ctr_details}]")
        log.step(
            f"Run `docker ps --filter label=com.docker.compose.service={svc.name}` "
            "to inspect running containers, then remove extras and retry."
        )
        return end(SVC_FAILED if _scale_back(svc.name, env, compose_cmd) else SVC_DIRTY)

    old, new = containers.identify_old_new(ctrs, tag)
    if old is None or new is None:
        log.failure("Could not identify old/new containers")
        return end(SVC_FAILED if _scale_back(svc.name, env, compose_cmd) else SVC_DIRTY)

    new_id = new["ID"]
    old_id = old["ID"]

    # 4. Wait for health check (skip if deploy.healthcheck.skip)
    if svc.healthcheck_skip:
        log.step("healthcheck skipped (deploy.healthcheck.skip=true)")
        healthy = True
    else:
        log.step(f"waiting for health check (timeout: {svc.healthcheck_timeout}s)...")
        healthy = _wait_for_healthy(new_id, svc.healthcheck_timeout, svc.healthcheck_poll)

    if healthy:
        if not svc.healthcheck_skip:
            health_elapsed = time.time() - svc_start
            log.step(f"healthy ({health_elapsed:.1f}s)")

        # 5a. Cutover: stop old, remove old, scale back
        log.step(f"draining old container ({old_id[:7]}, {svc.drain}s timeout)...")
        clean = containers.stop_container(old_id, timeout=svc.drain)
        clean = containers.remove_container(old_id) and clean
        clean = _scale_back(svc.name, env, compose_cmd) and clean
        if not clean:
            # The new container is healthy and serving, so the deploy
            # succeeded, as on master. A stop/rm failure here is usually the
            # old container having already exited; failing would revert the
            # repo underneath a healthy new container.
            log.step(f"warning: cleanup of old container {old_id[:7]} did not complete")

        elapsed = time.time() - svc_start
        log.success(f"{svc.name} deployed ({elapsed:.1f}s)")
        return end(SVC_OK)
    else:
        # 5b. Abort: stop new, remove new, scale back
        log.step(f"aborting: stopping new container ({new_id[:7]})...")
        clean = containers.stop_container(new_id)
        clean = containers.remove_container(new_id) and clean
        clean = _scale_back(svc.name, env, compose_cmd) and clean
        if clean:
            log.step("aborted, old container still serving")
        else:
            log.step(f"abort incomplete: new container {new_id[:7]} may still be running")
        log.failure(f"{svc.name} FAILED")
        return end(SVC_FAILED if clean else SVC_DIRTY)


def _wait_for_healthy(container_id: str, timeout: int, poll_interval: int) -> bool:
    """Poll docker inspect for container health. Returns True if healthy."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = containers.get_container_health(container_id)
        if status == "healthy":
            return True
        if status == "unhealthy":
            return False
        time.sleep(poll_interval)
    return False


def _scale_back(service_name: str, env: dict, compose_cmd: list[str]) -> bool:
    """Scale service back to 1. Returns False if compose failed.

    --no-recreate: scale back only ever removes extras. Without it, compose
    "fixes" the surviving container to match ``env``'s DEPLOY_TAG, so after a
    failed health check the old, healthy container was recreated on the new,
    failing image (master did this; found by the integration suite).
    """
    result = compose.compose_run(
        ["up", "-d", "--no-deps", "--no-recreate", "--scale", f"{service_name}=1", service_name],
        env=env,
        cmd=compose_cmd,
    )
    if result.returncode != 0:
        log.failure(f"scale back failed: {result.stderr.strip()}")
        return False
    return True


def _dry_run(tag: str, services: list[config.ServiceConfig]) -> None:
    """Show what would happen without executing."""
    log.header("deploy (dry-run)")
    log.info(f"tag: {tag}")
    log.info(f"services: {', '.join(s.name for s in services)}")
    log.info("")
    drain_first = [s for s in services if s.drains_first]
    for svc in reversed(drain_first):
        log.service_start(svc.name)
        image_name = (svc.image or svc.name).rsplit(":", 1)[0]
        log.step(f"would pull {image_name}:{tag}")
        log.step(f"would drain running containers first ({svc.drain}s timeout)")
        log.service_end()
    for svc in services:
        if svc.drains_first:
            continue
        log.service_start(svc.name)
        image_name = (svc.image or svc.name).rsplit(":", 1)[0]
        log.step(f"would pull {image_name}:{tag}")
        log.step(f"would scale to 2, health check (timeout: {svc.healthcheck_timeout}s)")
        log.step(f"would drain old container ({svc.drain}s timeout)")
        log.step("would scale back to 1")
        log.service_end()
    for svc in drain_first:
        log.service_start(svc.name)
        log.step(f"would start on {tag}, health check (timeout: {svc.healthcheck_timeout}s)")
        log.service_end()
    log.footer("dry-run complete")
