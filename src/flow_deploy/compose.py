"""Compose command resolution + execution."""

import os

import yaml

from flow_deploy import process


def resolve_command() -> list[str]:
    """Resolve the compose command to use.

    Order: COMPOSE_COMMAND env → script/prod (if executable) → docker compose.
    """
    env_cmd = os.environ.get("COMPOSE_COMMAND")
    if env_cmd:
        return env_cmd.split()

    if os.path.isfile("script/prod") and os.access("script/prod", os.X_OK):
        return ["script/prod"]

    return ["docker", "compose"]


def compose_run(
    args: list[str],
    env: dict[str, str] | None = None,
    cmd: list[str] | None = None,
) -> process.Result:
    """Run a compose command with the resolved command prefix."""
    command = cmd or resolve_command()
    return process.run(command + args, env=env)


def compose_config(cmd: list[str] | None = None) -> dict:
    """Run <compose-cmd> config and parse YAML output."""
    command = cmd or resolve_command()
    result = process.run(command + ["config"])
    if result.returncode != 0:
        raise RuntimeError(f"compose config failed: {result.stderr}")
    return yaml.safe_load(result.stdout)
