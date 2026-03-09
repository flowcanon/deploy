"""Tests for flow_deploy.discovery — host grouping and env-var overrides."""

import pytest

from flow_deploy.discovery import discover_hosts, env_overrides


def _compose(x_deploy=None, **services):
    d = {"services": {}}
    if x_deploy:
        d["x-deploy"] = x_deploy
    for name, svc in services.items():
        d["services"][name] = svc
    return d


def _app_svc(host=None, user=None, dir_=None):
    svc = {
        "image": "app:latest",
        "labels": {"deploy.role": "app"},
        "healthcheck": {"test": ["CMD", "true"]},
    }
    if host:
        svc["labels"]["deploy.host"] = host
    if user:
        svc["labels"]["deploy.user"] = user
    if dir_:
        svc["labels"]["deploy.dir"] = dir_
    return svc


def test_basic_grouping():
    d = _compose(
        x_deploy={"host": "h1", "user": "deploy", "dir": "/srv/app"},
        web=_app_svc(),
    )
    groups = discover_hosts(d)
    assert len(groups) == 1
    assert groups[0]["host"] == "h1"
    assert groups[0]["user"] == "deploy"
    assert groups[0]["services"] == ["web"]


def test_override_host():
    d = _compose(
        x_deploy={"host": "h1", "user": "deploy", "dir": "/srv/app"},
        web=_app_svc(),
    )
    groups = discover_hosts(d, overrides={"host": "secret-host"})
    assert groups[0]["host"] == "secret-host"
    assert groups[0]["user"] == "deploy"


def test_override_user():
    d = _compose(
        x_deploy={"host": "h1", "user": "deploy", "dir": "/srv/app"},
        web=_app_svc(),
    )
    groups = discover_hosts(d, overrides={"user": "secret-user"})
    assert groups[0]["host"] == "h1"
    assert groups[0]["user"] == "secret-user"


def test_override_both():
    d = _compose(
        x_deploy={"host": "h1", "user": "deploy", "dir": "/srv/app"},
        web=_app_svc(),
    )
    groups = discover_hosts(d, overrides={"host": "secret-host", "user": "secret-user"})
    assert groups[0]["host"] == "secret-host"
    assert groups[0]["user"] == "secret-user"


def test_override_collapses_groups():
    """Two services on different hosts collapse into one group when host is overridden."""
    d = _compose(
        x_deploy={"user": "deploy", "dir": "/srv/app"},
        web=_app_svc(host="h1"),
        worker=_app_svc(host="h2"),
    )
    groups = discover_hosts(d, overrides={"host": "single-host"})
    assert len(groups) == 1
    assert groups[0]["host"] == "single-host"
    assert sorted(groups[0]["services"]) == ["web", "worker"]


def test_override_supplies_missing_host():
    """Override can provide host when x-deploy and labels are missing."""
    d = _compose(web=_app_svc())
    groups = discover_hosts(d, overrides={"host": "supplied-host"})
    assert groups[0]["host"] == "supplied-host"


def test_no_overrides_passes_through():
    d = _compose(
        x_deploy={"host": "h1", "user": "deploy", "dir": "/srv/app"},
        web=_app_svc(),
    )
    groups = discover_hosts(d, overrides={})
    assert groups[0]["host"] == "h1"
    assert groups[0]["user"] == "deploy"


def test_missing_host_raises():
    d = _compose(web=_app_svc())
    with pytest.raises(ValueError, match="missing deploy host"):
        discover_hosts(d)


def test_port_from_x_deploy():
    d = _compose(
        x_deploy={"host": "h1", "user": "deploy", "port": 2222, "dir": "/srv/app"},
        web=_app_svc(),
    )
    groups = discover_hosts(d)
    assert groups[0]["port"] == 2222


def test_port_omitted_when_none():
    d = _compose(
        x_deploy={"host": "h1", "user": "deploy", "dir": "/srv/app"},
        web=_app_svc(),
    )
    groups = discover_hosts(d)
    assert "port" not in groups[0]


def test_override_port():
    d = _compose(
        x_deploy={"host": "h1", "user": "deploy", "dir": "/srv/app"},
        web=_app_svc(),
    )
    groups = discover_hosts(d, overrides={"port": "2222"})
    assert groups[0]["port"] == 2222


def test_override_port_replaces_x_deploy():
    d = _compose(
        x_deploy={"host": "h1", "user": "deploy", "port": 22, "dir": "/srv/app"},
        web=_app_svc(),
    )
    groups = discover_hosts(d, overrides={"port": "2222"})
    assert groups[0]["port"] == 2222


def test_env_overrides_reads_env(monkeypatch):
    monkeypatch.setenv("HOST_NAME", "env-host")
    monkeypatch.setenv("HOST_USER", "env-user")
    monkeypatch.delenv("SSH_PORT", raising=False)
    assert env_overrides() == {"host": "env-host", "user": "env-user"}


def test_env_overrides_empty(monkeypatch):
    monkeypatch.delenv("HOST_NAME", raising=False)
    monkeypatch.delenv("HOST_USER", raising=False)
    monkeypatch.delenv("SSH_PORT", raising=False)
    assert env_overrides() == {}


def test_env_overrides_partial(monkeypatch):
    monkeypatch.setenv("HOST_NAME", "env-host")
    monkeypatch.delenv("HOST_USER", raising=False)
    monkeypatch.delenv("SSH_PORT", raising=False)
    assert env_overrides() == {"host": "env-host"}


def test_env_overrides_with_port(monkeypatch):
    monkeypatch.setenv("HOST_NAME", "env-host")
    monkeypatch.setenv("HOST_USER", "env-user")
    monkeypatch.setenv("SSH_PORT", "2222")
    assert env_overrides() == {"host": "env-host", "user": "env-user", "port": "2222"}
