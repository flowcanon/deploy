"""Real-container integration harness for flow-deploy.

Each test gets a Stack: a throwaway git repo (with a bare origin, since
flow-deploy fetches) holding one tagged commit per compose variant, and a
compose project of stub services pulled from a local registry. flow-deploy
runs as a real subprocess, so tests can signal it, against real Docker.

Run with:  poetry run pytest -m integration --no-cov
Skipped automatically when Docker isn't available.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
import yaml

IMAGES = Path(__file__).parent / "images"
SRC = Path(__file__).resolve().parents[2] / "src"
STUB_TAGS = {
    "v1": {"STUB_VERSION": "v1"},
    "v2": {"STUB_VERSION": "v2"},
    "v3": {"STUB_VERSION": "v3"},
    "unhealthy": {"STUB_VERSION": "unhealthy", "STUB_HEALTH": "fail"},
}


def pytest_collection_modifyitems(config, items):
    for item in items:
        if Path(str(item.fspath)).is_relative_to(Path(__file__).parent):
            item.add_marker(pytest.mark.integration)


def run(args, **kwargs):
    kwargs.setdefault("check", True)
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(args, **kwargs)


@pytest.fixture(scope="session")
def docker():
    if shutil.which("docker") is None or run(["docker", "info"], check=False).returncode:
        pytest.skip("Docker is not available")
    up_help = run(["docker", "compose", "up", "--help"], check=False).stdout
    if "--wait" not in up_help:
        pytest.skip("Docker Compose is too old: `docker compose up --wait` is required")


@pytest.fixture(scope="session")
def registry(docker):
    """A local registry:2 with the stub image pushed under each STUB_TAGS tag."""
    name = f"flowdeploy-it-registry-{uuid.uuid4().hex[:8]}"
    run(["docker", "run", "-d", "--name", name, "-p", "127.0.0.1::5000", "registry:2"])
    try:
        port = run(["docker", "port", name, "5000"]).stdout.strip().split(":")[-1]
        host = f"localhost:{port}"
        for _ in range(50):
            if run(["curl", "-sf", f"http://{host}/v2/"], check=False).returncode == 0:
                break
            time.sleep(0.2)
        for tag, args in STUB_TAGS.items():
            image = f"{host}/stub:{tag}"
            build_args = [a for k, v in args.items() for a in ("--build-arg", f"{k}={v}")]
            run(["docker", "build", "-q", "-t", image, *build_args, str(IMAGES / "stub")])
            run(["docker", "push", "-q", image])
        yield host
    finally:
        run(["docker", "rm", "-f", name], check=False)


def service(
    order, strategy="rolling", drain=10, env=None, health_timeout=30, image=None, start_period=None
):
    """One compose service of the stub image, labelled for flow-deploy."""
    labels = {
        "deploy.role": "app",
        "deploy.order": str(order),
        "deploy.drain": str(drain),
        "deploy.healthcheck.timeout": str(health_timeout),
        "deploy.healthcheck.poll": "1",
    }
    if strategy != "rolling":
        labels["deploy.strategy"] = strategy
    healthcheck = {
        "test": ["CMD", "test", "-f", "/tmp/healthy"],
        "interval": "1s",
        "timeout": "2s",
        "retries": 2,
    }
    if start_period:
        healthcheck["start_period"] = start_period
    return {
        "image": image or "${REGISTRY}/stub:${DEPLOY_TAG}",
        "environment": dict(env or {}),
        "labels": labels,
        "healthcheck": healthcheck,
    }


def standard(**overrides):
    """web (rolling), scheduler and worker (drain-first), with per-service overrides.

    Pass ``scheduler=None`` to leave a service out.
    """
    services = {
        "web": service(100),
        "scheduler": service(200, "drain-first"),
        "worker": service(300, "drain-first"),
    }
    for name, value in overrides.items():
        if value is None:
            services.pop(name, None)
        else:
            services[name] = value
    return {"services": services}


class Stack:
    """A git repo + compose project that flow-deploy deploys."""

    def __init__(self, root: Path, registry: str):
        self.registry = registry
        self.project = f"fdit{uuid.uuid4().hex[:10]}"
        self.origin = root / "origin.git"
        self.repo = root / "repo"
        run(["git", "init", "-q", "--bare", str(self.origin)])
        run(["git", "init", "-q", "-b", "main", str(self.repo)])
        self.git("config", "user.email", "it@example.com")
        self.git("config", "user.name", "it")
        self.git("config", "commit.gpgsign", "false")
        self.git("remote", "add", "origin", str(self.origin))
        # Never lose a scaled-up compose project if a test fails.
        self._up = False

    def env(self, tag=None, extra=None):
        env = {
            **(extra or {}),
            **os.environ,
            "COMPOSE_PROJECT_NAME": self.project,
            "REGISTRY": self.registry,
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": str(SRC),
        }
        env.pop("GITHUB_ACTIONS", None)  # plain log output for assertions
        if tag:
            env["DEPLOY_TAG"] = tag
        return env

    def git(self, *args):
        return run(["git", "-C", str(self.repo), *args]).stdout.strip()

    def commit(self, tag, compose):
        """Commit ``compose`` as docker-compose.yml and tag it ``tag``."""
        (self.repo / "docker-compose.yml").write_text(yaml.safe_dump(compose, sort_keys=False))
        self.git("add", "docker-compose.yml")
        self.git("commit", "-q", "--allow-empty", "-m", tag)
        self.git("tag", tag)
        self.git("push", "-q", "origin", "HEAD:refs/heads/main", "--tags")
        return self.git("rev-parse", "HEAD")

    def compose(self, *args, tag=None, check=True, extra=None):
        return run(
            ["docker", "compose", *args], cwd=self.repo, env=self.env(tag, extra), check=check
        )

    def up(self, tag, scale=None, wait=True, extra=None):
        """Bring the stack up at ``tag``, as a running production host would be."""
        self.git("checkout", "-q", "--detach", tag)
        scale_args = [a for name, n in (scale or {}).items() for a in ("--scale", f"{name}={n}")]
        wait_args = ["--wait"] if wait else []
        self._up = True
        self.compose("up", "-d", *wait_args, *scale_args, tag=tag, extra=extra)

    def digest(self, tag):
        """The registry digest reference (repo@sha256:...) of the stub at ``tag``."""
        return run(
            [
                "docker",
                "inspect",
                "--format",
                "{{index .RepoDigests 0}}",
                f"{self.registry}/stub:{tag}",
            ]
        ).stdout.strip()

    def deploy_cmd(self, tag):
        return [sys.executable, "-m", "flow_deploy.cli", "deploy", "--tag", tag]

    def deploy(self, tag, timeout=240):
        """Run a deploy to completion; returns the CompletedProcess."""
        self._up = True
        return run(
            self.deploy_cmd(tag), cwd=self.repo, env=self.env(), check=False, timeout=timeout
        )

    def start_deploy(self, tag):
        """Start a deploy in the background; returns a Running.

        It leads its own process group, so a test can signal the whole group
        the way a terminal's Ctrl-C does (os.killpg), not just flow-deploy.
        """
        self._up = True
        return Running(
            subprocess.Popen(
                self.deploy_cmd(tag),
                cwd=self.repo,
                env=self.env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        )

    def state(self):
        """service -> sorted list of (version tag, state) for its containers."""
        out = run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
                "--format",
                "{{json .}}",
            ]
        ).stdout
        result = {}
        for line in out.splitlines():
            ctr = json.loads(line)
            labels = dict(kv.split("=", 1) for kv in ctr["Labels"].split(",") if "=" in kv)
            svc = labels.get("com.docker.compose.service")
            tag = ctr["Image"].rsplit(":", 1)[-1] if "/" in ctr["Image"] else ctr["Image"]
            result.setdefault(svc, []).append((tag, ctr["State"]))
        return {svc: sorted(v) for svc, v in result.items()}

    def running(self):
        """service -> sorted list of the version tags of its running containers."""
        return {
            svc: sorted(tag for tag, state in ctrs if state == "running")
            for svc, ctrs in self.state().items()
            if any(state == "running" for _, state in ctrs)
        }

    def container_ids(self, service_name):
        return set(self.compose("ps", "-q", service_name).stdout.split())

    def head(self):
        return self.git("rev-parse", "HEAD")

    def sha(self, tag):
        return self.git("rev-parse", f"{tag}^{{commit}}")

    def lock_exists(self):
        return (self.repo / ".git" / "deploy-lock").exists()

    def down(self):
        if self._up:
            self.compose("down", "-v", "--remove-orphans", "--timeout", "1", check=False)


@pytest.fixture
def stack(registry, tmp_path):
    s = Stack(tmp_path, registry)
    try:
        yield s
    finally:
        s.down()


class Running:
    """A background deploy whose output a thread collects, so waits can time out."""

    def __init__(self, proc):
        self.proc = proc
        self.pid = proc.pid
        self.lines = []
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self):
        for line in self.proc.stdout:
            self.lines.append(line)

    @property
    def output(self):
        return "".join(self.lines)

    def wait_for(self, needle, timeout=120):
        """Block until a line contains ``needle``; fail after ``timeout``."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if needle in self.output:
                return
            if self.proc.poll() is not None and not self._reader.is_alive():
                break
            time.sleep(0.1)
        self.kill()
        raise AssertionError(f"never saw {needle!r} in output:\n{self.output}")

    def finish(self, timeout=180):
        """Wait for exit; returns (exit code, output). Kills it on timeout."""
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            raise
        self._reader.join(timeout=10)
        return self.proc.returncode, self.output

    def kill(self):
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


SIGNALS = {"SIGINT": signal.SIGINT, "SIGTERM": signal.SIGTERM}
