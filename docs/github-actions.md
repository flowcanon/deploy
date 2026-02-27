# GitHub Actions Setup

Flow Deploy includes a composite GitHub Action that handles host discovery, GHCR authentication, and SSH-based deployment. This guide walks through setting up automated deploys from CI.

## Overview

The deploy pipeline:

1. CI builds your Docker image and pushes to GHCR
2. The deploy action discovers hosts from your `docker-compose.yml`
3. For each host: authenticates with GHCR, pulls the repo, and runs `flow-deploy deploy`

## Prerequisites

On your deploy server:

- Docker and Docker Compose
- Traefik (or your reverse proxy) running
- Git (the server repo is updated via `git pull --ff-only` before each deploy)
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

**3. Start the stack:**

```sh
docker compose up -d
```

**4. Install flow-deploy:**

```sh
curl -fsSL https://deploy.flowcanon.com/install | sh
```

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

Copy `.github/actions/deploy/` from this repo into your project, or reference it directly.

### Inputs

| Input | Required | Default | Description |
|---|---|---|---|
| `tag` | Yes | — | Image tag to deploy |
| `ssh-key` | Yes | — | SSH private key |
| `command` | No | `script/prod` | Compose command |
| `host` | No | — | Override deploy host |
| `user` | No | — | Override deploy user |
| `ssh-port` | No | — | Override SSH port |
| `registry-token` | No | — | GHCR token for server auth |

### Example Workflow

```yaml
name: Deploy

on:
  push:
    branches: [master]

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

      - id: meta
        uses: docker/metadata-action@v5
        with:
          images: ${{ env.IMAGE }}
          tags: type=sha,prefix=

      - uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: ${{ steps.meta.outputs.tags }}

      - uses: ./.github/actions/deploy
        with:
          tag: ${{ steps.meta.outputs.version }}
          ssh-key: ${{ secrets.DEPLOY_SSH_KEY }}
          host: ${{ vars.HOST_NAME }}
          user: ${{ vars.HOST_USER }}
          ssh-port: ${{ vars.SSH_PORT }}
          registry-token: ${{ secrets.GITHUB_TOKEN }}
```

## What the Action Does

For each host group discovered from your compose config:

1. **SSH agent** — loads your deploy key
2. **Discover hosts** — parses `docker-compose.yml` for `x-deploy` and `deploy.*` labels, groups services by `(host, user, dir)`
3. **GHCR login** — authenticates Docker on the server (and logs out after)
4. **Git pull** — fast-forward only, fails safely if the server has diverged
5. **Deploy** — runs `flow-deploy deploy --tag <tag>` on the server

## Host Discovery

The action discovers where to deploy from your compose file:

```yaml
x-deploy:
  host: app-1.example.com
  user: deploy
  dir: /srv/myapp
```

**Priority order** (highest wins):

1. GitHub Actions variables (`vars.HOST_NAME`, `vars.HOST_USER`)
2. Per-service labels (`deploy.host`, `deploy.user`, `deploy.dir`)
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

The action groups services by `(host, user, dir)` and deploys to each group sequentially.

## Versioned Releases

To cut releases with binaries and changelogs, see the release workflow in this repo which uses:

- `salsify/action-detect-and-tag-new-version` for version detection
- `softprops/action-gh-release` for GitHub releases with musl/glibc binaries
- `flowcanon/release-builder` for changelogs and version bumps

## Troubleshooting

**`git pull --ff-only` fails:**
The server repo has diverged from the remote. SSH into the server and resolve manually — check for local commits or uncommitted changes.

**`unauthorized` pulling from GHCR:**
Pass `registry-token: ${{ secrets.GITHUB_TOKEN }}` to the deploy action. The job needs `packages: write` (or at least `packages: read`) permission.

**`flow-deploy: command not found`:**
Install it on the server: `curl -fsSL https://deploy.flowcanon.com/install | sh`
Then start a new login shell or run `. ~/.profile`.

**Health check timeout:**
Increase the timeout via label: `deploy.healthcheck.timeout: 300`. Check that your app's health endpoint responds within the Docker healthcheck interval.
