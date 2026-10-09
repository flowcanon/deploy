"""Core deploy algorithm."""

import signal
import time

from flow_deploy import compose, config, containers, git, lock, log


def deploy(
    tag: str | None = None,
    services_filter: list[str] | None = None,
    dry_run: bool = False,
    cmd: list[str] | None = None,
) -> int:
    """Perform a rolling deploy. Returns exit code (0=success, 1=failure, 2=locked)."""
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
            return 1
        app_services = _resolve_app_services(compose_dict, services_filter)
        if app_services is None:
            return 1
        _dry_run(tag, app_services)
        return 0

    # Acquire lock before any mutations (git checkout, container changes)
    if not lock.acquire():
        lock_info = lock.read_lock()
        pid = lock_info["pid"] if lock_info else "unknown"
        log.error(f"Deploy lock held by PID {pid}")
        return 2

    # Register signal handlers for cleanup
    _original_sigterm = signal.getsignal(signal.SIGTERM)
    _original_sigint = signal.getsignal(signal.SIGINT)

    def _cleanup_handler(signum, frame):
        log.error(f"Received signal {signum}, cleaning up...")
        lock.release()
        raise SystemExit(1)

    signal.signal(signal.SIGTERM, _cleanup_handler)
    signal.signal(signal.SIGINT, _cleanup_handler)

    keep_lock = False
    try:
        # Git pre-flight: dirty check, fetch, checkout detached.
        # Protected by the lock so concurrent deploys can't race on checkout.
        # Must happen before config parsing so we read the compose file
        # from the target commit, not whatever is currently checked out.
        git_code, previous_sha = git.preflight_and_checkout(tag if tag_provided else None)
        if git_code != 0:
            return 1

        # Parse compose config (now reading from the target commit)
        try:
            compose_dict = compose.compose_config(cmd=compose_cmd)
        except RuntimeError as e:
            log.error(str(e))
            keep_lock = not git.restore(previous_sha)
            return 1

        app_services = _resolve_app_services(compose_dict, services_filter)
        if app_services is None:
            keep_lock = not git.restore(previous_sha)
            return 1

        service_names = ", ".join(s.name for s in app_services)
        log.header("deploy")
        log.info(f"tag: {tag}")
        log.info(f"services: {service_names}")
        log.info("")

        project = compose_dict.get("name", "")
        start_time = time.time()

        drain_first = [s for s in app_services if s.drains_first]
        rolling = [s for s in app_services if not s.drains_first]

        # Phase 1: drain-first services stop before anything rolls, so none of
        # their old code runs beside new code or after new migrations. Each
        # one's previous image tag is kept to bring it back if the deploy fails.
        drained: list[tuple[config.ServiceConfig, str | None]] = []
        for svc in drain_first:
            result, old_tag = _drain_service(svc, tag, compose_cmd, project=project)
            if result != 0:
                log.info("")
                keep_lock = not git.restore(previous_sha)
                _restore_drained(drained, compose_cmd)
                log.footer("FAILED (deploy aborted)")
                return 1
            drained.append((svc, old_tag))

        # Phase 2: rolling services, as before.
        for svc in rolling:
            result = _deploy_service(svc, tag, compose_cmd, project=project)
            if result != 0:
                log.info("")
                keep_lock = not git.restore(previous_sha)
                _restore_drained(drained, compose_cmd)
                log.footer("FAILED (deploy aborted)")
                return 1

        # Phase 3: drain-first services start on the new tag. There's no
        # rollback past this point: the old code would now run against
        # whatever the rolling phase changed, so a failure leaves the service
        # down for an operator.
        for svc in drain_first:
            result = _start_service(svc, tag, compose_cmd, project=project)
            if result != 0:
                log.info("")
                keep_lock = not git.restore(previous_sha)
                log.footer(f"FAILED ({svc.name} is down; rolling services deployed)")
                return 1

        elapsed = time.time() - start_time

        log.info("")
        log.info(f"HEAD detached at {tag}")
        log.footer(f"complete ({elapsed:.1f}s)")
    finally:
        if keep_lock:
            log.error(
                "Lock retained — git restore failed, manual intervention required. "
                "Run: git checkout --detach <sha> && rm .git/deploy-lock"
            )
        else:
            lock.release()
        signal.signal(signal.SIGTERM, _original_sigterm)
        signal.signal(signal.SIGINT, _original_sigint)

    return 0


def _resolve_app_services(
    compose_dict: dict, services_filter: list[str] | None
) -> list[config.ServiceConfig] | None:
    """Parse and validate app services from compose config. Returns None on error."""
    all_services = config.parse_services(compose_dict)
    app_services = [s for s in all_services if s.is_app]

    if services_filter:
        app_services = [s for s in app_services if s.name in services_filter]

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
    """The tag of ``registry/name:tag``, or None when the image has no tag."""
    last = image.rsplit("/", 1)[-1]
    return last.rsplit(":", 1)[1] if ":" in last else None


def _drain_service(
    svc: config.ServiceConfig, tag: str, compose_cmd: list[str], project: str = ""
) -> tuple[int, str | None]:
    """Pull the new image, then stop and remove the running containers.

    Pulls first so an unpullable tag fails before anything stops. Returns
    (exit code, the old containers' image tag or None if none were running).
    """
    log.service_start(svc.name)
    svc_start = time.time()
    env = {"DEPLOY_TAG": tag}

    if not _pull(svc, env, compose_cmd):
        log.service_end()
        return 1, None

    ctrs = containers.get_containers_for_service(svc.name, project=project)
    old_tag = _image_tag(ctrs[0].get("Image", "")) if ctrs else None
    if not ctrs:
        log.step("not running; nothing to drain")
    for ctr in ctrs:
        ctr_id = ctr["ID"]
        log.step(f"draining {ctr_id[:7]} ({svc.drain}s timeout)...")
        containers.stop_container(ctr_id, timeout=svc.drain)
        containers.remove_container(ctr_id)

    log.success(f"{svc.name} drained ({time.time() - svc_start:.1f}s)")
    log.service_end()
    return 0, old_tag


def _start_service(
    svc: config.ServiceConfig, tag: str, compose_cmd: list[str], project: str = ""
) -> int:
    """Start a drained service on the new tag and wait for it to be healthy."""
    log.service_start(svc.name)
    svc_start = time.time()
    env = {"DEPLOY_TAG": tag}

    log.step("starting new container...")
    result = compose.compose_run(["up", "-d", "--no-deps", svc.name], env=env, cmd=compose_cmd)
    ctrs = containers.get_containers_for_service(svc.name, project=project)
    if result.returncode != 0 or len(ctrs) != 1:
        log.failure(f"start failed: {result.stderr.strip() or f'{len(ctrs)} containers'}")
        log.service_end()
        return 1

    if svc.healthcheck_skip:
        log.step("healthcheck skipped (deploy.healthcheck.skip=true)")
    else:
        log.step(f"waiting for health check (timeout: {svc.healthcheck_timeout}s)...")
        if not _wait_for_healthy(ctrs[0]["ID"], svc.healthcheck_timeout, svc.healthcheck_poll):
            log.failure(f"{svc.name} unhealthy; left running for inspection")
            log.service_end()
            return 1

    log.success(f"{svc.name} started ({time.time() - svc_start:.1f}s)")
    log.service_end()
    return 0


def _restore_drained(
    drained: list[tuple[config.ServiceConfig, str | None]], compose_cmd: list[str]
) -> None:
    """Best-effort: bring drained services back on the tag they were running."""
    for svc, old_tag in drained:
        if old_tag is None:
            log.step(f"{svc.name} was not running before the deploy; leaving it stopped")
            continue
        log.step(f"restoring {svc.name} on {old_tag}...")
        result = compose.compose_run(
            ["up", "-d", "--no-deps", svc.name], env={"DEPLOY_TAG": old_tag}, cmd=compose_cmd
        )
        if result.returncode != 0:
            log.failure(f"could not restore {svc.name}: {result.stderr.strip()}")


def _deploy_service(
    svc: config.ServiceConfig, tag: str, compose_cmd: list[str], project: str = ""
) -> int:
    """Deploy a single service. Returns 0 on success, 1 on failure."""
    log.service_start(svc.name)
    svc_start = time.time()

    env = {"DEPLOY_TAG": tag}

    # 1. Pull
    if not _pull(svc, env, compose_cmd):
        log.service_end()
        return 1

    # 2. Scale to 2
    log.step("starting new container...")
    result = compose.compose_run(
        ["up", "-d", "--no-deps", "--no-recreate", "--scale", f"{svc.name}=2", svc.name],
        env=env,
        cmd=compose_cmd,
    )
    if result.returncode != 0:
        log.failure(f"scale up failed: {result.stderr.strip()}")
        log.service_end()
        return 1

    # 3. Get containers, identify old vs new
    ctrs = containers.get_containers_for_service(svc.name, project=project)
    if len(ctrs) != 2:
        ctr_details = ", ".join(f"{c.get('ID', '?')[:12]} ({c.get('Image', '?')})" for c in ctrs)
        log.failure(f"Expected 2 containers for {svc.name}, found {len(ctrs)}: [{ctr_details}]")
        log.step(
            f"Run `docker ps --filter label=com.docker.compose.service={svc.name}` "
            "to inspect running containers, then remove extras and retry."
        )
        _scale_back(svc.name, env, compose_cmd)
        log.service_end()
        return 1

    old, new = containers.identify_old_new(ctrs, tag)
    if old is None or new is None:
        log.failure("Could not identify old/new containers")
        _scale_back(svc.name, env, compose_cmd)
        log.service_end()
        return 1

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
        containers.stop_container(old_id, timeout=svc.drain)
        containers.remove_container(old_id)
        _scale_back(svc.name, env, compose_cmd)

        elapsed = time.time() - svc_start
        log.success(f"{svc.name} deployed ({elapsed:.1f}s)")
        log.service_end()
        return 0
    else:
        # 5b. Abort: stop new, remove new, scale back
        log.step(f"aborting: stopping new container ({new_id[:7]})...")
        containers.stop_container(new_id)
        containers.remove_container(new_id)
        _scale_back(svc.name, env, compose_cmd)
        log.step("aborted, old container still serving")
        log.failure(f"{svc.name} FAILED")
        log.service_end()
        return 1


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


def _scale_back(service_name: str, env: dict, compose_cmd: list[str]) -> None:
    """Scale service back to 1."""
    compose.compose_run(
        ["up", "-d", "--no-deps", "--scale", f"{service_name}=1", service_name],
        env=env,
        cmd=compose_cmd,
    )


def _dry_run(tag: str, services: list[config.ServiceConfig]) -> None:
    """Show what would happen without executing."""
    log.header("deploy (dry-run)")
    log.info(f"tag: {tag}")
    log.info(f"services: {', '.join(s.name for s in services)}")
    log.info("")
    drain_first = [s for s in services if s.drains_first]
    for svc in drain_first:
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
