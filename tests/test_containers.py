"""Tests for containers.py — Docker inspect, identify, stop, remove."""

import json

from flow_deploy import process
from flow_deploy.containers import (
    exit_code,
    get_container_health,
    get_containers_for_service,
    identify_old_new,
    image_ids,
    image_repo,
    image_tag,
    local_image_id,
    other_projects_here,
    remove_container,
    running_config_hashes,
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


def test_get_containers_filters_by_project(mock_process):
    mock_process.responses.append(process.Result(0, "", ""))
    get_containers_for_service("web", project="myapp")
    _, args, _, _ = mock_process.calls[0]
    assert "com.docker.compose.project=myapp" in " ".join(args)


def test_identify_old_new_same_tag_newer_listed_first():
    new = {"ID": "new1", "Image": "app:latest", "CreatedAt": "2024-01-02 00:00:00"}
    old = {"ID": "old1", "Image": "app:latest", "CreatedAt": "2024-01-01 00:00:00"}
    o, n = identify_old_new([new, old], "latest")
    assert o["ID"] == "old1"
    assert n["ID"] == "new1"


def test_identify_old_new_indistinguishable():
    a = {"ID": "a", "Image": "app:latest", "CreatedAt": "2024-01-01 00:00:00"}
    b = {"ID": "b", "Image": "app:latest", "CreatedAt": "2024-01-01 00:00:00"}
    assert identify_old_new([a, b], "latest") == (None, None)


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


def test_stop_container_isolated_runs_in_its_own_session(mock_process):
    assert stop_container("abc123", timeout=5, isolate=True) is True
    assert stop_container("def456") is True
    assert mock_process.new_sessions == [["docker", "stop", "--time", "5", "abc123"]]


def test_stop_container_failure(mock_process):
    mock_process.responses.append(process.Result(1, "", ""))
    assert stop_container("abc123") is False


def test_remove_container(mock_process):
    mock_process.responses.append(process.Result(0, "", ""))
    assert remove_container("abc123") is True
    _, args, _, _ = mock_process.calls[0]
    assert args == ["docker", "rm", "abc123"]


def test_image_tag():
    assert image_tag("ghcr.io/org/app:v1.2.0") == "v1.2.0"
    assert image_tag("localhost:5000/app:abc123@sha256:ff") == "abc123"
    assert image_tag("localhost:5000/app") is None
    assert image_tag("ghcr.io/org/app@sha256:ff") is None
    assert image_tag("sha256:" + "a" * 64) is None


def test_image_repo():
    assert image_repo("ghcr.io/org/app:v1") == "ghcr.io/org/app"
    assert image_repo("localhost:5000/app:v1@sha256:ff") == "localhost:5000/app"
    assert image_repo("app:v1") == "app"
    assert image_repo("app") == "app"


def test_image_ids_maps_short_ids(mock_process):
    mock_process.responses.append(
        process.Result(0, "aaa111ffff sha256:one\nbbb222ffff sha256:two\n", "")
    )
    assert image_ids(["aaa111", "bbb222"]) == {"aaa111": "sha256:one", "bbb222": "sha256:two"}
    assert mock_process.calls[0][1] == [
        "docker",
        "inspect",
        "--format",
        "{{.Id}} {{.Image}}",
        "aaa111",
        "bbb222",
    ]


def test_image_ids_empty_makes_no_call(mock_process):
    assert image_ids([]) == {}
    assert mock_process.calls == []


def test_image_ids_inspect_failure(mock_process):
    mock_process.responses.append(process.Result(1, "", "No such container"))
    assert image_ids(["aaa111"]) is None


def test_image_ids_missing_container(mock_process):
    mock_process.responses.append(process.Result(0, "aaa111ffff sha256:one\n", ""))
    assert image_ids(["aaa111", "ccc333"]) is None


def test_local_image_id(mock_process):
    mock_process.responses.append(process.Result(0, "sha256:one\n", ""))
    mock_process.responses.append(process.Result(1, "", "No such image"))
    assert local_image_id("app:v1") == "sha256:one"
    assert mock_process.calls[0][1] == [
        "docker",
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
        "app:v1",
    ]
    assert local_image_id("app:v9") is None


def test_other_projects_here(mock_process, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PWD", str(tmp_path))
    out = "proj web\nproj worker\nold web\nobs grafana\n\n"
    mock_process.responses.append(process.Result(0, out, ""))
    # obs shares the directory but runs none of these services: not a rename.
    assert other_projects_here("proj", ["web", "worker"]) == {"old"}
    args = mock_process.calls[0][1]
    assert f"label=com.docker.compose.project.working_dir={tmp_path}" in args


def test_other_projects_here_checks_the_shell_path_too(mock_process, monkeypatch, tmp_path):
    """compose records the directory as given, which may be a symlinked path."""
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    monkeypatch.chdir(real)
    monkeypatch.setenv("PWD", str(tmp_path / "link"))
    mock_process.responses.append(process.Result(0, "", ""))
    mock_process.responses.append(process.Result(0, "other web\n", ""))
    assert other_projects_here("proj", ["web"]) == {"other"}
    assert len(mock_process.calls) == 2


def test_other_projects_here_ignores_an_inherited_pwd(mock_process, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PWD", "/somewhere/else")
    mock_process.responses.append(process.Result(0, "", ""))
    assert other_projects_here("proj", ["web"]) == set()
    assert len(mock_process.calls) == 1


def test_other_projects_here_failure(mock_process, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PWD", raising=False)
    mock_process.responses.append(process.Result(1, "", "daemon down"))
    assert other_projects_here("proj", ["web"]) is None


def test_exit_code(mock_process):
    mock_process.responses += [
        process.Result(0, "137\n", ""),
        process.Result(1, "", "No such container"),
        process.Result(0, "<no value>\n", ""),
    ]
    assert exit_code("abc") == 137
    assert mock_process.calls[0][1] == [
        "docker",
        "inspect",
        "--format",
        "{{.State.ExitCode}}",
        "abc",
    ]
    assert exit_code("gone") is None
    assert exit_code("odd") is None


def test_running_config_hashes(mock_process):
    mock_process.responses += [process.Result(0, "h1\nh1\n\n", ""), process.Result(1, "", "")]
    assert running_config_hashes("db", project="proj") == {"h1"}
    assert "label=com.docker.compose.project=proj" in mock_process.calls[0][1]
    assert "label=com.docker.compose.oneoff=False" in mock_process.calls[0][1]
    assert running_config_hashes("db") is None
