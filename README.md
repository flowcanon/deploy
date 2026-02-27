# Flow Deploy

Minimal, opinionated rolling deploys for Docker Compose + Traefik stacks.

Replaces Kamal's useful subset — rolling deploys, health checks, automatic rollback — without its baggage.

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

Every `deploy.role=app` service **must** have a `healthcheck`. Services without a `deploy.role` label are ignored.

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
5. **Rollback** — if unhealthy, remove the new container. Old container is untouched.

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

## Host Discovery

For CI/CD orchestration across multiple hosts, declare topology in your compose file:

```yaml
x-deploy:
  host: app-1.example.com
  user: deploy
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

Resolution order: per-service label > `x-deploy` default > GitHub Actions variable.

See [GitHub Actions Setup](docs/github-actions.md) for CI/CD integration.

## CLI Commands

```
flow-deploy deploy [--tag TAG] [--service NAME] [--dry-run]
flow-deploy rollback [--service NAME]
flow-deploy status
flow-deploy exec SERVICE COMMAND...
flow-deploy logs SERVICE [-f] [-n LINES]
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
