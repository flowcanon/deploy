# Flow Deploy

[![CI](https://github.com/flowcanon/deploy/actions/workflows/ci.yml/badge.svg)](https://github.com/flowcanon/deploy/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/flowcanon/deploy)](https://github.com/flowcanon/deploy/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Coverage: 90%+](https://img.shields.io/badge/coverage-90%25+-brightgreen)](https://github.com/flowcanon/deploy/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10+-3776AB?logo=python&logoColor=white)](https://python.org)

Minimal, opinionated rolling deploys for Docker Compose + Traefik stacks.

Replaces Kamal's useful subset — rolling deploys, health checks, automatic failure recovery — without its baggage.

## Install

```sh
curl -fsSL https://deploy.flowcanon.com/install | sh
```

This installs the `flow-deploy` binary to `~/.local/bin`. Start a new login shell if it's not in your `PATH`.

## Quick Start

**1. Label your services** in `docker-compose.yml`:

```yaml
services:
  web:
    image: ghcr.io/myorg/myapp:${DEPLOY_TAG:-latest}
    labels:
      deploy.role: app
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 10s
      timeout: 5s
      retries: 5
```

Every `deploy.role=app` service **must** have a `healthcheck` defined in compose (Dockerfile `HEALTHCHECK` is not detected). To opt out, set `deploy.healthcheck.skip=true`. Services without a `deploy.role` label are ignored.

**2. Deploy:**

```sh
flow-deploy deploy --tag abc123f
```

```
[12:34:56] ── deploy ──────────────────────────────
[12:34:56] tag: abc123f
[12:34:58] ▸ web
[12:34:58]   pulling ghcr.io/myorg/myapp:abc123f...
[12:35:02]   pulled (3.8s)
[12:35:02]   starting new container...
[12:35:05]   waiting for health check (timeout: 120s)...
[12:35:08]   healthy (6.2s)
[12:35:08]   draining old container (a1b2c3d, 30s timeout)...
[12:35:11]   ✓ web deployed (16.1s)
[12:35:11] ── complete (16.1s) ─────────────────────
```

That's it. If the health check fails, the old container keeps serving traffic and the deploy exits `1`.

## How It Works

For each `deploy.role=app` service, in order:

1. **Pull** the new image
2. **Scale to 2** — start a new container alongside the old one
3. **Health check** — poll the new container until healthy or timeout
4. **Cutover** — if healthy, gracefully drain the old container and scale back to 1
5. **Abort** — if unhealthy, remove the new container. Old container is untouched.

## Service Roles

| Label | Behavior |
|---|---|
| `deploy.role=app` | Rolled during deploy. Health-checked. Rolled back on failure. |
| `deploy.role=accessory` | Never touched during deploy. |
| *(no label)* | Ignored entirely. |

## Configuration Labels

All configuration is via Docker labels on your services:

| Label | Default | Description |
|---|---|---|
| `deploy.role` | — | `app` or `accessory` (required) |
| `deploy.order` | `100` | Deploy order. Lower goes first. |
| `deploy.drain` | `30` | Seconds to wait for graceful shutdown |
| `deploy.healthcheck.timeout` | `120` | Seconds to wait for healthy |
| `deploy.healthcheck.poll` | `2` | Seconds between health polls |
| `deploy.healthcheck.skip` | `false` | Skip healthcheck validation and waiting |
| `deploy.strategy` | `rolling` | `rolling` or `drain-first` (see below) |

### Drain-first services

Rolling deploys briefly run old and new containers side by side. That's right
for web servers, but wrong for queue workers and schedulers. An old worker
that's still draining keeps running old code after the new web container has
migrated the database.

Label those services `deploy.strategy: drain-first` and the deploy runs in three
phases:

1. **Drain.** Each drain-first service, in `deploy.order`, pulls its new image
   and then stops its running containers. Stopping is `docker stop --time
   <deploy.drain>`: SIGTERM first, so Celery-style workers finish their current
   tasks and stop taking new ones, then SIGKILL when the drain expires. Nothing
   rolls until every drain has finished.
2. **Roll.** Every rolling service deploys as usual.
3. **Start.** Each drain-first service starts on the new tag and must pass its
   health check.

The service is down from the start of its drain until phase 3. Queued work
waits in the broker and the new worker picks it up.

If the deploy fails before phase 3, drained services are restarted on the image
tag they were running before, at the same replica count. This includes a
container that won't stop or be removed, which aborts the deploy before
anything rolls.

Every drain-first service is started in phase 3, even if an earlier one fails.
If one fails its start:

- When no rolling service deployed (e.g. `--service worker`), it's restored on
  its previous tag.
- Otherwise it's left down. Its old code would run against whatever the rolling
  phase changed, so an operator takes it from there.

Deploying rolling services with `--service` while leaving drain-first services
out prints a warning, since those keep running old code.

```yaml
services:
  worker:
    labels:
      deploy.role: app
      deploy.strategy: drain-first
      deploy.drain: "3660"   # longest job + margin
```

## Host Discovery

For CI/CD orchestration across multiple hosts, declare topology in your compose file:

```yaml
x-deploy:
  host: app-1.example.com
  user: deploy
  port: 22
  dir: /srv/myapp

services:
  web:
    labels:
      deploy.role: app
  worker:
    labels:
      deploy.role: app
      deploy.host: worker-1.example.com  # override per-service
```

Resolution order: GitHub Actions variable > per-service label > `x-deploy` default.

See [GitHub Actions Setup](docs/github-actions.md) for CI/CD integration.

## CLI Commands

```
flow-deploy deploy [--tag TAG] [--service NAME] [--dry-run]
flow-deploy status
flow-deploy exec SERVICE COMMAND...
flow-deploy logs SERVICE [-f] [-n LINES]
flow-deploy upgrade
```

## Compose Command Resolution

The tool resolves the compose command in this order:

1. `COMPOSE_COMMAND` environment variable
2. `script/prod` (if executable)
3. `docker compose`

## Links

- [GitHub Actions Setup](docs/github-actions.md)
- [Full Specification](SPEC.md)
- [GitHub](https://github.com/flowcanon/deploy)
