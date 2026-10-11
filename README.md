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

The tag is both a git ref (checked out on the host) and the image tag: every app
service's image must end in `:${DEPLOY_TAG}`, usually `:${DEPLOY_TAG:-latest}`
(see [Image Tags](#image-tags)).

Every `deploy.role=app` service **must** have a `healthcheck` defined in compose (Dockerfile `HEALTHCHECK` is not detected). To opt out, set `deploy.healthcheck.skip=true`. Services without a `deploy.role` label are ignored.

**2. Deploy:**

```sh
flow-deploy deploy --tag v1.4.0     # a release tag, or a commit SHA
```

```
[12:34:56] ── deploy ──────────────────────────────
[12:34:56] tag: v1.4.0
[12:34:58] ▸ web
[12:34:58]   pulling ghcr.io/myorg/myapp:v1.4.0...
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

1. **Drain.** Drain-first services stop in *reverse* `deploy.order`. Each one
   pulls its new image, then stops its running containers. Stopping is
   `docker stop --time <deploy.drain>`: SIGTERM first, so Celery-style workers
   finish their current tasks and stop taking new ones, then SIGKILL when the
   drain expires. Nothing rolls until every drain has finished.
2. **Roll.** Rolling services deploy in `deploy.order`, as usual.
3. **Start.** Drain-first services start in `deploy.order` on the new tag, and
   each must pass its health check.

So with `web` (100), `api` (200) and `worker` (300), the worker drains first,
then web and api roll, then the worker starts last, after the services it
depends on. The worker is down from the start of its drain until phase 3.
Queued work waits in the broker and the new worker picks it up.

**A failed deploy leaves no trace.** Whatever fails, every app service goes
back to exactly what it ran before the deploy, with no manual cleanup:

- The failing rolling service keeps its old container; the new one is removed.
- Rolling services that already shipped earlier in the run are rolled back, in
  reverse `deploy.order`, with the same rolling deploy onto their previous image.
- Every drain-first service comes back on its previous image at the same
  replica count, in `deploy.order`. That includes one that wouldn't stop, one
  that failed its own start, and one already healthy on the new tag: phase 3
  attempts every start, and a failure anywhere sends them all back. A service
  that wasn't running before the deploy is left stopped.
- The repo goes back to the previous commit, and the lock is released.

A rollback runs old code against whatever the new code already did (a
migration, say), so this relies on the usual expand-then-contract discipline:
old code keeps working against the new schema.

What "previous" means is read from the containers, not from git or `:latest`.
Before changing anything, the deploy records the image each app service is
running (the most common one if its containers disagree, with a warning) and
gives it a local alias, `<repo>:flow-deploy-<image id>`. A revert runs
`compose up` with that alias in `DEPLOY_TAG`, so it restores the exact image
even if the service was started from `:latest`, from a tag that has since been
re-pushed, from a digest, or from no tag at all. See [Image Tags](#image-tags).
If an image can't be aliased, or containers of a differently named compose
project run in the same directory (a release that renamed the project would
start a second stack beside the first), the deploy stops before changing
anything.

A signal reverts the same way, and so does an unexpected error. The signals are
SIGTERM (a timeout or shutdown), Ctrl-C (SIGINT), and SIGHUP (a hung-up
terminal). Further signals are ignored until the revert finishes.

A signal that arrives while a drain-first container is stopping is held until
that container has stopped and been removed, and the deploy then reverts. The
docker CLI being interrupted doesn't interrupt the daemon's stop, so reverting
immediately would find the container still "running" and leave it be, only to
see it exit afterwards. Holding the signal costs at most the rest of that
container's drain.

**A cancelled CI job does not revert a remote deploy.** `deploy-action` runs
`ssh host "cd dir && flow-deploy deploy --tag X"` with no pty. Without a pty,
sshd sends the remote process no signal when the client disconnects.
`tests/integration/test_ci_cancel.py` checks this against a real sshd. The
deploy runs to completion, or reverts on its own failure, exactly as if the job
were still watching. Its output to the closed session is discarded rather than
crashing it. Check the host afterwards (`flow-deploy status`) to see how it
ended.

The deploy exits `1` when it failed and everything was put back. It exits `3`
only when something couldn't be put back:

- the git restore failed
- a rollback or restore failed
- a *failed* service's own cleanup failed, such as an aborted new container
  that won't stop or be removed

Even then the lock is released. Exit `3` is the signal that someone needs to
look.

Rolling deploys otherwise behave as before. `tests/test_rolling_parity.py`
replays 15 rolling scenarios against a recording of the previous release: every
command, its environment, the exit code, and the lock. As before, a successful
cutover whose old-container cleanup fails still succeeds, with a warning: the
new container is healthy and serving.

The deliberate changes for rolling-only projects, each pinned by that test:

- A later service's failure rolls back services that already shipped.
  Previously they stayed on the new tag beside services on the old one.
- The deploy snapshots and aliases each service's running image first.
- `git fetch --tags --force origin`, so release tags arrive and a re-pushed
  tag moves.
- `compose config` is rendered with the deploy's `DEPLOY_TAG`.
- Scale-back after a failed health check passes `--no-recreate`. Previously it
  recreated the surviving old container on the new, failing image.
- Scale-back is skipped when the container it should leave behind couldn't be
  stopped and removed: with two left it removes one, and could pick the wrong
  one. The cutover still succeeds with a warning, as before.
- A compose project name that differs from the running containers' is refused.
- A failed git restore exits `3` and releases the lock.
- A signal restores the repo before exiting `1`. Previously it released the
  lock and exited without restoring.

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

## Image Tags

`DEPLOY_TAG` is normally the release version (`v1.4.0`), and a commit SHA works
just as well. The tag is used twice: `git checkout --detach <tag>` (lightweight
and annotated tags and SHAs all work; tags are fetched with `--force`, so a
re-pushed tag moves), and as the image tag compose pulls and runs. CI pushes the
image under every tag it might deploy by, typically the version and the SHA. The
compose file must name each app image with it:

```yaml
image: ghcr.io/myorg/myapp:${DEPLOY_TAG:-latest}
# or, through .env:  DOCKER_IMAGE=ghcr.io/myorg/myapp:${DEPLOY_TAG:-latest}
#                    image: ${DOCKER_IMAGE}
```

A deploy warns when an app service's rendered image doesn't end in `:<tag>`.
Such a service keeps running whatever the compose file names.

### Manual compose commands on the host

The `:-latest` default is a trap: a person running `docker compose up -d` on the
host, with `DEPLOY_TAG` unset, silently moves every service to `:latest`. So
flow-deploy can tell you what's deployed, and run compose with it:

```sh
flow-deploy tag                    # prints the deployed tag, e.g. v1.4.0
flow-deploy compose up -d          # compose with DEPLOY_TAG set to it
flow-deploy compose run --rm web python manage.py migrate
DEPLOY_TAG=v1.3.0 flow-deploy compose up -d   # an explicit DEPLOY_TAG wins
```

The deployed tag is read from the running app containers when they all agree,
else from `.git/deploy-tag` (written by every successful whole-project deploy)
when nothing is running. Mixed or untagged images, or nothing to go on, are an
error ("set DEPLOY_TAG"), never a guess. `flow-deploy status` shows it too.
After a revert, services recreated from the snapshot run the
`flow-deploy-<image id>` alias of their previous image, and the record holds it;
a service that kept its old container still shows its old tag. Either way the
image is the one that ran before, and when every service runs that one image
the deployed tag is the alias. An alias is a local tag: `flow-deploy compose
up` works with it, `pull` won't. The next deploy replaces it.

If the project has a compose wrapper (`script/prod`), make the wrapper default
`DEPLOY_TAG` the same way, so `script/prod up -d` is safe too:

```sh
if [ -z "${DEPLOY_TAG:-}" ] && command -v flow-deploy >/dev/null; then
  DEPLOY_TAG=$(flow-deploy tag 2>/dev/null) && export DEPLOY_TAG
fi
```

flow-deploy calls the wrapper itself; while it looks up the tag it sets
`DEPLOY_TAG` to a placeholder, so the wrapper never recurses. A `--service`
deploy that leaves some app services out leaves them on different tags, so it
clears the record and warns.

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
flow-deploy tag
flow-deploy compose ARGS...
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
