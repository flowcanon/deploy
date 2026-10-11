"""Docker inspect, identify old/new, stop/rm."""

import json
import os

from flow_deploy import process


def get_containers_for_service(service: str, project: str = "") -> list[dict]:
    """Get running containers for a service via docker ps.

    Returns list of dicts with keys: ID, Image, CreatedAt, State, Health.
    An empty list if docker ps fails.
    """
    return list_containers_for_service(service, project) or []


def list_containers_for_service(
    service: str, project: str = "", statuses: tuple[str, ...] = ("running",)
) -> list[dict] | None:
    """Like get_containers_for_service, but None when docker ps fails.

    For callers that must not mistake "couldn't list" for "nothing running".
    ``statuses`` are OR'd docker ps status filters. A status filter matches
    stopped containers too (exited, created) without ``--all``.
    """
    filters = ["--filter", f"label=com.docker.compose.service={service}"]
    if project:
        filters += ["--filter", f"label=com.docker.compose.project={project}"]
    for status in statuses:
        filters += ["--filter", f"status={status}"]

    result = process.run(["docker", "ps"] + filters + ["--format", "{{json .}}"])
    if result.returncode != 0:
        return None

    containers = []
    for line in result.stdout.strip().splitlines():
        if line.strip():
            containers.append(json.loads(line))
    return containers


def other_projects_here(project: str) -> set[str] | None:
    """Compose projects other than ``project`` with running containers in this directory.

    Read from compose's working_dir label. None if docker ps fails.
    """
    cwd = os.getcwd()
    dirs = {cwd}
    # The shell's path for the same directory, if it went through a symlink.
    # PWD is inherited, so it only counts when it really is this directory.
    pwd = os.environ.get("PWD", "")
    if pwd and os.path.realpath(pwd) == os.path.realpath(cwd):
        dirs.add(pwd)
    found = set()
    for d in sorted(dirs):
        result = process.run(
            [
                "docker",
                "ps",
                "--filter",
                f"label=com.docker.compose.project.working_dir={d}",
                "--format",
                '{{.Label "com.docker.compose.project"}}',
            ]
        )
        if result.returncode != 0:
            return None
        found |= {line.strip() for line in result.stdout.splitlines() if line.strip()}
    return found - {project}


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


def stop_container(container_id: str, timeout: int = 30, isolate: bool = False) -> bool:
    """Stop a container with given timeout. Returns True on success.

    ``isolate`` runs docker stop in its own session so a terminal signal
    can't cut it short (the daemon would carry on stopping regardless).
    """
    args = ["docker", "stop", "--time", str(timeout), container_id]
    result = process.run(args, new_session=True) if isolate else process.run(args)
    return result.returncode == 0


def remove_container(container_id: str) -> bool:
    """Remove a container. Returns True on success."""
    result = process.run(["docker", "rm", container_id])
    return result.returncode == 0


def image_tag(image: str) -> str | None:
    """The tag of ``registry/name:tag[@digest]``, or None when there's no tag.

    A bare image ID (``sha256:...``) has no tag.
    """
    if image.startswith("sha256:"):
        return None
    name = image.rsplit("/", 1)[-1].split("@", 1)[0]
    return name.rsplit(":", 1)[1] if ":" in name else None


def image_repo(image: str) -> str:
    """``registry/name`` of ``registry/name:tag[@digest]`` (the registry may have a port)."""
    image = image.split("@", 1)[0]
    head, _, last = image.rpartition("/")
    name = last.split(":", 1)[0]
    return f"{head}/{name}" if head else name


def image_ids(container_ids: list[str]) -> dict[str, str] | None:
    """Each container's image ID (``sha256:...``), keyed by the IDs given.

    Accepts docker ps's short IDs. None if docker inspect fails.
    """
    if not container_ids:
        return {}
    result = process.run(["docker", "inspect", "--format", "{{.Id}} {{.Image}}", *container_ids])
    if result.returncode != 0:
        return None
    full = dict(line.split() for line in result.stdout.splitlines() if len(line.split()) == 2)
    found = {}
    for short in container_ids:
        match = next((img for cid, img in full.items() if cid.startswith(short)), None)
        if match is None:
            return None
        found[short] = match
    return found


def tag_image(image_id: str, ref: str) -> bool:
    """Point the local tag ``ref`` at ``image_id``. Returns True on success."""
    return process.run(["docker", "tag", image_id, ref]).returncode == 0
