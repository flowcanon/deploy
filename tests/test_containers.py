"""Tests for containers.py — Docker inspect, identify, stop, remove."""

import json

from flow_deploy import process
from flow_deploy.containers import (
    get_container_health,
    get_containers_for_service,
    identify_old_new,
    remove_container,
    stop_container,
)


def test_get_containers_for_service(mock_process):
    container = {"ID": "abc123", "Image": "app:v1", "CreatedAt": "2024-01-01", "State": "running"}
    mock_process.responses.append(process.Result(0, json.dumps(container) + "\n", ""))
    result = get_containers_for_service("web")
    assert len(result) == 1
    assert result[0]["ID"] == "abc123"
    # Verify correct docker command
    _, args, _, _ = mock_process.calls[0]
    assert "docker" in args
    assert "ps" in args
    assert "com.docker.compose.service=web" in " ".join(args)


def test_get_containers_empty(mock_process):
    mock_process.responses.append(process.Result(0, "", ""))
    result = get_containers_for_service("web")
    assert result == []


def test_get_containers_error(mock_process):
    mock_process.responses.append(process.Result(1, "", "error"))
    result = get_containers_for_service("web")
    assert result == []


def test_identify_old_new_by_tag():
    old = {"ID": "old1", "Image": "app:v1", "CreatedAt": "2024-01-01 00:00:00"}
    new = {"ID": "new1", "Image": "app:v2", "CreatedAt": "2024-01-02 00:00:00"}
    o, n = identify_old_new([old, new], "v2")
    assert o["ID"] == "old1"
    assert n["ID"] == "new1"


def test_identify_old_new_by_tag_reversed():
    new = {"ID": "new1", "Image": "app:v2", "CreatedAt": "2024-01-01 00:00:00"}
    old = {"ID": "old1", "Image": "app:v1", "CreatedAt": "2024-01-02 00:00:00"}
    o, n = identify_old_new([new, old], "v2")
    assert o["ID"] == "old1"
    assert n["ID"] == "new1"


def test_identify_old_new_same_tag():
    old = {"ID": "old1", "Image": "app:latest", "CreatedAt": "2024-01-01 00:00:00"}
    new = {"ID": "new1", "Image": "app:latest", "CreatedAt": "2024-01-02 00:00:00"}
    o, n = identify_old_new([old, new], "latest")
    assert o["ID"] == "old1"
    assert n["ID"] == "new1"


def test_identify_wrong_count():
    assert identify_old_new([], "v1") == (None, None)
    c = {"ID": "a", "Image": "app:v1", "CreatedAt": "2024-01-01"}
    assert identify_old_new([c], "v1") == (None, None)
    assert identify_old_new([c, c, c], "v1") == (None, None)


def test_get_container_health(mock_process):
    mock_process.responses.append(process.Result(0, "healthy\n", ""))
    assert get_container_health("abc123") == "healthy"


def test_get_container_health_starting(mock_process):
    mock_process.responses.append(process.Result(0, "starting\n", ""))
    assert get_container_health("abc123") == "starting"


def test_get_container_health_error(mock_process):
    mock_process.responses.append(process.Result(1, "", "error"))
    assert get_container_health("abc123") is None


def test_stop_container(mock_process):
    mock_process.responses.append(process.Result(0, "", ""))
    assert stop_container("abc123", timeout=60) is True
    _, args, _, _ = mock_process.calls[0]
    assert args == ["docker", "stop", "--time", "60", "abc123"]


def test_stop_container_failure(mock_process):
    mock_process.responses.append(process.Result(1, "", ""))
    assert stop_container("abc123") is False


def test_remove_container(mock_process):
    mock_process.responses.append(process.Result(0, "", ""))
    assert remove_container("abc123") is True
    _, args, _, _ = mock_process.calls[0]
    assert args == ["docker", "rm", "abc123"]
