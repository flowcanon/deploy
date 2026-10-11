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


def compose_config(
    cmd: list[str] | None = None,
    env: dict[str, str] | None = None,
    no_interpolate: bool = False,
) -> dict:
    """Run <compose-cmd> config and parse YAML output.

    ``env`` (e.g. DEPLOY_TAG) applies while compose interpolates, so the
    rendered images show what a deploy with it would run. ``no_interpolate``
    leaves ${VARS} as written, so a project whose env isn't present (CI
    discovery) still parses.
    """
    command = (cmd or resolve_command()) + ["config"]
    if no_interpolate:
        command.append("--no-interpolate")
    if env:
        result = process.run(command, env=env)
    else:
        result = process.run(command)
    if result.returncode != 0:
        raise RuntimeError(f"compose config failed: {result.stderr}")
    return yaml.safe_load(result.stdout)


def config_hashes(
    services: list[str], cmd: list[str] | None = None, env: dict[str, str] | None = None
) -> dict[str, str] | None:
    """The config hash compose would label each service's containers with.

    What `up` compares to decide whether to recreate. None if compose can't
    say (an older compose without --hash, or an error).
    """
    if not services:
        return {}
    command = (cmd or resolve_command()) + ["config", f"--hash={','.join(services)}"]
    result = process.run(command, env=env) if env else process.run(command)
    if result.returncode != 0:
        return None
    pairs = (line.split() for line in result.stdout.splitlines())
    return {p[0]: p[1] for p in pairs if len(p) == 2}
