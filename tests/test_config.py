"""Tests for config.py — service parsing, label extraction, validation."""

from flow_deploy.config import parse_services, validate_healthchecks


def _compose_dict(*services):
    """Helper to build a compose dict from (name, svc_dict) tuples."""
    return {"services": dict(services)}


def test_parse_ignores_unlabeled():
    d = _compose_dict(
        (
            "web",
            {
                "image": "app:latest",
                "labels": {"deploy.role": "app"},
                "healthcheck": {"test": ["CMD", "curl", "localhost"]},
            },
        ),
        ("redis", {"image": "redis:7"}),
    )
    result = parse_services(d)
    assert len(result) == 1
    assert result[0].name == "web"


def test_parse_app_and_accessory():
    d = _compose_dict(
        (
            "web",
            {
                "image": "app:latest",
                "labels": {"deploy.role": "app"},
                "healthcheck": {"test": ["CMD", "true"]},
            },
        ),
        ("db", {"image": "postgres:16", "labels": {"deploy.role": "accessory"}}),
    )
    result = parse_services(d)
    assert len(result) == 2
    assert result[0].name == "web"
    assert result[0].is_app
    assert result[1].name == "db"
    assert not result[1].is_app


def test_parse_label_defaults():
    d = _compose_dict(
        (
            "web",
            {
                "image": "app:latest",
                "labels": {"deploy.role": "app"},
                "healthcheck": {"test": ["CMD", "true"]},
            },
        ),
    )
    svc = parse_services(d)[0]
    assert svc.order == 100
    assert svc.drain == 30
    assert svc.healthcheck_timeout == 120
    assert svc.healthcheck_poll == 2


def test_parse_custom_labels():
    d = _compose_dict(
        (
            "web",
            {
                "image": "app:latest",
                "labels": {
                    "deploy.role": "app",
                    "deploy.order": "10",
                    "deploy.drain": "60",
                    "deploy.healthcheck.timeout": "30",
                    "deploy.healthcheck.poll": "5",
                },
                "healthcheck": {"test": ["CMD", "true"]},
            },
        ),
    )
    svc = parse_services(d)[0]
    assert svc.order == 10
    assert svc.drain == 60
    assert svc.healthcheck_timeout == 30
    assert svc.healthcheck_poll == 5


def test_parse_sorts_by_order_then_file_order():
    d = _compose_dict(
        (
            "worker",
            {
                "image": "app:latest",
                "labels": {"deploy.role": "app", "deploy.order": "200"},
                "healthcheck": {"test": ["CMD", "true"]},
            },
        ),
        (
            "web",
            {
                "image": "app:latest",
                "labels": {"deploy.role": "app", "deploy.order": "10"},
                "healthcheck": {"test": ["CMD", "true"]},
            },
        ),
        (
            "api",
            {
                "image": "app:latest",
                "labels": {"deploy.role": "app", "deploy.order": "10"},
                "healthcheck": {"test": ["CMD", "true"]},
            },
        ),
    )
    result = parse_services(d)
    names = [s.name for s in result]
    assert names == ["web", "api", "worker"]


def test_parse_list_labels():
    d = _compose_dict(
        (
            "web",
            {
                "image": "app:latest",
                "labels": ["deploy.role=app", "deploy.order=50"],
                "healthcheck": {"test": ["CMD", "true"]},
            },
        ),
    )
    svc = parse_services(d)[0]
    assert svc.role == "app"
    assert svc.order == 50


def test_has_healthcheck_true():
    d = _compose_dict(
        (
            "web",
            {
                "image": "app:latest",
                "labels": {"deploy.role": "app"},
                "healthcheck": {"test": ["CMD", "curl", "localhost"]},
            },
        ),
    )
    assert parse_services(d)[0].has_healthcheck


def test_has_healthcheck_false():
    d = _compose_dict(
        ("web", {"image": "app:latest", "labels": {"deploy.role": "app"}}),
    )
    assert not parse_services(d)[0].has_healthcheck


def test_has_healthcheck_false_null_test():
    d = _compose_dict(
        (
            "web",
            {
                "image": "app:latest",
                "labels": {"deploy.role": "app"},
                "healthcheck": {"test": None},
            },
        ),
    )
    assert not parse_services(d)[0].has_healthcheck


def test_validate_healthchecks_returns_missing():
    d = _compose_dict(
        (
            "web",
            {
                "image": "app:latest",
                "labels": {"deploy.role": "app"},
                "healthcheck": {"test": ["CMD", "true"]},
            },
        ),
        ("worker", {"image": "app:latest", "labels": {"deploy.role": "app"}}),
        ("db", {"image": "postgres:16", "labels": {"deploy.role": "accessory"}}),
    )
    services = parse_services(d)
    missing = validate_healthchecks(services)
    assert missing == ["worker"]


def test_validate_healthchecks_all_good():
    d = _compose_dict(
        (
            "web",
            {
                "image": "app:latest",
                "labels": {"deploy.role": "app"},
                "healthcheck": {"test": ["CMD", "true"]},
            },
        ),
    )
    services = parse_services(d)
    assert validate_healthchecks(services) == []


def test_empty_services():
    assert parse_services({"services": {}}) == []
    assert parse_services({}) == []


# --- x-deploy discovery ---


def test_x_deploy_defaults():
    d = {
        "x-deploy": {"host": "app-1.example.com", "user": "deploy", "dir": "/srv/myapp"},
        "services": {
            "web": {
                "image": "app:latest",
                "labels": {"deploy.role": "app"},
                "healthcheck": {"test": ["CMD", "true"]},
            },
        },
    }
    svc = parse_services(d)[0]
    assert svc.host == "app-1.example.com"
    assert svc.user == "deploy"
    assert svc.dir == "/srv/myapp"


def test_per_service_labels_override_x_deploy():
    d = {
        "x-deploy": {"host": "app-1.example.com", "user": "deploy", "dir": "/srv/myapp"},
        "services": {
            "web": {
                "image": "app:latest",
                "labels": {"deploy.role": "app"},
                "healthcheck": {"test": ["CMD", "true"]},
            },
            "worker": {
                "image": "app:latest",
                "labels": {
                    "deploy.role": "app",
                    "deploy.host": "worker-1.example.com",
                    "deploy.dir": "/srv/worker",
                },
                "healthcheck": {"test": ["CMD", "true"]},
            },
        },
    }
    svcs = parse_services(d)
    web = next(s for s in svcs if s.name == "web")
    worker = next(s for s in svcs if s.name == "worker")

    assert web.host == "app-1.example.com"
    assert web.user == "deploy"
    assert web.dir == "/srv/myapp"

    assert worker.host == "worker-1.example.com"
    assert worker.user == "deploy"
    assert worker.dir == "/srv/worker"


def test_no_x_deploy_no_labels():
    d = _compose_dict(
        (
            "web",
            {
                "image": "app:latest",
                "labels": {"deploy.role": "app"},
                "healthcheck": {"test": ["CMD", "true"]},
            },
        ),
    )
    svc = parse_services(d)[0]
    assert svc.host is None
    assert svc.user is None
    assert svc.dir is None


def test_x_deploy_with_list_labels():
    d = {
        "x-deploy": {"host": "default.example.com", "user": "deploy", "dir": "/srv/app"},
        "services": {
            "web": {
                "image": "app:latest",
                "labels": ["deploy.role=app", "deploy.host=override.example.com"],
                "healthcheck": {"test": ["CMD", "true"]},
            },
        },
    }
    svc = parse_services(d)[0]
    assert svc.host == "override.example.com"
    assert svc.user == "deploy"
    assert svc.dir == "/srv/app"
