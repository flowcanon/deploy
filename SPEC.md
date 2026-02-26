# Flow Deploy — Specification

A minimal, opinionated deployment tool for Docker Compose + Traefik stacks. Replaces Kamal's useful subset (rolling deploys, health checks, automatic rollback) without its baggage (parallel config, local-first execution, registry coupling).

---

## 1. Design Principles

- **Docker Compose is the single source of truth.** No `deploy.yml`, no parallel declarations. The tool reads `docker-compose.yml` and does its work.
- **Two layers, clean split.** A server-side CLI (`flow-deploy`) handles the single-node deploy lifecycle — pull, scale, health check, cutover. A GitHub Action (`flow-deploy-action`) handles host discovery, fleet orchestration, and SSH fan-out. Single-host projects can skip the action entirely.
- **Accessories are left alone.** Databases, caches, and other stateful services are never restarted during a deploy unless explicitly requested.
- **Builds happen in CI.** The tool does not build images. GitHub Actions builds and pushes to GHCR. The tool pulls and swaps.
- **Failure is a no-op.** If a new container fails its health check, the old container continues serving traffic untouched. The deploy exits nonzero.
- **Logging is the interface.** All output is structured, human-readable, and designed to flow through an SSH session back into GitHub Actions logs.

---

## 2. Concepts

### 2.1 Service Roles

Every service in `docker-compose.yml` is classified by a label:

| Label | Behavior |
|---|---|
| `deploy.role=app` | Rolled during deploy. Health-checked. Rolled back on failure. |
| `deploy.role=accessory` | Never touched during deploy. Only started/stopped via explicit commands. |
| *(no label)* | Ignored entirely. The tool does not interact with unlabeled services. |

### 2.2 Deploy Lifecycle

For each service with `deploy.role=app`, in the order they appear in the compose file:

```
1.  Pull new image           <compose-command> pull <service>
2.  Start new container      <compose-command> up -d --no-deps --no-recreate --scale <service>=2
3.  Wait for health check    poll new container until healthy or timeout
4a. If healthy:
      Graceful shutdown      docker stop --time <drain> <old_id>
      Remove old container   docker rm <old_id>
      Scale back to 1        <compose-command> up -d --no-deps --scale <service>=1
      ✓ Continue to next service
4b. If unhealthy:
      Stop new container     docker stop <new_id> && docker rm <new_id>
      Scale back to 1        <compose-command> up -d --no-deps --scale <service>=1
      ✗ Abort deploy, exit 1
```

Where `<compose-command>` is the project's compose wrapper (see §3.1).

### 2.3 Graceful Shutdown

When stopping the old container, the tool sends SIGTERM and waits for in-flight requests to complete before removing it. This is the default behavior — not optional.

| Label | Default | Description |
|---|---|---|
| `deploy.drain` | `30` | Seconds to wait after SIGTERM before SIGKILL |

This maps directly to `docker stop --time <seconds>`. Traefik removes the container from its pool when it stops, so the drain period gives in-flight requests time to complete. Applications should handle SIGTERM gracefully (stop accepting new connections, finish existing ones).

### 2.4 Health Checks

The tool relies entirely on Docker's native health check mechanism as declared in `docker-compose.yml`. The tool does not define, override, or interpret health checks — it simply polls `docker inspect` for the container's health status.

**A service with `deploy.role=app` MUST have a `healthcheck` defined.** The tool refuses to deploy a service without one.

Configuration (via labels on the service):

| Label | Default | Description |
|---|---|---|
| `deploy.healthcheck.timeout` | `120` | Seconds to wait for healthy before rollback |
| `deploy.healthcheck.poll` | `2` | Seconds between health status polls |

### 2.5 Deploy Order

Services are deployed in the order they appear in `docker-compose.yml`. If a service fails, all subsequently listed services are skipped. Previously deployed services in the same run are NOT rolled back — they already passed health checks and are serving traffic. This matches the expand-then-contract migration discipline: each service should be independently deployable.

If explicit ordering is needed beyond file order, a label is available:

| Label | Default | Description |
|---|---|---|
| `deploy.order` | `100` | Integer. Lower deploys first. Ties broken by file order. |

### 2.6 Host Discovery

Host information is declared in the compose file using Docker Compose's native `x-` extension mechanism. This keeps all deployment topology in the same file that defines the services.

**Single-host (most projects):** Declare defaults at the top level:

```yaml
x-deploy:
  host: app-1.example.com
  user: deploy
  dir: /srv/myapp

services:
  web:
    image: ghcr.io/myorg/myapp:${DEPLOY_TAG:-latest}
    labels:
      deploy.role: app
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]

  worker:
    image: ghcr.io/myorg/myapp:${DEPLOY_TAG:-latest}
    labels:
      deploy.role: app
    healthcheck:
      test: ["CMD", "celery", "inspect", "ping"]

  postgres:
    image: postgres:16
    labels:
      deploy.role: accessory
```

All services inherit `host`, `user`, and `dir` from `x-deploy`. One declaration, no repetition.

**Multi-host:** Per-service labels override the defaults:

```yaml
x-deploy:
  host: app-1.example.com
  user: deploy
  dir: /srv/myapp

services:
  web:
    labels:
      deploy.role: app

  celery-default:
    labels:
      deploy.role: app
      deploy.host: worker-1.example.com

  celery-email:
    labels:
      deploy.role: app
      deploy.host: worker-1.example.com

  centrifugo:
    labels:
      deploy.role: app
      deploy.host: realtime-1.example.com
      deploy.dir: /srv/centrifugo

  postgres:
    labels:
      deploy.role: accessory
      deploy.host: db-1.example.com
```

| Label | Default | Description |
|---|---|---|
| `deploy.host` | `x-deploy.host` | SSH hostname for this service |
| `deploy.user` | `x-deploy.user` | SSH user for this service |
| `deploy.dir` | `x-deploy.dir` | Project directory on the remote host |

Resolution: per-service label wins, then `x-deploy` default, then error if neither is set.

The GitHub Action (§7) reads these values by running `<compose-command> config` in CI, which outputs the fully merged YAML with all overrides applied. It then groups services by host and SSHes to each one.

### 2.7 Pre-Deploy and Post-Deploy Hooks

*Deferred to v2 (see §12.3).* Hook support (pre-deploy commands like migrations, post-deploy commands like cache warming) is a natural extension but not required for v1. Migrations are left to the framework's own boot sequence or manual execution via `flow-deploy exec`.

### 2.8 Versioning and Image Tags

The tool needs to know which image tag to deploy. By default it pulls whatever tag is declared in the compose file (typically `latest` or a pinned tag). This can be overridden at deploy time:

```
flow-deploy deploy --tag abc123f
```

When `--tag` is provided, the tool temporarily overrides the image tag for all `deploy.role=app` services before pulling. This is implemented via the `DEPLOY_TAG` environment variable, which compose files can reference:

```yaml
services:
  web:
    image: ghcr.io/myorg/myapp:${DEPLOY_TAG:-latest}
```

The tool writes the deployed tag to a file (`.deploy-tag`) in the project root for introspection.

---

## 3. Project Layout (Server-Side)

The tool expects a project directory containing a checked-out Git repository with a `docker-compose.yml` at its root. The standard layout:

```
/srv/myapp/
├── docker-compose.yml          # Base service definitions
├── docker-compose.prod.yml     # Production overrides
├── script/
│   ├── dev                     # Local dev compose wrapper
│   └── prod                    # Production compose wrapper
├── Dockerfile
├── .env                        # Environment variables (secrets)
├── .deploy-tag                 # Written by the tool: current deployed tag
├── .deploy-lock                # Written by the tool: deploy lock file
└── (application source)
```

### 3.1 Compose Command Wrapper

The tool never calls `docker compose` directly. It delegates to a compose wrapper script that knows which override files to use for the current environment.

The wrapper is specified by the `COMPOSE_COMMAND` environment variable, which defaults to `script/prod` if the file exists.

Resolution order:

1. `COMPOSE_COMMAND` env var (explicit override)
2. `script/prod` (if present and executable)
3. `docker compose` (bare fallback — no overrides)

A typical `script/prod`:

```bash
#!/usr/bin/env bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml "$@"
```

A wrapper that includes image tagging:

```bash
#!/usr/bin/env bash
DOCKER_IMAGE_TAG="${DOCKER_IMAGE_TAG:-latest}" \
  docker compose -f docker-compose.yml -f docker-compose.prod.yml "$@"
```

This pattern means the tool is completely agnostic about which compose files exist or how they're layered. The project defines its own composition strategy, and the tool just calls the wrapper with the appropriate subcommand arguments (`pull`, `up`, `stop`, etc.).

**All examples in this spec that reference `<compose-command>` refer to whatever this wrapper resolves to.**

The GitHub Action (§7.1) accepts a `command` input that specifies the same wrapper. The action runs `<command> config` in CI to get the fully merged YAML for host discovery and service classification. This means both the CI-side action and the server-side tool use the same wrapper — one declaration, both sides agree.

### 3.2 Secrets / Environment Variables

Secrets are managed via `.env` files, which Docker Compose reads natively. The tool does not manage, rotate, or inject secrets. This is intentionally left to the operator.

The `.env` file should be excluded from version control and provisioned separately (via Ansible, manual setup, or a secrets manager). The tool only requires that the file exists if the compose file references it.

---

## 4. CLI Interface

The tool is invoked as `flow-deploy <command> [options]`.

### 4.1 `flow-deploy deploy`

Perform a rolling deploy of all `deploy.role=app` services.

```
flow-deploy deploy [--tag TAG] [--service SERVICE] [--dry-run]
```

| Flag | Description |
|---|---|
| `--tag TAG` | Override image tag for all app services |
| `--service SERVICE` | Deploy only a specific service (repeatable) |
| `--dry-run` | Show what would happen without executing |

Exit codes:
- `0` — all services deployed successfully
- `1` — one or more services failed, rolled back
- `2` — deploy lock held by another process

### 4.2 `flow-deploy rollback`

Rollback to the previously deployed image tag. Reads the prior tag from `.deploy-tag` history (the tool maintains the last 10 tags).

```
flow-deploy rollback [--service SERVICE]
```

This performs the same rolling deploy lifecycle using the previous tag.

### 4.3 `flow-deploy status`

Show the current state of all managed services.

```
flow-deploy status
```

Output includes: service name, role, container ID, image tag, health status, uptime.

### 4.4 `flow-deploy exec`

Run a command inside a running service container. Convenience wrapper around `docker compose exec`.

```
flow-deploy exec <service> <command...>
```

### 4.5 `flow-deploy logs`

Tail logs for a service. Convenience wrapper around `docker compose logs`.

```
flow-deploy logs <service> [--follow] [--tail N]
```

### 4.6 `flow-deploy self-upgrade`

Update the tool to the latest version from its Git repository.

```
flow-deploy self-upgrade
```

This performs: `git -C <install-path> pull && pip install -e <install-path>`. The tool knows its own install path.

---

## 5. Deploy Locking

Only one deploy may run at a time per project directory. The tool uses a lock file (`.deploy-lock`) containing the PID and timestamp of the running deploy. The lock is:

- Acquired at the start of `deploy` or `rollback`
- Released on completion (success or failure)
- Automatically broken if the holding PID is no longer running (stale lock recovery)
- Reported with a clear message if held by another process (exit code 2)

---

## 6. Logging and Output

All output goes to stdout. The format is human-readable and designed for both terminal use and GitHub Actions log rendering.

```
[12:34:56] ── deploy ──────────────────────────────
[12:34:56] tag: abc123f
[12:34:56] services: web, worker
[12:34:56]
[12:34:56] ▸ web
[12:34:56]   pulling ghcr.io/myorg/myapp:abc123f...
[12:34:58]   pulled (2.1s)
[12:34:58]   starting new container...
[12:34:58]   waiting for health check (timeout: 120s)...
[12:35:11]   healthy (10.2s)
[12:35:11]   draining old container (a1b2c3d, 30s timeout)...
[12:35:14]   ✓ web deployed (16.1s)
[12:35:12]
[12:35:12] ▸ worker
[12:35:12]   pulling ghcr.io/myorg/myapp:abc123f...
[12:35:13]   pulled (1.0s)
[12:35:13]   starting new container...
[12:35:13]   waiting for health check (timeout: 120s)...
[12:35:23]   healthy (9.8s)
[12:35:23]   draining old container (e4f5g6h, 30s timeout)...
[12:35:26]   ✓ worker deployed (13.5s)
[12:35:24]
[12:35:24] ── complete (25.6s) ─────────────────────
```

Failure output:

```
[12:35:12] ▸ worker
[12:35:12]   pulling ghcr.io/myorg/myapp:abc123f...
[12:35:13]   pulled (1.0s)
[12:35:13]   starting new container...
[12:35:13]   waiting for health check (timeout: 120s)...
[12:37:13]   ✗ health check timeout (120.0s)
[12:37:13]   rolling back: stopping new container (x9y8z7w)...
[12:37:14]   rollback complete, old container still serving
[12:37:14]   ✗ worker FAILED
[12:37:14]
[12:37:14] ── FAILED (deploy aborted) ─────────────
```

### 6.1 GitHub Actions Integration

Since the tool runs over SSH, output naturally appears in Actions logs. For richer integration, the tool emits GitHub Actions log commands when it detects the `GITHUB_ACTIONS=true` environment variable (passed through SSH):

- `::group::service-name` / `::endgroup::` for collapsible sections
- `::error::` for deploy failures
- Step summary written to `$GITHUB_STEP_SUMMARY` if available

---

## 7. GitHub Actions

### 7.1 The Action (`flow-deploy-action`)

For multi-host deploys or when you want host discovery from compose labels, use the GitHub Action. The action:

1. Checks out the repo (already done by the workflow)
2. Runs `<command> config` to get the fully merged compose YAML
3. Parses `x-deploy` and `deploy.*` labels to discover hosts
4. Groups services by host
5. SSHes to each host: `git pull` → `flow-deploy deploy --tag <tag>`
6. Streams logs back to GitHub Actions

```yaml
name: Deploy

on:
  push:
    branches: [main]

env:
  REGISTRY: ghcr.io
  IMAGE: ${{ github.repository }}

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    outputs:
      tag: ${{ steps.meta.outputs.version }}
    steps:
      - uses: actions/checkout@v4

      - uses: docker/login-action@v3
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - id: meta
        uses: docker/metadata-action@v5
        with:
          images: ${{ env.REGISTRY }}/${{ env.IMAGE }}
          tags: |
            type=sha,prefix=

      - uses: docker/build-push-action@v5
        with:
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          cache-from: type=gha
          cache-to: type=gha,mode=max

  deploy:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: flowcannon/flow-deploy-action@v1
        with:
          tag: ${{ needs.build.outputs.tag }}
          ssh-key: ${{ secrets.DEPLOY_SSH_KEY }}
          command: script/prod
```

The `command` input tells the action which compose wrapper to use for config parsing. This is the same wrapper `flow-deploy` uses on the server — one declaration, both sides agree.

For staging, swap the wrapper:

```yaml
      - uses: flowcannon/flow-deploy-action@v1
        with:
          tag: ${{ needs.build.outputs.tag }}
          ssh-key: ${{ secrets.DEPLOY_SSH_KEY }}
          command: script/staging
```

### 7.2 Single-Host Shortcut

For single-host projects, the action is optional. A raw SSH command works:

```yaml
  deploy:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - name: Deploy
        run: |
          ssh -o StrictHostKeyChecking=no deploy@${{ secrets.PROD_HOST }} \
            "cd /srv/myapp && \
             git fetch && git checkout -B main origin/main && \
             GITHUB_ACTIONS=true flow-deploy deploy --tag ${{ needs.build.outputs.tag }}"
```

This is the simplest possible deploy: one SSH call, no action, no host discovery. The tool runs locally on the server, calls `script/prod`, and handles the rolling deploy.

---

## 8. Installation

### 8.1 Server Setup (One-Time)

```bash
# Clone the tool
git clone https://github.com/flowcannon/flow-deploy.git /opt/flow-deploy

# Install (editable, so self-upgrade via git pull works)
pip install -e /opt/flow-deploy

# Verify
flow-deploy --version
```

### 8.2 Per-Project Setup (One-Time)

```bash
# Clone the project
git clone git@github.com:myorg/myapp.git /srv/myapp

# Authenticate with GHCR (once per server)
echo $GHCR_TOKEN | docker login ghcr.io -u $GHCR_USER --password-stdin

# Create .env with secrets
cp .env.example .env
vim .env

# Ensure script/prod exists and is executable
chmod +x script/prod

# Start accessories (these run independently of deploys)
cd /srv/myapp
script/prod up -d postgres redis

# First deploy
flow-deploy deploy --tag latest
```

### 8.3 Self-Upgrade Across a Fleet

```bash
# From GitHub Actions or any CI — upgrade all hosts then deploy
for host in web1 web2 web3; do
  ssh deploy@$host "flow-deploy self-upgrade"
done
```

Or embed it in the deploy workflow:

```yaml
- name: Upgrade and Deploy
  run: |
    ssh deploy@${{ secrets.PROD_HOST }} \
      "flow-deploy self-upgrade && \
       cd /srv/myapp && flow-deploy deploy --tag ${{ needs.build.outputs.tag }}"
```

---

## 9. Configuration Summary

The tool has zero configuration files. All behavior is controlled by `x-deploy` defaults and labels on services in `docker-compose.yml`:

**Top-level defaults** (via `x-deploy`):

| Key | Required | Description |
|---|---|---|
| `x-deploy.host` | Yes | Default SSH hostname |
| `x-deploy.user` | Yes | Default SSH user |
| `x-deploy.dir` | Yes | Default project directory on remote host |

**Per-service labels** (override defaults when needed):

| Label | Required | Default | Description |
|---|---|---|---|
| `deploy.role` | Yes | *(none)* | `app` or `accessory` |
| `deploy.host` | No | `x-deploy.host` | SSH hostname for this service |
| `deploy.user` | No | `x-deploy.user` | SSH user for this service |
| `deploy.dir` | No | `x-deploy.dir` | Project directory on remote host |
| `deploy.order` | No | `100` | Deploy order (lower first) |
| `deploy.drain` | No | `30` | Seconds to wait after SIGTERM before SIGKILL |
| `deploy.healthcheck.timeout` | No | `120` | Seconds before rollback |
| `deploy.healthcheck.poll` | No | `2` | Seconds between polls |

Plus the standard Docker/Traefik labels you're already using.

---

## 10. What This Tool Does NOT Do

Explicitly out of scope, by design:

- **Build images.** That's CI's job.
- **Manage secrets.** Use `.env` files, Ansible, Vault, or whatever you prefer.
- **Provision servers.** That's Ansible's job.
- **Manage DNS or SSL.** That's Traefik's job.
- **Run from your laptop.** SSH into the server for manual operations, or trigger from CI.
- **Replace Docker Compose.** It's a thin orchestration layer on top of compose, not a replacement.

---

## 11. V1 → V2 Compatibility

All v1 design decisions are made with the v2 roadmap in mind. Specifically:

- The single-command `flow-deploy deploy` is a convenience that runs prepare + health check + cutover in one shot. v2 splits this into discrete phases without breaking the v1 interface.
- The `deploy.role` label convention is extensible — v2 adds behavior, not new classification schemes.
- The compose command wrapper (§3.1) means the tool never makes assumptions about compose file structure, which keeps it compatible with arbitrarily complex service topologies.
- The `.deploy-tag` file is the only state the tool writes. v2 adds `.deploy-prepare` for two-phase state tracking, but the tag history mechanism is unchanged.
- Host discovery via `x-deploy` and `deploy.host` labels (§2.6) gives the GitHub Action enough information to orchestrate multi-host deploys in v1 (sequential) and v2 (two-phase coordinated).

---

## 12. V2 Roadmap

### 12.1 Two-Phase Fleet Deploys

v1 supports multi-host deploys via the GitHub Action (§7.1), which discovers hosts from compose labels, SSHes to each one, and runs `flow-deploy deploy` sequentially. This works but has a gap: if host 3 of 4 fails, hosts 1 and 2 are already on the new version while hosts 3 and 4 are on the old version.

v2 introduces three commands that decompose the deploy lifecycle to solve this:

| Command | Behavior |
|---|---|
| `flow-deploy prepare --tag TAG` | Pull new image, start new container alongside old, health check. Both containers running. Old still serving traffic. Stop here. |
| `flow-deploy cutover` | Graceful shutdown of old containers. New containers take traffic. |
| `flow-deploy cancel` | Kill new containers. Old containers continue serving. No-op rollback. |

`flow-deploy deploy` remains available as a convenience that runs `prepare` + `cutover` in one shot, preserving full backward compatibility with v1 workflows.

**CI orchestration for fleet deploys:**

```
build ──→ prepare (all nodes in parallel, fail-fast)
              │
              ├── all green ──→ cutover (all nodes in parallel)
              │
              └── any red ───→ cancel (all nodes, best-effort)
```

The tool remains single-node — it has no awareness of other nodes. CI (GitHub Actions) is the fleet orchestrator, using job dependencies and `fail-fast` matrix strategies to coordinate across hosts.

**Stale prepare protection:** If CI crashes between `prepare` and `cutover`, two containers are left running indefinitely. The tool writes a `.deploy-prepare` file with a timestamp. If a prepare is older than a configurable threshold (default: 30 minutes), subsequent commands auto-cancel it and `flow-deploy status` warns about it.

### 12.2 Multi-Container Services

If a service is already scaled to N (e.g., 3 workers), the rolling deploy should scale to N+1, health check the new instance, then kill one old instance, repeating until all N are replaced. Managed via a `deploy.scale` label:

| Label | Default | Description |
|---|---|---|
| `deploy.scale` | `1` | Number of instances for this service |

### 12.3 Pre-Deploy and Post-Deploy Hooks

Hook support for pre-deploy commands (migrations, asset compilation) and post-deploy commands (cache warming, notifications). Deferred from v1 — migrations are handled by the framework's own boot sequence or via manual `flow-deploy exec`.

---

## 13. Resolved Design Decisions

Captured here for context on why v1 works the way it does:

1. **Compose scaling behavior:** Confirmed that `--no-recreate --scale=2` leaves the old container untouched (env vars, volumes, networks unchanged). Validated as the correct approach for rolling deploys.

2. **Compose override detection:** The tool delegates to the project's compose wrapper (`script/prod` or `COMPOSE_COMMAND`). It never detects or assembles compose file stacks itself. Projects define their own composition strategy.

3. **Traefik drain:** Graceful shutdown is the default behavior. `docker stop --time <drain>` sends SIGTERM and waits (default 30s) before SIGKILL. Configurable via `deploy.drain` label.

4. **Hook complexity:** Deferred to v2. Not needed for v1 — migrations are handled by the framework or via manual `flow-deploy exec`.

5. **Fleet coordination:** The server-side tool (`flow-deploy`) is single-node by design. Multi-node coordination is handled by the GitHub Action (`flow-deploy-action`), which discovers hosts from compose labels and orchestrates via SSH. v2's two-phase deploy (`prepare` / `cutover` / `cancel`) gives the action the primitives it needs for coordinated fleet deploys.
