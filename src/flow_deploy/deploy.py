"""Core deploy algorithm."""

import signal
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field

from flow_deploy import compose, config, containers, deployed, git, lock, log

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
class Previous:
    """What an app service was running when the deploy started."""

    svc: config.ServiceConfig
    replicas: int = 0
    image_id: str = ""  # the image its containers ran (the most common, if mixed)
    image_ref: str = ""  # how docker named it: repo:tag, repo@digest, ...
    restore_tag: str | None = None  # the git tag or SHA that brings image_id back


@dataclass
class _Run:
    """What a deploy has changed so far, so any failure can put it back."""

    compose_cmd: list[str]
    project: str = ""
    previous_sha: str | None = None
    previous: dict[str, Previous] = field(default_factory=dict)
    drained: list[str] = field(default_factory=list)  # drain-first services touched
    shipped: list[str] = field(default_factory=list)  # rolling services deployed or deploying
    aborted: str | None = None  # the rolling service whose own abort kept its old container
    whole_project: bool = True  # every app service is in this deploy

    def revert(self, reason: str) -> int:
        """Put everything back as it was before the deploy.

        Restores the repo, rolls shipped rolling services back to the image
        each was running (newest change first), then brings every drained
        service back on its previous image in deploy order. What was running
        comes from the containers themselves (see _snapshot), not git or
        ``:latest``. Expand-then-contract migrations keep old code compatible
        with the new schema, as rolling deploys already assume. Exits 3 only
        when something can't be put back.
        """
        # Don't let a further signal cut the revert short (or start a second one).
        for signum in HANDLED_SIGNALS:
            signal.signal(signum, signal.SIG_IGN)
        log.info("")
        problems = []
        if self.previous_sha is not None and not git.restore(self.previous_sha):
            problems.append("git restore failed")
        if self.aborted and not _remove_stopped(self.previous[self.aborted].svc, self.project):
            # A new container its abort stopped but couldn't remove: the next
            # deploy's `compose up --scale` would restart it.
            problems.append(f"{self.aborted}'s stopped new container could not be removed")
        for name in reversed(self.shipped):
            if not _roll_back(self.previous[name], self.compose_cmd, self.project):
                problems.append(f"{name} could not be rolled back")
        restore = [self.previous[name] for name in self.drained]
        if not _restore_drained(restore, self.compose_cmd, self.project):
            problems.append("drained services could not be restored")
        self._record_previous_tag()
        log.footer(f"FAILED ({reason})")
        return _degraded(", ".join(problems)) if problems else EXIT_FAILED

    def _record_previous_tag(self) -> None:
        """After a revert, record the previous tag if every service shared one."""
        tags = {p.restore_tag for p in self.previous.values() if p.replicas}
        if self.whole_project and len(tags) == 1 and None not in tags:
            deployed.write(tags.pop(), self.previous_sha or "")
        elif self.previous:
            deployed.clear()


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
            compose_dict = compose.compose_config(cmd=compose_cmd, env={"DEPLOY_TAG": tag})
        except RuntimeError as e:
            log.error(str(e))
            return EXIT_FAILED
        app_services = _resolve_app_services(compose_dict, services_filter)
        if app_services is None:
            return EXIT_FAILED
        _dry_run(tag, app_services)
        return EXIT_OK

    return _deploy_guarded(compose_cmd, tag, tag_provided, services_filter)


def _deploy_guarded(
    compose_cmd: list[str], tag: str, tag_provided: bool, services_filter: list[str] | None
) -> int:
    """The deploy under its lock and signal handlers; always reverts on failure.

    The run is logged to .git/flow-deploy.log while the lock is held, so a
    deploy that's locked out never touches the log of the one running.
    """
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
    code = EXIT_FAILED
    try:
        # Acquire lock before any mutations (git checkout, container changes)
        locked = lock.acquire()
        if not locked:
            lock_info = lock.read_lock()
            pid = lock_info["pid"] if lock_info else "unknown"
            log.error(f"Deploy lock held by PID {pid}")
            return EXIT_LOCKED
        log.open_run(
            f"deploy --tag {tag}" + "".join(f" --service {s}" for s in services_filter or [])
        )
        code = _deploy_locked(run, tag, tag_provided, services_filter)
        return code
    except (Exception, Interrupted) as exc:
        if isinstance(exc, Interrupted):
            reason = f"interrupted by signal {exc.signum}"
        else:
            reason = f"unexpected error: {exc!r}"
        log.error(f"{reason}; reverting")
        code = run.revert(reason)
        return code
    finally:
        # Ignore signals first, so one landing here can't skip the rest.
        for signum in HANDLED_SIGNALS:
            signal.signal(signum, signal.SIG_IGN)
        # Always released: a failed deploy puts things back rather than
        # leaving state for someone to clean up by hand.
        if locked:
            log.close_run(code)
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

    # Parse compose config (now reading from the target commit), rendered with
    # this deploy's tag so each image shows what will run.
    try:
        compose_dict = compose.compose_config(cmd=run.compose_cmd, env={"DEPLOY_TAG": tag})
    except RuntimeError as e:
        log.error(str(e))
        return EXIT_FAILED if git.restore(run.previous_sha) else _degraded("git restore failed")

    app_services = _resolve_app_services(compose_dict, services_filter)
    if app_services is None:
        return EXIT_FAILED if git.restore(run.previous_sha) else _degraded("git restore failed")
    all_apps = [s for s in config.parse_services(compose_dict) if s.is_app]
    run.whole_project = len(app_services) == len(all_apps)

    service_names = ", ".join(s.name for s in app_services)
    log.header("deploy")
    log.info(f"tag: {tag}")
    log.info(f"services: {service_names}")
    log.info("")
    _warn_untagged_images(app_services, tag)

    run.project = compose_dict.get("name", "")
    project = run.project
    compose_cmd = run.compose_cmd
    start_time = time.time()

    # A release that renames the compose project would start a second stack
    # beside the running one, and snapshot (so revert) nothing.
    others = containers.other_projects_here(project, [s.name for s in app_services])
    if others is None:
        log.error("could not list this directory's containers; nothing was changed")
    elif others:
        log.error(
            f"compose project(s) {', '.join(sorted(others))} run this deploy's services "
            f"in this directory, but this commit's project is {project or '(unnamed)'}; "
            "a project rename can't be deployed in place. Nothing was changed."
        )
    if others is None or others:
        return EXIT_FAILED if git.restore(run.previous_sha) else _degraded("git restore failed")

    # Before changing anything, record what each service is running, so any
    # failure can put back exactly that image.
    if not _snapshot(run, app_services, tag):
        return EXIT_FAILED if git.restore(run.previous_sha) else _degraded("git restore failed")

    # app_services is in deploy.order. Services start in that order and stop
    # in reverse, so a worker comes up after the web and API it depends on,
    # and drains before them.
    drain_first = [s for s in app_services if s.drains_first]
    rolling = [s for s in app_services if not s.drains_first]
    dirty = []  # services whose own failure cleanup also failed

    def failed(reason: str) -> int:
        code = run.revert(reason)
        if dirty and code == EXIT_FAILED:
            return _degraded(f"cleanup failed for {', '.join(dirty)}")
        return code

    # Phase 1: drain-first services stop, in reverse deploy order, before
    # anything rolls, so none of their old code runs beside new code.
    for svc in reversed(drain_first):
        if _drain_service(svc, tag, compose_cmd, run.drained, project=project) != 0:
            return failed("deploy aborted")

    # Phase 2: rolling services, in deploy order, as before.
    for svc in rolling:
        # Recorded before it's touched: a signal mid-service (during the
        # health wait, say) then rolls it back like any shipped service,
        # which keeps an old container that's still running.
        run.shipped.append(svc.name)
        result = _deploy_service(svc, tag, compose_cmd, project=project)
        if result == SVC_DIRTY:
            dirty.append(svc.name)
        if result != SVC_OK and run.previous[svc.name].replicas:
            run.shipped.pop()
            run.aborted = svc.name
            return failed("deploy aborted")  # its abort left the old container serving
        if result != SVC_OK:
            # Nothing ran before, so whatever the failed rollout left goes too.
            return failed("deploy aborted")

    # Phase 3: drain-first services start in deploy order, at the replica
    # count they ran. One that wasn't running (just added in this release,
    # or crashed) starts at 1: a deploy brings its services up. Every one is
    # attempted, so one failure doesn't strand the rest; any failure reverts
    # the whole deploy.
    failures = []
    for svc in drain_first:
        replicas = max(run.previous[svc.name].replicas, 1)
        result = _start_service(svc, tag, compose_cmd, replicas, project=project)
        if result == SVC_DIRTY:
            dirty.append(svc.name)
        if result != SVC_OK:
            failures.append(svc.name)
    if failures:
        return failed(f"{', '.join(failures)} failed to start")

    if run.whole_project:
        deployed.write(tag, git.current_sha())
    else:
        # Services now run different tags; no one tag describes the host.
        deployed.clear()
        log.warning(
            "--service deployed only some app services; manual compose commands "
            "on this host need DEPLOY_TAG set explicitly until a full deploy"
        )

    elapsed = time.time() - start_time

    log.info("")
    log.info(f"HEAD detached at {tag}")
    log.footer(f"complete ({elapsed:.1f}s)")
    return EXIT_OK


def _warn_untagged_images(services: list[config.ServiceConfig], tag: str) -> None:
    """Warn about app images that don't follow DEPLOY_TAG.

    Such an image deploys whatever its fixed reference names (often
    ``:latest``, possibly an unreleased build), and a revert can't target the
    previous version.
    """
    for svc in services:
        if svc.image and containers.image_tag(svc.image) != tag:
            log.warning(
                f"{svc.name}'s image ({svc.image}) doesn't use DEPLOY_TAG, so this "
                "deploy runs whatever it names and a revert can't target the previous "
                "version. Name it repo:${DEPLOY_TAG:-latest}."
            )


def _snapshot(run: _Run, services: list[config.ServiceConfig], tag: str) -> bool:
    """Record what each service is running on ``run.previous``. False if Docker can't say.

    Each service gets the DEPLOY_TAG that brings its running image back: see
    _restore_tag.
    """
    for svc in services:
        ctrs = containers.list_containers_for_service(
            svc.name, project=run.project, statuses=("running", "restarting")
        )
        ids = containers.image_ids([c["ID"] for c in ctrs]) if ctrs is not None else None
        if ids is None:
            log.error(f"could not inspect {svc.name}'s containers; nothing was changed")
            return False
        prev = Previous(svc, replicas=len(ctrs))
        if ctrs:
            counts = Counter(ids[c["ID"]] for c in ctrs)
            prev.image_id = counts.most_common(1)[0][0]
            prev.image_ref = next(c.get("Image", "") for c in ctrs if ids[c["ID"]] == prev.image_id)
            if len(counts) > 1:
                log.warning(
                    f"{svc.name} is running {len(counts)} different images; a revert "
                    f"restores the most common, {prev.image_ref}"
                )
            prev.restore_tag = _restore_tag(prev, run.previous_sha, tag)
        run.previous[svc.name] = prev
    return True


def _restore_tag(prev: Previous, previous_sha: str | None, deploy_tag: str) -> str | None:
    """The DEPLOY_TAG a revert brings ``prev.svc`` back with: a git tag or commit SHA.

    The tag the containers were started from, when it's a ref in this repo (a
    release tag or a SHA) and still names the image they run. Not when it's
    the tag being deployed: this deploy pulls that, which moves it if it was
    re-pushed. Otherwise (``:latest``, a tag re-pushed since, a digest, no
    tag, a redeploy of the same tag) the commit that was checked out before
    the deploy, whose SHA CI pushes every image under.
    """
    tag = containers.image_tag(prev.image_ref)
    # Check repo:tag itself, which is what a revert renders: a ref like
    # repo:v1@sha256:... names the digest, whatever repo:v1 says now.
    named = f"{containers.image_repo(prev.image_ref)}:{tag}"
    if (
        tag
        and tag != deploy_tag
        and git.is_ref(tag)
        and containers.local_image_id(named) == prev.image_id
    ):
        return tag
    if previous_sha == deploy_tag:
        # Redeploying the commit that's running: git has no other name for the
        # build running now, so a revert restores this commit as pulled now.
        # (master didn't roll shipped services back at all.)
        log.warning(
            f"redeploying {deploy_tag}: if {prev.svc.name}'s image under it was rebuilt, "
            "a revert restores the rebuild, not the build running now"
        )
    elif previous_sha:
        log.info(
            f"note: {prev.svc.name} runs {prev.image_ref or prev.image_id[:19]}; a revert "
            f"would restore it by the previous commit, {previous_sha[:12]}"
        )
    return previous_sha


def _stop_reporting(name: str, ctr_id: str, timeout: int, isolate: bool = False) -> bool:
    """docker stop, then warn if the container had to be killed.

    A container that ignores SIGTERM waits out the whole timeout and is
    SIGKILLed (exit 137), dropping whatever it was doing. Usually PID 1 is
    a shell that doesn't forward signals. Reported, never fatal.
    """
    started = time.time()
    if not containers.stop_container(ctr_id, timeout=timeout, isolate=isolate):
        return False
    took = time.time() - started
    code = containers.exit_code(ctr_id)
    if code == 137 or (timeout > 0 and took >= timeout):
        log.warning(
            f"{name} ({ctr_id[:7]}) didn't exit on SIGTERM within {timeout}s and was "
            "killed; make sure its PID 1 handles SIGTERM (an exec-form CMD, or exec "
            "in its entrypoint script)"
        )
    return True


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


def _drain_service(
    svc: config.ServiceConfig,
    tag: str,
    compose_cmd: list[str],
    drained: list[str],
    project: str = "",
) -> int:
    """Pull the new image, then stop and remove the running containers.

    Pulls first so an unpullable tag fails before anything stops. Adds the
    service to ``drained`` *before* stopping anything, so a signal mid-drain
    (a drain can take as long as the service's longest task) still restores
    it. Returns 1 if the pull failed or any container failed to stop or be
    removed: the caller must not roll anything while old code may still be
    running.
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
    drained.append(svc.name)
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
            stopped = _stop_reporting(svc.name, ctr_id, svc.drain, isolate=True)
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


def _remove_stopped(svc: config.ServiceConfig, project: str, keep_image: str | None = None) -> bool:
    """Remove ``svc``'s stopped leftovers (a stop that worked, then an rm that didn't).

    `compose up --scale` starts stopped containers before creating new ones,
    so a revert would otherwise bring back whatever image a leftover has.
    Leftovers on ``keep_image`` are kept: restarting one restores that image.
    """
    stopped = containers.list_containers_for_service(
        svc.name, project=project, statuses=("created", "exited", "dead")
    )
    ids = containers.image_ids([c["ID"] for c in stopped]) if stopped is not None else None
    if ids is None:
        log.failure(f"cannot revert {svc.name}: its stopped containers can't be inspected")
        return False
    ok = True
    for ctr in [c for c in stopped if keep_image is None or ids[c["ID"]] != keep_image]:
        if not containers.remove_container(ctr["ID"]):
            log.failure(f"could not remove stopped {svc.name} container {ctr['ID'][:7]}")
            ok = False
    return ok


def _roll_back(prev: Previous, compose_cmd: list[str], project: str) -> bool:
    """Put a shipped rolling service back on the image it was running.

    A rolling deploy of the previous image (already local, so no pull): no
    downtime. A service that had no container before the deploy goes back to
    having none.
    """
    svc = prev.svc
    if not _remove_stopped(svc, project):
        return False
    ctrs = containers.list_containers_for_service(
        svc.name, project=project, statuses=("running", "restarting")
    )
    ids = containers.image_ids([c["ID"] for c in ctrs]) if ctrs is not None else None
    if ids is None:
        log.failure(f"cannot roll {svc.name} back: its containers can't be inspected")
        return False
    if not prev.replicas:
        log.step(f"{svc.name} had no container before the deploy; removing the new one")
        return _remove_all(ctrs, timeout=svc.drain)
    newer = [c for c in ctrs if ids[c["ID"]] != prev.image_id]
    if len(newer) < len(ctrs):
        # The cutover's cleanup failed, so the previous image still runs beside
        # the new one. A rolling deploy can't tell them apart by tag here.
        log.step(f"{svc.name} still runs its previous image; removing the new container(s)")
        return _remove_all(newer, timeout=svc.drain) and _scale_to_previous(
            prev, compose_cmd, project
        )
    log.step(f"rolling {svc.name} back to {prev.restore_tag}...")
    return (
        _deploy_service(svc, prev.restore_tag, compose_cmd, project=project, pull=False) == SVC_OK
    )


def _restore_drained(previous: list[Previous], compose_cmd: list[str], project: str) -> bool:
    """Bring drained services back to what they ran, in deploy (start) order.

    Drains any new containers first (with each service's drain timeout), then
    starts the previous image at its previous replica count. Returns False if
    any couldn't be put back.
    """
    ok = True
    for prev in sorted(previous, key=lambda p: (p.svc.order, p.svc.file_order)):
        svc = prev.svc
        if not _remove_stopped(svc, project, keep_image=prev.image_id):
            ok = False
            continue
        ctrs = containers.list_containers_for_service(
            svc.name, project=project, statuses=("running", "restarting")
        )
        ids = containers.image_ids([c["ID"] for c in ctrs]) if ctrs is not None else None
        if ids is None:
            log.failure(f"cannot restore {svc.name}: its containers can't be inspected")
            ok = False
            continue
        newer = [c for c in ctrs if ids[c["ID"]] != prev.image_id]
        if newer:
            log.step(f"draining {len(newer)} new {svc.name} container(s)...")
            if not _remove_all(newer, timeout=svc.drain):
                ok = False
                continue
        if not prev.replicas:
            log.step(f"{svc.name} was not running before the deploy; leaving it stopped")
            continue
        log.step(f"restoring {svc.name} on {prev.restore_tag} ({prev.replicas} replica(s))...")
        ok = _scale_to_previous(prev, compose_cmd, project) and ok
    return ok


def _scale_to_previous(prev: Previous, compose_cmd: list[str], project: str) -> bool:
    """Bring ``prev.svc`` to its previous replica count on its previous image.

    --no-recreate: containers already on that image are kept, never replaced.
    """
    svc = prev.svc
    result = compose.compose_run(
        ["up", "-d", "--no-deps", "--no-recreate", "--scale", f"{svc.name}={prev.replicas}"]
        + [svc.name],
        env={"DEPLOY_TAG": prev.restore_tag},
        cmd=compose_cmd,
    )
    if result.returncode != 0:
        log.failure(f"could not restore {svc.name}: {result.stderr.strip()}")
        return False
    return _restored_healthy(svc, project, prev.replicas)


def _restored_healthy(svc: config.ServiceConfig, project: str, replicas: int) -> bool:
    """Check a restored service is back at ``replicas`` and passing its health check.

    A revert isn't done until what it put back is serving: exit 3 if not,
    including when a restored container exited straight away.
    """
    ctrs = containers.list_containers_for_service(
        svc.name, project=project, statuses=("running", "restarting")
    )
    if ctrs is None:
        log.failure(f"restored {svc.name} but can't list its containers to check health")
        return False
    if len(ctrs) < replicas:
        log.failure(f"restored {svc.name} but only {len(ctrs)} of {replicas} containers are up")
        return False
    if svc.healthcheck_skip:
        return True
    log.step(f"waiting for restored {svc.name} to be healthy...")
    for ctr in ctrs:
        if not _wait_for_healthy(ctr["ID"], svc.healthcheck_timeout, svc.healthcheck_poll):
            log.failure(f"restored {svc.name} but it isn't healthy ({ctr['ID'][:7]})")
            return False
    return True


def _deploy_service(
    svc: config.ServiceConfig,
    tag: str,
    compose_cmd: list[str],
    project: str = "",
    pull: bool = True,
) -> int:
    """Deploy a single service with a rolling cutover.

    ``pull=False`` for a rollback: the previous image is normally still
    local, and compose pulls it if not. Returns SVC_OK, SVC_FAILED
    (old container still serving, nothing left behind), or SVC_DIRTY
    (failed, and cleaning up after itself failed too).
    """
    log.service_start(svc.name)
    svc_start = time.time()

    env = {"DEPLOY_TAG": tag}

    def end(code: int) -> int:
        log.service_end()
        return code

    # 1. Pull
    if pull and not _pull(svc, env, compose_cmd):
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
        clean = _stop_reporting(svc.name, old_id, svc.drain)
        clean = containers.remove_container(old_id) and clean
        # Scale back only once the old container is gone: with two left, it
        # can only remove one, and compose might pick the new one.
        clean = clean and _scale_back(svc.name, env, compose_cmd)
        if not clean:
            # The new container is healthy and serving, so the deploy
            # succeeded, as on master. A stop/rm failure here is usually the
            # old container having already exited; failing would revert the
            # repo underneath a healthy new container. If it's still running,
            # a later revert keeps it (_roll_back).
            log.step(f"warning: cleanup of old container {old_id[:7]} did not complete")

        elapsed = time.time() - svc_start
        log.success(f"{svc.name} deployed ({elapsed:.1f}s)")
        return end(SVC_OK)
    else:
        # 5b. Abort: stop new, remove new, scale back
        log.step(f"aborting: stopping new container ({new_id[:7]})...")
        clean = containers.stop_container(new_id)
        clean = containers.remove_container(new_id) and clean
        # As above: with the new container still there, scale back might
        # remove the old one instead.
        clean = clean and _scale_back(svc.name, env, compose_cmd)
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
