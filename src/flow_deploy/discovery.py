"""Host discovery — group app services by deploy target."""

import os

from flow_deploy.config import parse_services


def env_overrides() -> dict[str, str]:
    """Read optional HOST_NAME / HOST_USER env-var overrides."""
    overrides: dict[str, str] = {}
    if os.environ.get("HOST_NAME"):
        overrides["host"] = os.environ["HOST_NAME"]
    if os.environ.get("HOST_USER"):
        overrides["user"] = os.environ["HOST_USER"]
    return overrides


def discover_hosts(compose_dict: dict, overrides: dict[str, str] | None = None) -> list[dict]:
    """Group app services by (host, user, dir).

    Optional *overrides* dict replaces host/user on every group
    (typically sourced from GitHub Actions variables).

    Raises ValueError if any app service is missing a deploy host.
    """
    overrides = overrides or {}
    services = parse_services(compose_dict)
    app_services = [s for s in services if s.is_app]

    # Apply overrides before validation so env vars can supply missing hosts
    for svc in app_services:
        if "host" in overrides:
            svc.host = overrides["host"]
        if "user" in overrides:
            svc.user = overrides["user"]

    missing = [s.name for s in app_services if s.host is None]
    if missing:
        raise ValueError(f"services missing deploy host: {', '.join(missing)}")

    groups: dict[tuple, dict] = {}
    for svc in app_services:
        key = (svc.host, svc.user, svc.dir)
        if key not in groups:
            groups[key] = {
                "host": svc.host,
                "user": svc.user,
                "dir": svc.dir,
                "services": [],
            }
        groups[key]["services"].append(svc.name)

    return list(groups.values())
