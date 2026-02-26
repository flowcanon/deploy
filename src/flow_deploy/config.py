"""Parse compose config into ServiceConfig objects."""

from dataclasses import dataclass


@dataclass
class ServiceConfig:
    name: str
    role: str
    image: str | None
    order: int
    drain: int
    healthcheck_timeout: int
    healthcheck_poll: int
    has_healthcheck: bool
    file_order: int
    host: str | None = None
    user: str | None = None
    dir: str | None = None

    @property
    def is_app(self) -> bool:
        return self.role == "app"


def _get_label(labels: dict, key: str, default=None):
    """Get a label value, with optional default."""
    return labels.get(key, default)


def _parse_x_deploy(compose_dict: dict) -> dict:
    """Extract x-deploy top-level defaults."""
    return compose_dict.get("x-deploy", {})


def parse_services(compose_dict: dict) -> list[ServiceConfig]:
    """Parse compose config dict into sorted ServiceConfig list.

    Only returns services with deploy.role label.
    Sorted by deploy.order (ascending), then file order.

    Host discovery: per-service deploy.host/user/dir labels override
    x-deploy top-level defaults.
    """
    x_deploy = _parse_x_deploy(compose_dict)
    services_dict = compose_dict.get("services", {})
    configs = []

    for idx, (name, svc) in enumerate(services_dict.items()):
        labels = svc.get("labels", {})
        if isinstance(labels, list):
            # Convert list format ["key=value", ...] to dict
            parsed = {}
            for item in labels:
                k, _, v = item.partition("=")
                parsed[k] = v
            labels = parsed

        role = _get_label(labels, "deploy.role")
        if role is None:
            continue

        has_healthcheck = "healthcheck" in svc and svc["healthcheck"].get("test") is not None

        # Host discovery: per-service label → x-deploy default → None
        host = _get_label(labels, "deploy.host") or x_deploy.get("host")
        user = _get_label(labels, "deploy.user") or x_deploy.get("user")
        svc_dir = _get_label(labels, "deploy.dir") or x_deploy.get("dir")

        configs.append(
            ServiceConfig(
                name=name,
                role=role,
                image=svc.get("image"),
                order=int(_get_label(labels, "deploy.order", "100")),
                drain=int(_get_label(labels, "deploy.drain", "30")),
                healthcheck_timeout=int(
                    _get_label(labels, "deploy.healthcheck.timeout", "120")
                ),
                healthcheck_poll=int(_get_label(labels, "deploy.healthcheck.poll", "2")),
                has_healthcheck=has_healthcheck,
                file_order=idx,
                host=host,
                user=user,
                dir=svc_dir,
            )
        )

    configs.sort(key=lambda s: (s.order, s.file_order))
    return configs


def validate_healthchecks(services: list[ServiceConfig]) -> list[str]:
    """Return list of app services missing healthchecks."""
    return [s.name for s in services if s.is_app and not s.has_healthcheck]
