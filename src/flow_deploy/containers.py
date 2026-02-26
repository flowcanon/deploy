"""Docker inspect, identify old/new, stop/rm."""

import json

from flow_deploy import process


def get_containers_for_service(service: str, project: str = "") -> list[dict]:
    """Get running containers for a service via docker ps.

    Returns list of dicts with keys: ID, Image, CreatedAt, State, Health.
    """
    filters = ["--filter", f"label=com.docker.compose.service={service}"]
    if project:
        filters += ["--filter", f"label=com.docker.compose.project={project}"]
    filters += ["--filter", "status=running"]

    result = process.run(
        ["docker", "ps"] + filters + ["--format", "{{json .}}"]
    )
    if result.returncode != 0:
        return []

    containers = []
    for line in result.stdout.strip().splitlines():
        if line.strip():
            containers.append(json.loads(line))
    return containers


def identify_old_new(containers: list[dict], new_tag: str) -> tuple[dict | None, dict | None]:
    """Identify old and new containers by image tag.

    For same-tag redeploys, falls back to CreatedAt (newest = new).
    Returns (old, new) or (None, None) if can't determine.
    """
    if len(containers) != 2:
        return None, None

    a, b = containers

    a_image = a.get("Image", "")
    b_image = b.get("Image", "")

    # Different tags — match by tag
    if new_tag and a_image.endswith(f":{new_tag}") and not b_image.endswith(f":{new_tag}"):
        return b, a
    if new_tag and b_image.endswith(f":{new_tag}") and not a_image.endswith(f":{new_tag}"):
        return a, b

    # Same tag or can't determine by tag — use creation time (newer = new container)
    a_created = a.get("CreatedAt", "")
    b_created = b.get("CreatedAt", "")
    if a_created > b_created:
        return b, a
    elif b_created > a_created:
        return a, b

    return None, None


def get_container_health(container_id: str) -> str | None:
    """Get health status of a container via docker inspect.

    Returns 'healthy', 'unhealthy', 'starting', or None.
    """
    result = process.run(
        ["docker", "inspect", "--format", "{{.State.Health.Status}}", container_id]
    )
    if result.returncode != 0:
        return None
    status = result.stdout.strip()
    return status if status else None


def stop_container(container_id: str, timeout: int = 30) -> bool:
    """Stop a container with given timeout. Returns True on success."""
    result = process.run(["docker", "stop", "--time", str(timeout), container_id])
    return result.returncode == 0


def remove_container(container_id: str) -> bool:
    """Remove a container. Returns True on success."""
    result = process.run(["docker", "rm", container_id])
    return result.returncode == 0
