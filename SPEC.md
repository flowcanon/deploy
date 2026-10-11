# Flow Deploy — Specification

A minimal, opinionated deployment tool for Docker Compose + Traefik stacks. Replaces Kamal's useful subset (rolling deploys, health checks, automatic failure recovery) without its baggage (parallel config, local-first execution, registry coupling).

---

## 1. Design Principles

- **Docker Compose is the single source of truth.** No `deploy.yml`, no parallel declarations. The tool reads `docker-compose.yml` and does its work.
- **Two layers, clean split.** A server-side CLI (`flow-deploy`) handles the single-node deploy lifecycle — pull, scale, health check, cutover. A GitHub Action (`flow-deploy-action`) handles host discovery, fleet orchestration, and SSH fan-out. Single-host projects can skip the action entirely.
- **Accessories are left alone.** Databases, caches, and other stateful services are never restarted during a deploy unless explicitly requested.
- **Builds happen in CI.** The tool does not build images. GitHub Actions builds and pushes to GHCR. The tool pulls and swaps.
- **Failure is a no-op.** If a new container fails its health check, the old container continues serving traffic untouched. Any failure puts every app service back on the image it ran before the deploy and exits 1, or exits 3 naming what it couldn't put back.
- **Logging is the interface.** All output is structured, human-readable, and designed to flow through an SSH session back into GitHub Actions logs.

---

## 2. Concepts

### 2.1 Service Roles

Every service in `docker-compose.yml` is classified by a label:

| Label | Behavior |
|---|---|
| `deploy.role=app` | Rolled during deploy. Health-checked. Old container preserved on failure. |
| `deploy.role=accessory` | Never touched during deploy. Only started/stopped via explicit commands. |
| *(no label)* | Ignored entirely. The tool does not interact with unlabeled services. |

### 2.2 Deploy Lifecycle

The deploy runs as a single transaction — `flow-deploy deploy` owns the full lifecycle including git operations. When `--tag <tag>` is provided, the value serves double duty: it is both the Docker image tag and the git ref to checkout. It is normally a release version (`v1.4.0`); a commit SHA works too (§2.8). When `--tag` is omitted, the tool deploys the current HEAD — the git checkout is skipped and `DEPLOY_TAG` is set to the current commit SHA.

**Pre-flight and git checkout (before any service work):**

```
0a. Dirty check             git status --porcelain
                             If non-empty → log "working tree is dirty — deploy aborted", exit 1
0b. Fetch                   git fetch --tags --force origin
0c. Record previous SHA     previous_sha = git rev-parse HEAD
0d. Checkout (detached)     git checkout --detach <tag>   (skipped when --tag is omitted)
0e. Render config           DEPLOY_TAG=<tag> <compose-command> config
0f. Project check            docker ps by compose's working_dir and service labels:
                             another compose project running these services here → refuse
0g. Snapshot                for each app service: docker ps (running + restarting),
                             docker inspect → image IDs; the most common one is
                             "previous" (warn if they differ). Its restore tag is the
                             tag it was started from if that's a git ref
                             (git rev-parse --verify) still naming that image
                             (docker image inspect) and isn't <tag>; else <previous_sha>
                             If listing or inspecting fails → restore repo, exit 1
```

The snapshot is what a revert restores: the image each service was running, named by a git tag or commit SHA (§2.8).

**For each service with `deploy.role=app`, in the order they appear in the compose file:**

```
1.  Pull new image           <compose-command> pull <service>
2.  Start new container      <compose-command> up -d --no-deps --no-recreate --scale <service>=2
3.  Wait for health check    poll new container until healthy or timeout
4a. If healthy:
      Graceful shutdown      docker stop --time <drain> <old_id>
      Remove old container   docker rm <old_id>
      Scale back to 1        <compose-command> up -d --no-deps --no-recreate --scale <service>=1
                             (skipped, with a warning, if the old one couldn't be removed)
      ✓ Continue to next service
4b. If unhealthy:
      Stop new container     docker stop <new_id> && docker rm <new_id>
      Scale back to 1        <compose-command> up -d --no-deps --no-recreate --scale <service>=1
      ✗ Revert (below), exit 1
```

Services labelled `deploy.strategy=drain-first` are instead stopped before any rolling service and started after them; see §2.5.

**On success:** the server is in detached HEAD at `<tag>`, and `.git/deploy-tag` records the tag (§2.8). Log `HEAD detached at <tag>`.

**On failure** (a failed step, a signal, or an unexpected error), the deploy reverts:

```
R1. Restore repo             git checkout --detach <previous_sha>
R1b. Failed service          remove stopped containers its abort couldn't remove
R2. Roll back shipped        for each rolling service that already deployed, in reverse order:
                               remove its stopped leftovers (compose would restart them), then
                               DEPLOY_TAG=<restore tag> rolling deploy (no pull) back to its
                               previous image; if a container on the previous image
                               survived the cutover, remove the new ones instead;
                               a service that had no containers is removed
R3. Restore drain-first      for each drained service, in deploy order:
                               remove its stopped leftovers on other images,
                               stop + remove containers not on its previous image,
                               DEPLOY_TAG=<restore tag> <compose-command> up -d --no-deps
                                 --no-recreate --scale <service>=<previous replicas>,
                               then wait for its health check (unless skipped);
                               unhealthy → not reverted (exit 3)
                               (left stopped if it wasn't running before)
R4. Record                   .git/deploy-tag = the restore tag (cleared if services differ)
R5. Release the lock         always
```

Exit `1` if everything was put back, `3` if anything couldn't be (§4.1). The invariant is preserved: `git rev-parse HEAD` matches the code of the images actively serving traffic.

Where `<compose-command>` is the project's compose wrapper (see §3.1).

### 2.3 Graceful Shutdown

When stopping the old container, the tool sends SIGTERM and waits for in-flight requests to complete before removing it. This is the default behavior — not optional.

| Label | Default | Description |
|---|---|---|
| `deploy.drain` | `30` | Seconds to wait after SIGTERM before SIGKILL |

This maps directly to `docker stop --time <seconds>`. Traefik removes the container from its pool when it stops, so the drain period gives in-flight requests time to complete. Applications should handle SIGTERM gracefully (stop accepting new connections, finish existing ones).

After each drain and cutover stop, the tool checks the stopped container: if it exited 137 (SIGKILL) or the stop used the whole timeout, it warns that the container didn't exit on SIGTERM. The usual cause is PID 1 being a shell that doesn't forward signals (a shell-form `CMD`, or an entrypoint that doesn't `exec`). It's a warning, never a failure; the tool doesn't inspect PID 1 itself.

### 2.4 Health Checks

The tool relies entirely on Docker's native health check mechanism as declared in `docker-compose.yml`. The tool does not define, override, or interpret health checks — it simply polls `docker inspect` for the container's health status.

**A service with `deploy.role=app` MUST have a `healthcheck` defined.** The tool refuses to deploy a service without one.

Configuration (via labels on the service):

| Label | Default | Description |
|---|---|---|
| `deploy.healthcheck.timeout` | `120` | Seconds to wait for healthy before aborting |
| `deploy.healthcheck.poll` | `2` | Seconds between health status polls |

### 2.5 Deploy Order

Services are deployed in the order they appear in `docker-compose.yml`. If a service fails, all subsequently listed services are skipped, and services that already deployed in the same run are rolled back to their previous image (§2.2, R2), so no service runs new code beside others on old code. Old code then runs against whatever the new code did (a migration, say), which is the expand-then-contract discipline every rolling deploy already relies on.

If explicit ordering is needed beyond file order, a label is available:

| Label | Default | Description |
|---|---|---|
| `deploy.order` | `100` | Integer. Lower deploys first. Ties broken by file order. |
| `deploy.strategy` | `rolling` | `rolling` or `drain-first` |

**Drain-first services** (queue workers, schedulers) never overlap old and new. The deploy runs in three phases:

1. **Drain** drain-first services in *reverse* `deploy.order`: pull the new image, then `docker stop --time <deploy.drain>` every running or restarting container, then remove it. Nothing rolls until every drain has finished.
2. **Roll** rolling services in `deploy.order`, as above.
3. **Start** drain-first services in `deploy.order` on the new tag, at their previous replica count (at least 1), each health-checked. Every start is attempted; a failure removes what it started.

A failure in any phase reverts everything (§2.2). A signal that arrives while a drain is stopping a container is held until that container has stopped and been removed, then the deploy reverts.

### 2.6 Host Discovery

Host information is declared in the compose file using Docker Compose's native `x-` extension mechanism. This keeps all deployment topology in the same file that defines the services.

**Single-host (most projects):** Declare defaults at the top level:

```yaml
x-deploy:
  host: app-1.example.com
  user: deploy
  port: 22
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

All services inherit `host`, `user`, `port`, and `dir` from `x-deploy`. One declaration, no repetition.

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
| `deploy.port` | `x-deploy.port` | SSH port for this service |
| `deploy.dir` | `x-deploy.dir` | Project directory on the remote host |

**Resolution order** (highest priority wins):

1. GitHub Actions environment variable (`HOST_NAME`, `HOST_USER`, `SSH_PORT`)
2. Per-service label (`deploy.host`, `deploy.user`, `deploy.port`, `deploy.dir`)
3. Top-level `x-deploy` default
4. Error if none is set (for `host`)

Environment variable overrides exist because `x-deploy` values are committed to the repository. For public repos, operators may prefer to keep hostnames, usernames, and SSH ports out of version control. GitHub Actions [environment variables](https://docs.github.com/en/actions/learn-github-actions/variables) provide this — set them in a GitHub environment and reference them in the deploy workflow.

| Environment Variable | Overrides | Description |
|---|---|---|
| `HOST_NAME` | `x-deploy.host` / `deploy.host` | SSH hostname for all services |
| `HOST_USER` | `x-deploy.user` / `deploy.user` | SSH user for all services |
| `SSH_PORT` | `deploy.port` / `x-deploy.port` | SSH port for all connections |

The GitHub Action (§7) reads these values by running `<compose-command> config` in CI, which outputs the fully merged YAML with all overrides applied. It then groups services by host, applies any environment variable overrides, and SSHes to each one.

### 2.7 Pre-Deploy and Post-Deploy Hooks

*Deferred to v2 (see §12.3).* Hook support (pre-deploy commands like migrations, post-deploy commands like cache warming) is a natural extension but not required for v1. Migrations are left to the framework's own boot sequence or manual execution via `flow-deploy exec`.

### 2.8 Versioning and Image Tags

The tool needs to know which image tag to deploy. When `--tag` is omitted, the tool uses the current HEAD SHA — deploying whatever is currently checked out. When `--tag` is provided, the tool checks out that ref and uses it as the image tag:

```
flow-deploy deploy --tag v1.4.0
flow-deploy deploy --tag 3f2c9e1d...   # a commit SHA works the same way
```

**Git side.** Tags are fetched with `git fetch --tags --force origin`, so a new release tag is always present and a re-pushed (moved) tag updates locally. `git checkout --detach <tag>` accepts lightweight tags, annotated tags (it checks out the tagged commit), and SHAs.

**Image side.** The `--tag` value is passed to every compose command as the `DEPLOY_TAG` environment variable, which each `deploy.role=app` image must end with:

```yaml
services:
  web:
    image: ghcr.io/myorg/myapp:${DEPLOY_TAG:-latest}
    # or image: ${DOCKER_IMAGE}, with DOCKER_IMAGE=ghcr.io/myorg/myapp:${DEPLOY_TAG:-latest} in .env
```

CI must push the image under every tag it may deploy by: the version, and always the full commit SHA, which a revert falls back to. The deploy renders its compose config with `DEPLOY_TAG` set and warns when an app image doesn't end in `:<tag>`; such a service keeps whatever image the compose file names.

**What's running.** Restores are named by git: a release tag or a commit SHA. The pre-deploy snapshot (§2.2, 0g) records each service's running image ID and the tag it was started from. A revert uses that tag when it is a git ref, still names the running image locally, and isn't the tag being deployed (the deploy pulls that, which would move a re-pushed tag). Otherwise (started from `:latest`, from a tag re-pushed since, from a digest, untagged, or a redeploy of the same tag) it uses the full SHA of the commit checked out before the deploy, so CI must push every image under its full commit SHA. If that image isn't in the registry, the restore fails and the deploy exits 3 (for a service with `build:`, compose builds it from the restored checkout instead). Redeploying the commit that's already checked out leaves no other git name for the running build: if its image was rebuilt under the same tag, a revert restores the rebuild, and the deploy warns up front. flow-deploy creates no tags and writes nothing in the working tree.

**What's deployed.** `flow-deploy tag` (§4.7) answers "which tag is deployed here": the tag of the running app containers when they all agree; if they run one image under `:latest` and one other tag (after a revert from `:latest`), that other tag; otherwise, when nothing is running, `.git/deploy-tag`. Anything else mixed, or untagged, is an error, never a guess. The record is written after every successful deploy of all app services, rewritten to the restore tag after a revert, and cleared by a `--service` deploy that leaves some app services out (they're then on different tags).

**Manual commands.** With `${DEPLOY_TAG:-latest}`, running `<compose-command> up -d` on the host with `DEPLOY_TAG` unset would move every service to `:latest`. Use `flow-deploy compose <args>` (§4.8), which sets `DEPLOY_TAG` to the deployed tag, or have the wrapper default it (§3.1).

The deployed commit is always available via `git rev-parse HEAD` — the server is kept in detached HEAD at the deployed commit.

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
└── (application source)
```

The tool writes no state files to the working tree. The deploy lock is stored at `.git/deploy-lock`, the deployed tag and commit at `.git/deploy-tag`, and a log of the last 20 deploys at `.git/flow-deploy.log` (all invisible to `git status`). The current deployed commit is always `git rev-parse HEAD`.

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

A wrapper that keeps manual commands on the deployed tag (§2.8):

```bash
#!/usr/bin/env bash
if [ -z "${DEPLOY_TAG:-}" ]; then
  DEPLOY_TAG=$(flow-deploy tag) || {
    echo "script/prod: set DEPLOY_TAG (flow-deploy tag couldn't tell)" >&2
    exit 1
  }
  export DEPLOY_TAG
fi
docker compose -f docker-compose.yml -f docker-compose.prod.yml "$@"
```

It fails closed: when `flow-deploy tag` can't tell, the wrapper stops rather than run compose on `:latest`. `flow-deploy` always sets `DEPLOY_TAG` when it calls the wrapper (to a placeholder while looking up the tag, and in `flow-deploy config`), so the default never asks it and never recurses. On a host with nothing running, pass `DEPLOY_TAG` explicitly.

**The wrapper contract.** flow-deploy runs `config`, `pull` and `up` through the wrapper, and `exec`, `logs` and `flow-deploy compose` pass through it too. A wrapper may set up the environment (sourcing `.env`, defaulting `DEPLOY_TAG` as below), as long as it:

- passes its arguments to compose unchanged
- prints nothing extra on stdout (`config`'s output is parsed)
- exits with compose's exit status
- never prompts, and never changes the project or any files

flow-deploy passes `DEPLOY_TAG` on every compose call it makes: the deploy tag, the deployed tag, or a lookup placeholder when it doesn't matter or isn't known (`config`, `status`, `exec`, `logs`). So a wrapper's own `DEPLOY_TAG` lookup never runs from inside flow-deploy. That matters for a fail-closed wrapper: a call without `DEPLOY_TAG` would exit 1, failing whatever flow-deploy was doing.

This pattern means the tool is completely agnostic about which compose files exist or how they're layered. The project defines its own composition strategy, and the tool just calls the wrapper with the appropriate subcommand arguments (`pull`, `up`, `stop`, etc.).

**All examples in this spec that reference `<compose-command>` refer to whatever this wrapper resolves to.**

The GitHub Action (§7.1) accepts a `command` input that specifies the same wrapper. The action runs `<command> config` in CI to get the fully merged YAML for host discovery and service classification. This means both the CI-side action and the server-side tool use the same wrapper — one declaration, both sides agree.

### 3.2 Secrets / Environment Variables

Secrets are managed via `.env` files, which Docker Compose reads natively. The tool does not manage, rotate, or inject secrets. This is intentionally left to the operator.

The `.env` file should be excluded from version control and provisioned separately (via Ansible, manual setup, or a secrets manager). The tool only requires that the file exists if the compose file references it.

**Accessories and `.env`.** Compose recreates every service that reads an `env_file` when the file changes. The tool never touches accessories, so that recreate happens on the next manual `up -d`. Keep accessories off the app's `.env`: drop `env_file` and list the variables they need under `environment:` (interpolated from `.env`). Scope manual changes: `flow-deploy compose up -d --no-deps <service>`.

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
| `--tag TAG` | Image tag and git ref to deploy: a version tag or a commit SHA (defaults to current HEAD SHA) |
| `--service SERVICE` | Deploy only a specific service (repeatable) |
| `--dry-run` | Show what would happen without executing |

Exit codes:
- `0` — all services deployed successfully
- `1` — deploy failed (dirty tree, git error, unhealthy service, etc.) and everything it touched was put back
- `2` — deploy lock held by another process
- `3` — deploy failed and could not be fully reverted (the git restore, a rollback or restore, a restored service's health check, or a failed service's own cleanup failed); the lock is still released

Every run is recorded in `.git/flow-deploy.log` with a header (time, PID, arguments) and its exit code, keeping the last 20 runs, so a deploy nobody is watching (a cancelled CI job, §7.1) still leaves a record.

To deploy a previous version, run `flow-deploy deploy --tag <previous-version>` (or its SHA). Find it in your CI history, `git tag` on the server, or `flow-deploy status` before deploying.

### 4.2 `flow-deploy status`

Show the current state of all managed services.

```
flow-deploy status
```

Output includes: current HEAD SHA, the deployed tag and where it came from (§2.8), the last deploy from `.git/flow-deploy.log` and how it ended (or that it never recorded an exit: still running, or killed), and per container: service name, role, container ID, image, health status.

### 4.3 `flow-deploy exec`

Run a command inside a running service container. Convenience wrapper around `docker compose exec`.

```
flow-deploy exec <service> <command...>
```

### 4.4 `flow-deploy logs`

Tail logs for a service. Convenience wrapper around `docker compose logs`.

```
flow-deploy logs <service> [--follow] [--tail N]
```

### 4.5 `flow-deploy config`

Output resolved deploy configuration as JSON. Used by the GitHub Action to discover hosts without requiring the Python source tree.

```
flow-deploy config [--command COMMAND]
```

| Flag | Description |
|---|---|
| `--command COMMAND` | Override compose command (e.g. `docker compose`) |

Reads `COMPOSE_COMMAND` env var (same as all other commands), runs `<compose-command> config`, parses host discovery, and emits a JSON array of host groups to stdout. Errors go to stderr with exit code 1.

It sets `DEPLOY_TAG` to a placeholder, so a wrapper that defaults it (§3.1) doesn't ask. Discovery doesn't need the app's environment: if compose rejects the project (CI has no `.env`, so an `image: ${DOCKER_IMAGE}` is empty), it retries with `config --no-interpolate` and warns on stderr, naming the unset variables. `${VARS}` in `x-deploy` and `deploy.*` labels then stay literal. Stdout stays pure JSON even on GitHub Actions; the `::warning::` goes to stderr.

### 4.6 `flow-deploy upgrade`

Update the tool to the latest release.

```
flow-deploy upgrade
```

This detects the system's libc (musl or glibc), downloads the latest binary from the GitHub release, and atomically replaces the current binary. Works when run manually on the server or via SSH from a CI pipeline.

### 4.7 `flow-deploy tag`

Print the deployed tag (§2.8) to stdout, and nothing else, for scripts:

```
DEPLOY_TAG=${DEPLOY_TAG:-$(flow-deploy tag)}
```

Exits 1 with the reason on stderr when it can't tell (images that disagree or are untagged, as in §2.8, or nothing running and no record).

### 4.8 `flow-deploy compose`

Run the compose command (§3.1) with `DEPLOY_TAG` set to the deployed tag, for manual operations on the host:

```
flow-deploy compose up -d
flow-deploy compose run --rm web python manage.py migrate
```

An explicit `DEPLOY_TAG` in the environment wins. Refuses (exit 1) when the deployed tag can't be told, rather than fall back to `:latest`. The exit code is compose's.

---

## 5. Deploy Locking

Only one deploy may run at a time per project directory. The tool uses a lock file (`.git/deploy-lock`) containing the PID and timestamp of the running deploy. The lock is stored inside `.git/` to avoid dirtying the working tree. The lock is:

- Acquired before any mutations (git checkout, container changes)
- Released on completion: success, failure, a handled signal (SIGTERM, SIGINT, SIGHUP), or an unexpected error. That includes a deploy that couldn't be fully reverted, which exits `3` to say someone needs to look. A retained lock would not have stopped the next deploy anyway: see the next point.
- Automatically broken if the holding PID is no longer running (stale lock recovery)
- Reported with a clear message if held by another process (exit code 2)

---

## 6. Logging and Output

Progress goes to stdout; warnings and errors go to stderr. The format is human-readable and designed for both terminal use and GitHub Actions log rendering. Commands whose stdout is data (`flow-deploy config`, `flow-deploy tag`) keep it to that data alone, GitHub annotations included. Every deploy's output is also recorded in `.git/flow-deploy.log` (§4.1).

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
[12:37:13]   aborting: stopping new container (x9y8z7w)...
[12:37:14]   aborted, old container still serving
[12:37:14]   ✗ worker FAILED
[12:37:14]
[12:37:14]   restoring repo to a1b2c3d...
[12:37:14] ── FAILED (deploy aborted) ─────────────
```

Dirty-tree output:

```
[12:34:56] ERROR: working tree is dirty — deploy aborted
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
2. Runs `flow-deploy config` (§4.5): it renders `<command> config`, parses `x-deploy` and `deploy.*` labels, and prints the host groups as JSON on stdout. The action reads only that JSON, never compose's output.
3. Groups services by host (in that JSON)
4. SSHes to each host: `flow-deploy deploy --tag <tag>`
5. Streams logs back to GitHub Actions

**Cancelling the workflow does not cancel the remote deploy.** The action runs ssh without a pty, so sshd sends the remote `flow-deploy` no signal when the connection drops; `tests/integration/test_ci_cancel.py` verifies this against a real sshd. The deploy runs to completion, or reverts on its own failure, and its output to the closed session is discarded. Running ssh with `-tt` would deliver SIGHUP and make a cancel revert, but every dropped connection would then revert a deploy too.

```yaml
name: Deploy

on:
  push:
    branches: [main]
    tags: ["v*"]

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
      tag: ${{ github.ref_type == 'tag' && github.ref_name || github.sha }}
    steps:
      - uses: actions/checkout@v4

      - uses: docker/login-action@v3
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      # The version tag on release pushes, and always the full SHA.
      - id: meta
        uses: docker/metadata-action@v5
        with:
          images: ${{ env.REGISTRY }}/${{ env.IMAGE }}
          tags: |
            type=ref,event=tag
            type=sha,format=long,prefix=

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

      - uses: flowcanon/deploy-action@master
        with:
          tag: ${{ needs.build.outputs.tag }}
          ssh-key: ${{ secrets.DEPLOY_SSH_KEY }}
          command: script/prod
```

The `command` input tells the action which compose wrapper to use for config parsing. This is the same wrapper `flow-deploy` uses on the server — one declaration, both sides agree.

For staging, swap the wrapper:

```yaml
      - uses: flowcanon/deploy-action@master
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
             GITHUB_ACTIONS=true flow-deploy deploy --tag ${{ needs.build.outputs.tag }}"
```

This is the simplest possible deploy: one SSH call, no action, no host discovery. The tool handles git operations (fetch, detached checkout), calls `script/prod`, and runs the rolling deploy. No separate `git pull` or `git checkout` is needed — the tool owns the full transaction.

---

## 8. Installation

### 8.1 Server Setup (One-Time)

```bash
# Install the latest binary
curl -fsSL https://deploy.flowcanon.com/install | sh

# Verify
flow-deploy --version
```

This installs a standalone binary to `~/.local/bin`. Set `INSTALL_DIR` to install elsewhere. The installer detects the system's libc (musl or glibc) and downloads the appropriate binary from GitHub releases.

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
flow-deploy deploy
```

### 8.3 Upgrading Across a Fleet

```bash
# From GitHub Actions or any CI — upgrade all hosts then deploy
for host in web1 web2 web3; do
  ssh deploy@$host "flow-deploy upgrade"
done
```

Or embed it in the deploy workflow:

```yaml
- name: Upgrade and Deploy
  run: |
    ssh deploy@${{ secrets.PROD_HOST }} \
      "flow-deploy upgrade && \
       cd /srv/myapp && flow-deploy deploy --tag ${{ needs.build.outputs.tag }}"
```

---

## 9. Configuration Summary

The tool has zero configuration files. All behavior is controlled by `x-deploy` defaults, labels on services in `docker-compose.yml`, and optional environment variable overrides:

**Top-level defaults** (via `x-deploy`):

| Key | Required | Description |
|---|---|---|
| `x-deploy.host` | No* | Default SSH hostname |
| `x-deploy.user` | No* | Default SSH user |
| `x-deploy.port` | No | Default SSH port |
| `x-deploy.dir` | No | Default project directory on remote host |

\* Required unless overridden by an environment variable or per-service label.

**Per-service labels** (override `x-deploy` defaults):

| Label | Required | Default | Description |
|---|---|---|---|
| `deploy.role` | Yes | *(none)* | `app` or `accessory` |
| `deploy.host` | No | `x-deploy.host` | SSH hostname for this service |
| `deploy.user` | No | `x-deploy.user` | SSH user for this service |
| `deploy.port` | No | `x-deploy.port` | SSH port for this service |
| `deploy.dir` | No | `x-deploy.dir` | Project directory on remote host |
| `deploy.order` | No | `100` | Deploy order (lower first) |
| `deploy.strategy` | No | `rolling` | `rolling` or `drain-first` (§2.5) |
| `deploy.drain` | No | `30` | Seconds to wait after SIGTERM before SIGKILL |
| `deploy.healthcheck.timeout` | No | `120` | Seconds before aborting |
| `deploy.healthcheck.poll` | No | `2` | Seconds between polls |

**Environment variable overrides** (highest priority, set via GitHub Actions environment):

| Variable | Overrides | Description |
|---|---|---|
| `HOST_NAME` | `deploy.host` / `x-deploy.host` | SSH hostname for all services |
| `HOST_USER` | `deploy.user` / `x-deploy.user` | SSH user for all services |
| `SSH_PORT` | `deploy.port` / `x-deploy.port` | SSH port for all connections |

These are useful for public repositories where hostnames and usernames should not be committed to version control.

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
- The tool writes no state to the working tree. The deploy lock lives in `.git/deploy-lock`, the deployed tag and commit in `.git/deploy-tag`, the last 20 deploys' output in `.git/flow-deploy.log`, and the current deployed commit is always `git rev-parse HEAD`. v2 adds `.git/deploy-prepare` for two-phase state tracking.
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
