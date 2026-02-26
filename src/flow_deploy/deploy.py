"""Core deploy algorithm + rollback."""

import signal
import time

from flow_deploy import compose, config, containers, lock, log, tags


def deploy(
    tag: str | None = None,
    services_filter: list[str] | None = None,
    dry_run: bool = False,
    cmd: list[str] | None = None,
) -> int:
    """Perform a rolling deploy. Returns exit code (0=success, 1=failure, 2=locked)."""
    compose_cmd = cmd or compose.resolve_command()

    # Parse compose config
    try:
        compose_dict = compose.compose_config(cmd=compose_cmd)
    except RuntimeError as e:
        log.error(str(e))
        return 1

    all_services = config.parse_services(compose_dict)
    app_services = [s for s in all_services if s.is_app]

    if services_filter:
        app_services = [s for s in app_services if s.name in services_filter]

    if not app_services:
        log.error("No app services to deploy")
        return 1

    # Validate healthchecks
    missing = config.validate_healthchecks(app_services)
    if missing:
        log.error(f"Services missing healthcheck: {', '.join(missing)}")
        return 1

    # Determine tag
    if tag is None:
        # Use whatever is in compose config (no override)
        tag = tags.current_tag() or "latest"

    if dry_run:
        _dry_run(tag, app_services)
        return 0

    # Acquire lock
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

    try:
        service_names = ", ".join(s.name for s in app_services)
        log.header("deploy")
        log.info(f"tag: {tag}")
        log.info(f"services: {service_names}")
        log.info("")

        start_time = time.time()

        for svc in app_services:
            result = _deploy_service(svc, tag, compose_cmd)
            if result != 0:
                log.info("")
                log.footer("FAILED (deploy aborted)")
                lock.release()
                return 1

        elapsed = time.time() - start_time
        tags.write_tag(tag)

        log.info("")
        log.footer(f"complete ({elapsed:.1f}s)")
    finally:
        lock.release()
        signal.signal(signal.SIGTERM, _original_sigterm)
        signal.signal(signal.SIGINT, _original_sigint)

    return 0


def _deploy_service(svc: config.ServiceConfig, tag: str, compose_cmd: list[str]) -> int:
    """Deploy a single service. Returns 0 on success, 1 on failure."""
    log.service_start(svc.name)
    svc_start = time.time()

    env = {"DEPLOY_TAG": tag}

    # 1. Pull
    log.step(f"pulling {svc.image or svc.name}:{tag}...")
    pull_start = time.time()
    result = compose.compose_run(["pull", svc.name], env=env, cmd=compose_cmd)
    if result.returncode != 0:
        log.failure(f"pull failed: {result.stderr.strip()}")
        log.service_end()
        return 1
    log.step(f"pulled ({time.time() - pull_start:.1f}s)")

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
    ctrs = containers.get_containers_for_service(svc.name)
    if len(ctrs) != 2:
        log.failure(f"Expected 2 containers, found {len(ctrs)}")
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

    # 4. Wait for health check
    log.step(f"waiting for health check (timeout: {svc.healthcheck_timeout}s)...")
    healthy = _wait_for_healthy(new_id, svc.healthcheck_timeout, svc.healthcheck_poll)

    if healthy:
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
        # 5b. Rollback: stop new, remove new, scale back
        log.step(f"rolling back: stopping new container ({new_id[:7]})...")
        containers.stop_container(new_id)
        containers.remove_container(new_id)
        _scale_back(svc.name, env, compose_cmd)
        log.step("rollback complete, old container still serving")
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
    for svc in services:
        log.service_start(svc.name)
        log.step(f"would pull {svc.image or svc.name}:{tag}")
        log.step(f"would scale to 2, health check (timeout: {svc.healthcheck_timeout}s)")
        log.step(f"would drain old container ({svc.drain}s timeout)")
        log.step("would scale back to 1")
        log.service_end()
    log.footer("dry-run complete")


def rollback(
    services_filter: list[str] | None = None,
    cmd: list[str] | None = None,
) -> int:
    """Rollback to the previous tag. Returns exit code."""
    prev = tags.previous_tag()
    if prev is None:
        log.error("No previous tag to rollback to")
        return 1

    log.info(f"Rolling back to tag: {prev}")
    return deploy(tag=prev, services_filter=services_filter, cmd=cmd)
