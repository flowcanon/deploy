"""Click entry point — all commands."""

import json
import re
import sys

import click

from flow_deploy import __version__, compose
from flow_deploy import deploy as deploy_mod
from flow_deploy import deployed, discovery, log, process


@click.group()
@click.version_option(version=__version__, prog_name="flow-deploy")
def main():
    """Rolling deploys for Docker Compose + Traefik stacks."""


@main.command()
@click.option("--tag", default=None, help="Override image tag for all app services")
@click.option("--service", multiple=True, help="Deploy only specific service(s)")
@click.option("--dry-run", is_flag=True, help="Show what would happen without executing")
def deploy(tag, service, dry_run):
    """Perform a rolling deploy of all app services."""
    services_filter = list(service) if service else None
    code = deploy_mod.deploy(tag=tag, services_filter=services_filter, dry_run=dry_run)
    sys.exit(code)


@main.command()
def status():
    """Show current state of all managed services."""
    try:
        compose_dict = compose.compose_config(env={"DEPLOY_TAG": deployed.LOOKUP_PLACEHOLDER})
        tag, how = deployed.current()
    except RuntimeError as e:
        log.error(str(e))
        sys.exit(1)

    from flow_deploy import config, containers, git

    all_services = config.parse_services(compose_dict)
    current_sha = git.current_sha()

    log.info(f"Current SHA: {current_sha[:7] if current_sha else '(none)'}")
    log.info(f"Deployed tag: {tag} (from {how})" if tag else f"Deployed tag: unknown ({how})")
    last = log.last_run()
    if last:
        header, code = last
        ended = f"exit {code}" if code is not None else "no exit recorded (running, or killed)"
        log.info(f"Last deploy: {header}: {ended} (log: {log.RUN_LOG})")
    project = compose_dict.get("name", "")
    # The deployed tag, or the placeholder when it's unknown: never empty, or a
    # fail-closed wrapper would exit and the check would quietly say nothing.
    drift_env = {"DEPLOY_TAG": tag or deployed.LOOKUP_PLACEHOLDER}
    for name in deploy_mod.accessory_drift(compose_dict, None, project, drift_env):
        log.info(f"Accessory {name}: config changed since it started; next `up -d` recreates it")
    log.info("")

    for svc in all_services:
        ctrs = containers.get_containers_for_service(svc.name)
        if ctrs:
            for c in ctrs:
                cid = c.get("ID", "?")[:12]
                image = c.get("Image", "?")
                state = c.get("State", "?")
                health = containers.get_container_health(c.get("ID", ""))
                log.info(f"  {svc.name} ({svc.role})  {cid}  {image}  {state}/{health or 'none'}")
        else:
            log.info(f"  {svc.name} ({svc.role})  no containers")


@main.command(name="tag")
def tag_cmd():
    """Print the image tag deployed on this host.

    From the running app containers when they agree, else the last deploy's
    record. For scripts: DEPLOY_TAG=${DEPLOY_TAG:-$(flow-deploy tag)}.
    """
    tag, how = _deployed_tag()
    click.echo(tag)


def _deployed_tag(compose_cmd: list[str] | None = None) -> tuple[str, str]:
    """deployed.current(), or exit 1 with the reason.

    The reason goes to stderr only, never as a GitHub Actions annotation on
    stdout: scripts capture `flow-deploy tag`'s stdout as the tag.
    """
    try:
        tag, how = deployed.current(compose_cmd)
    except RuntimeError as e:
        tag, how = None, str(e)
    if tag is None:
        click.echo(f"Error: can't tell which tag is deployed: {how}", err=True)
        sys.exit(1)
    return tag, how


@main.command(
    name="compose",
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def compose_cmd(args):
    """Run compose with DEPLOY_TAG set to the deployed tag.

    For manual commands on the host (up -d, run --rm, ...), so they keep the
    deployed version instead of moving services to :latest. An explicit
    DEPLOY_TAG in the environment wins.
    """
    import os

    command = compose.resolve_command()
    tag = os.environ.get("DEPLOY_TAG")
    if not tag:
        tag, how = _deployed_tag(command)
        click.echo(f"DEPLOY_TAG={tag} (from {how})", err=True)
    sys.exit(process.run_streaming(command + list(args), env={"DEPLOY_TAG": tag}))


@main.command(name="exec", context_settings={"ignore_unknown_options": True})
@click.argument("service")
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
def exec_cmd(service, command):
    """Run a command inside a running service container."""
    if not command:
        click.echo("Error: No command specified", err=True)
        sys.exit(1)
    compose_cmd = compose.resolve_command()
    code = process.run_streaming(compose_cmd + ["exec", service] + list(command), env=_tag_env())
    sys.exit(code)


def _tag_env() -> dict[str, str]:
    """DEPLOY_TAG for compose calls whose image doesn't matter (exec, logs).

    An explicit DEPLOY_TAG wins; otherwise the lookup placeholder, so a
    wrapper that defaults DEPLOY_TAG from `flow-deploy tag` never has to ask.
    """
    import os

    return {"DEPLOY_TAG": os.environ.get("DEPLOY_TAG") or deployed.LOOKUP_PLACEHOLDER}


@main.command()
@click.argument("service")
@click.option("--follow", "-f", is_flag=True, help="Follow log output")
@click.option("--tail", "-n", default=None, type=int, help="Number of lines to show")
def logs(service, follow, tail):
    """Tail logs for a service."""
    compose_cmd = compose.resolve_command()
    args = ["logs"]
    if follow:
        args.append("--follow")
    if tail is not None:
        args.extend(["--tail", str(tail)])
    args.append(service)
    code = process.run_streaming(compose_cmd + args, env=_tag_env())
    sys.exit(code)


@main.command()
@click.option("--command", default=None, help="Compose command override (e.g. 'docker compose')")
def config(command):
    """Show resolved deploy configuration as JSON."""
    cmd = command.split() if command else None
    # DEPLOY_TAG is set so a wrapper that defaults it from `flow-deploy tag`
    # never has to ask (there's nothing deployed in CI to ask about).
    env = {"DEPLOY_TAG": deployed.LOOKUP_PLACEHOLDER}
    try:
        compose_dict = compose.compose_config(cmd=cmd, env=env)
    except RuntimeError as e:
        # Discovery only needs x-deploy and labels, not the app's env (CI
        # has no .env): retry without interpolation rather than fail.
        try:
            compose_dict = compose.compose_config(cmd=cmd, env=env, no_interpolate=True)
        except RuntimeError:
            log.error(str(e))
            sys.exit(1)
        # compose logs these as: msg="The \"VAR\" variable is not set. ..."
        unset = sorted(set(re.findall(r'The \\?"(\w+)\\?" variable is not set', str(e))))
        missing = f" (unset: {', '.join(unset)})" if unset else ""
        log.warning(
            f"compose config failed with this environment{missing}; discovered hosts "
            "from the uninterpolated config. ${VARS} in x-deploy or deploy.* labels "
            "stay literal: set them, or use the HOST_NAME/HOST_USER/SSH_PORT overrides.",
            stdout_clean=True,
        )

    try:
        overrides = discovery.env_overrides()
        hosts = discovery.discover_hosts(compose_dict, overrides)
    except ValueError as e:
        log.error(str(e))
        sys.exit(1)

    click.echo(json.dumps(hosts))


@main.command()
def upgrade():
    """Upgrade flow-deploy to the latest release."""
    from flow_deploy import upgrade as upgrade_mod

    sys.exit(upgrade_mod.upgrade())


if __name__ == "__main__":
    main()
