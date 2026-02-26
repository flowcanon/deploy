"""Click entry point — all commands."""

import sys

import click

from flow_deploy import __version__, compose
from flow_deploy import deploy as deploy_mod
from flow_deploy import log, process, tags


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
@click.option("--service", multiple=True, help="Rollback only specific service(s)")
def rollback(service):
    """Rollback to the previously deployed image tag."""
    services_filter = list(service) if service else None
    code = deploy_mod.rollback(services_filter=services_filter)
    sys.exit(code)


@main.command()
def status():
    """Show current state of all managed services."""
    try:
        compose_dict = compose.compose_config()
    except RuntimeError as e:
        log.error(str(e))
        sys.exit(1)

    from flow_deploy import config, containers

    all_services = config.parse_services(compose_dict)
    current = tags.current_tag()

    log.info(f"Current tag: {current or '(none)'}")
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


@main.command(name="exec", context_settings={"ignore_unknown_options": True})
@click.argument("service")
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
def exec_cmd(service, command):
    """Run a command inside a running service container."""
    if not command:
        click.echo("Error: No command specified", err=True)
        sys.exit(1)
    compose_cmd = compose.resolve_command()
    code = process.run_streaming(compose_cmd + ["exec", service] + list(command))
    sys.exit(code)


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
    code = process.run_streaming(compose_cmd + args)
    sys.exit(code)


if __name__ == "__main__":
    main()
