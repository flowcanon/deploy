# GitHub Actions Setup

Flow Deploy includes a composite GitHub Action that handles host discovery, GHCR authentication, and SSH-based deployment. This guide walks through setting up automated deploys from CI.

## Overview

The deploy pipeline:

1. CI builds your Docker image and pushes it to GHCR under the release version (when there is one) and the commit SHA
2. The deploy action discovers hosts from your `docker-compose.yml`
3. For each host: authenticates with GHCR and runs `flow-deploy deploy --tag <version or sha>` (which handles git fetch and checkout internally)

## The Image Tag Contract

The deploy tag is both the git ref checked out on the server and the image tag pulled, so:

- **Push the image under every tag you deploy by, and always under the full commit SHA.** Typically the version tag (`v1.4.0`) for releases plus `${{ github.sha }}`. A failed deploy restores by the tag that was running when it's a git ref, else by the SHA of the commit that was checked out. `:latest` is optional; flow-deploy never relies on it.
- **Name every app image with `DEPLOY_TAG`**, directly or through `.env`:

```yaml
# docker-compose.yml
services:
  web:
    image: ${DOCKER_IMAGE}
```

```sh
# .env on the server
DOCKER_IMAGE=ghcr.io/yourorg/yourproject:${DEPLOY_TAG:-latest}
```

A bare `DOCKER_IMAGE=ghcr.io/yourorg/yourproject` (no tag) always runs `:latest`, whatever you deploy; flow-deploy warns when it sees that.

- **Use `flow-deploy compose` for manual commands on the server** (`flow-deploy compose up -d`, `flow-deploy compose run --rm web ...`). With `${DEPLOY_TAG:-latest}`, a plain `docker compose up -d` would move every service to `:latest`; `flow-deploy compose` sets `DEPLOY_TAG` to the deployed tag. Or have `script/prod` default it (see the README's Image Tags section).

Version tags can be lightweight or annotated, and a re-pushed tag is picked up: flow-deploy runs `git fetch --tags --force`. If a deploy fails, every service goes back to the image it was running, named by its release tag or commit SHA.

## Prerequisites

On your deploy server:

- Docker and Docker Compose
- Traefik (or your reverse proxy) running
- Git (`flow-deploy` handles `git fetch` and detached checkout internally during each deploy)
- `flow-deploy` installed:

```sh
curl -fsSL https://deploy.flowcanon.com/install | sh
```

## Bootstrap (First Deploy)

Flow Deploy takes over the update cycle — you still do the initial setup manually.

**1. Clone your project on the server:**

```sh
git clone git@github.com:yourorg/yourproject.git /srv/yourproject
cd /srv/yourproject
```

**2. Create your `.env` file:**

```sh
cp .env.example .env
# Edit with your values
```

**3. Install flow-deploy:**

```sh
curl -fsSL https://deploy.flowcanon.com/install | sh
```

**4. Start the stack** at the version you're deploying (with `DEPLOY_TAG` unset, `${DEPLOY_TAG:-latest}` runs `:latest`):

```sh
git checkout --detach v1.4.0
DEPLOY_TAG=v1.4.0 docker compose up -d
```

Or do the first deploy with flow-deploy itself: `flow-deploy deploy --tag v1.4.0`.

From this point, CI handles all subsequent deploys.

## GitHub Configuration

### SSH Key

Generate a deploy key:

```sh
ssh-keygen -t ed25519 -C "deploy@yourproject" -f deploy_key -N ""
```

- Add `deploy_key.pub` to the server's `~/.ssh/authorized_keys` for your deploy user
- Add `deploy_key` (the private key) as a GitHub secret: **`DEPLOY_SSH_KEY`**

### Environment Variables

If you want to keep host details out of your compose file (recommended for public repos), create a GitHub environment and set these variables:

| Variable | Description | Example |
|---|---|---|
| `HOST_NAME` | Server hostname or IP | `app-1.example.com` |
| `HOST_USER` | SSH user | `deploy` |
| `SSH_PORT` | SSH port (omit for 22) | `2222` |

These override `x-deploy` values from your compose file. Set them in:
**Settings > Environments > [your environment] > Environment variables**

Your workflow needs `environment: your-environment-name` on the deploy job to access these.

### Secrets

| Secret | Description |
|---|---|
| `DEPLOY_SSH_KEY` | Private SSH key for the deploy user |
| `GITHUB_TOKEN` | Provided automatically — used for GHCR auth on the server |

## The Deploy Action

Use the standalone action from [`flowcanon/deploy-action`](https://github.com/flowcanon/deploy-action).

### Inputs

| Input | Required | Default | Description |
|---|---|---|---|
| `tag` | Yes | — | Tag to deploy: the version tag, or the commit SHA |
| `ssh-key` | Yes | — | SSH private key |
| `command` | No | `script/prod` | Compose command |
| `host` | No | — | Override deploy host |
| `user` | No | — | Override deploy user |
| `ssh-port` | No | — | Override SSH port |
| `registry-token` | No | — | GHCR token for server auth |

### Example Workflow

Pushes to `master` deploy by commit SHA; pushing a version tag deploys that version.

```yaml
name: Deploy

on:
  push:
    branches: [master]
    tags: ["v*"]

env:
  IMAGE: ghcr.io/yourorg/yourproject

jobs:
  deploy:
    runs-on: ubuntu-latest
    environment: production
    permissions:
      contents: read
      packages: write

    steps:
      - uses: actions/checkout@v4

      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      # The version tag on tag pushes, and always the full SHA.
      - id: meta
        uses: docker/metadata-action@v5
        with:
          images: ${{ env.IMAGE }}
          tags: |
            type=ref,event=tag
            type=sha,format=long,prefix=

      - uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: ${{ steps.meta.outputs.tags }}

      - uses: flowcanon/deploy-action@master
        with:
          tag: ${{ github.ref_type == 'tag' && github.ref_name || github.sha }}
          ssh-key: ${{ secrets.DEPLOY_SSH_KEY }}
          host: ${{ vars.HOST_NAME }}
          user: ${{ vars.HOST_USER }}
          ssh-port: ${{ vars.SSH_PORT }}
          registry-token: ${{ secrets.GITHUB_TOKEN }}
```

### Release workflows that create the tag

When a release job creates the version tag itself (for example `salsify/action-detect-and-tag-new-version`, whose `tag` output is empty when no new version was detected), push the image under that tag and the SHA, and deploy by the tag with the SHA as the fallback:

```yaml
      - uses: docker/build-push-action@v6
        with:
          push: true
          tags: |
            ${{ env.IMAGE }}:${{ github.sha }}
            ${{ needs.release.outputs.created-tag && format('{0}:{1}', env.IMAGE, needs.release.outputs.created-tag) || '' }}

      - uses: flowcanon/deploy-action@master
        with:
          tag: ${{ needs.release.outputs.created-tag || github.sha }}
          ssh-key: ${{ secrets.DEPLOY_SSH_KEY }}
```

The tag must be pushed to the repository before the deploy runs, since flow-deploy fetches it on the server.

## What the Action Does

For each host group discovered from your compose config:

1. **SSH agent** — loads your deploy key
2. **Discover hosts** — parses `docker-compose.yml` for `x-deploy` and `deploy.*` labels, groups services by `(host, user, port, dir)`
3. **GHCR login** — authenticates Docker on the server (and logs out after)
4. **Deploy** — runs `flow-deploy deploy --tag <tag>` on the server (git fetch and detached checkout are handled by the tool)

## Host Discovery

The action discovers where to deploy from your compose file:

```yaml
x-deploy:
  host: app-1.example.com
  user: deploy
  port: 22
  dir: /srv/myapp
```

**Priority order** (highest wins):

1. GitHub Actions variables (`vars.HOST_NAME`, `vars.HOST_USER`, `vars.SSH_PORT`)
2. Per-service labels (`deploy.host`, `deploy.user`, `deploy.port`, `deploy.dir`)
3. `x-deploy` top-level defaults

This means you can keep `x-deploy` in your compose file for local/development use and override with GitHub variables for production — keeping real hostnames out of version control.

## Multi-Host Deploys

For services spread across multiple hosts:

```yaml
x-deploy:
  user: deploy
  dir: /srv/myapp

services:
  web:
    labels:
      deploy.role: app
      deploy.host: web-1.example.com

  worker:
    labels:
      deploy.role: app
      deploy.host: worker-1.example.com
      deploy.dir: /srv/worker
```

The action groups services by `(host, user, port, dir)` and deploys to each group sequentially.

## Versioned Releases

To cut releases with binaries and changelogs, see the release workflow in this repo which uses:

- `salsify/action-detect-and-tag-new-version` for version detection
- `softprops/action-gh-release` for GitHub releases with musl/glibc binaries
- `flowcanon/release-builder` for changelogs and version bumps

## Troubleshooting

**`working tree is dirty — deploy aborted`:**
The server repo has uncommitted changes. SSH into the server and resolve manually — `git status` will show what's dirty.

**`unauthorized` pulling from GHCR:**
Pass `registry-token: ${{ secrets.GITHUB_TOKEN }}` to the deploy action. The job needs `packages: write` (or at least `packages: read`) permission.

**`flow-deploy: command not found`:**
Install it on the server: `curl -fsSL https://deploy.flowcanon.com/install | sh`
Then start a new login shell or run `. ~/.profile`.

**`warning: web's image (...) doesn't use DEPLOY_TAG`:**
The rendered image doesn't end in `:<tag>`, so the deploy doesn't change what that service runs. Make the image (or `DOCKER_IMAGE` in `.env`) end in `:${DEPLOY_TAG:-latest}`.

**`manifest unknown` pulling `<image>:v1.4.0`:**
CI didn't push the image under that tag. Push the version tag as well as the SHA, or deploy by SHA.

**`can't tell which tag is deployed`** from `flow-deploy tag` or `flow-deploy compose`:
Services run different or untagged images (one image under `:latest` plus one other tag is fine), or nothing is running and no deploy is recorded. Set it explicitly: `DEPLOY_TAG=v1.4.0 flow-deploy compose up -d`.

**`WARNING: compose config failed with this environment ... (unset: DOCKER_IMAGE)`** from host discovery:
CI has no `.env`, so variables only set there are empty. Discovery retries without interpolation and carries on; the warning names the variables. It only matters if `x-deploy` or a `deploy.*` label uses them: set those in CI, or use the `HOST_NAME`/`HOST_USER`/`SSH_PORT` overrides.

**`didn't exit on SIGTERM ... and was killed`:**
The container's PID 1 isn't handling SIGTERM, so every deploy waits out the drain and kills it mid-request. Use an exec-form `CMD` (`["gunicorn", ...]`), or `exec` the server at the end of the entrypoint script.

**`accessory postgres's config changed since it started`:**
The next manual `up -d` will recreate it. Usually an `.env` edit: keep accessories off `env_file` and list their variables under `environment:` (see the README's "Accessories and `.env`").

**How did a deploy end, when CI was cancelled?**
On the host: `flow-deploy status` (last run and exit code), or `.git/flow-deploy.log`.

**Health check timeout:**
Increase the timeout via label: `deploy.healthcheck.timeout: 300`. Check that your app's health endpoint responds within the Docker healthcheck interval.
