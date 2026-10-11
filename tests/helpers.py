"""Compose builders and setup shared by the FakeHost-based tests."""

import json

from tests.fake_host import REPO


def app(order=100, strategy=None, drain=None, healthcheck=True, skip=False, image=None):
    """An app service for FakeHost's compose config."""
    labels = {"deploy.role": "app", "deploy.order": str(order)}
    if strategy:
        labels["deploy.strategy"] = strategy
    if drain is not None:
        labels["deploy.drain"] = str(drain)
    if skip:
        labels["deploy.healthcheck.skip"] = "true"
    svc = {"image": image or f"{REPO}:${{DEPLOY_TAG:-latest}}", "labels": labels}
    if healthcheck:
        svc["healthcheck"] = {"test": ["CMD", "true"]}
    return svc


def stack(**services):
    """A compose config: ``stack(web=app(100), worker=app(300, "drain-first"))``."""
    return {"services": services}


# web rolls; scheduler and worker drain first.
DRAIN = stack(
    web=app(100),
    scheduler=app(200, "drain-first"),
    worker=app(300, "drain-first", drain=3660),
)
# Rolling only: web then worker, as on master.
ROLLING = stack(web=app(100), worker=app(200))


def chdir(monkeypatch, tmp_path):
    """Work in tmp_path, with a .git/ for the lock and the deploy-tag record."""
    (tmp_path / ".git").mkdir(exist_ok=True)
    monkeypatch.chdir(tmp_path)


def record(tmp_path):
    """The .git/deploy-tag record, or None."""
    path = tmp_path / ".git" / "deploy-tag"
    return json.loads(path.read_text()) if path.exists() else None
