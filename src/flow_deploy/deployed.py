"""Which image tag is deployed on this host.

A compose file that names its images ``repo:${DEPLOY_TAG:-latest}`` resolves to
``:latest`` whenever DEPLOY_TAG isn't set, so a manual ``compose up`` on the
host would quietly recreate services onto ``:latest``. ``current()`` answers
"what's deployed" so manual commands can use it: ``flow-deploy tag`` prints it
and ``flow-deploy compose ...`` runs compose with it.

The answer comes from the running app containers when they all agree, then
from the record of the last deploy (``.git/deploy-tag``, written by
flow-deploy) when nothing is running. Mixed or untagged images are an
explicit "don't know", never a guess.
"""

import json
import os
import time

from flow_deploy import compose, config, containers

RECORD = ".git/deploy-tag"
# Rendered into DEPLOY_TAG while looking up the tag, so a compose wrapper that
# defaults DEPLOY_TAG from `flow-deploy tag` doesn't call back into us forever.
LOOKUP_PLACEHOLDER = "flow-deploy-lookup"


def write(tag: str, sha: str = "") -> None:
    with open(RECORD, "w") as f:
        json.dump({"tag": tag, "sha": sha, "timestamp": time.time()}, f)


def clear() -> None:
    if os.path.exists(RECORD):
        os.remove(RECORD)


def read() -> dict | None:
    try:
        with open(RECORD) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("tag") else None


def current(compose_cmd: list[str] | None = None) -> tuple[str | None, str]:
    """(tag, where it came from), or (None, why it can't be told).

    Raises RuntimeError if compose config or docker ps fails: "couldn't
    look" must not fall through to the record as if nothing were running.
    """
    compose_dict = compose.compose_config(cmd=compose_cmd, env={"DEPLOY_TAG": LOOKUP_PLACEHOLDER})
    project = compose_dict.get("name", "")
    apps = [s for s in config.parse_services(compose_dict) if s.is_app]
    tags = {}
    ids = []
    for svc in apps:
        ctrs = containers.list_containers_for_service(
            svc.name, project=project, statuses=("running", "restarting")
        )
        if ctrs is None:
            raise RuntimeError(f"could not list {svc.name} containers (docker ps failed)")
        for ctr in ctrs:
            tags.setdefault(containers.image_tag(ctr.get("Image", "")), []).append(svc.name)
            ids.append(ctr["ID"])
    if len(tags) == 1 and None not in tags:
        return next(iter(tags)), "running containers"
    if len(tags) > 1 and None not in tags and len(tags.keys() - {"latest"}) == 1:
        # After a failed deploy from :latest, recreated services run the
        # previous commit's SHA while the failed service's kept container still
        # says :latest. If they run one image, the SHA names it. Refusing here
        # would make a fail-closed wrapper refuse every compose call until the
        # next deploy.
        images = containers.image_ids(ids)
        if images is None:
            raise RuntimeError("could not inspect the app containers (docker inspect failed)")
        if len(set(images.values())) == 1:
            return (tags.keys() - {"latest"}).pop(), "running containers (one image)"
    if tags:
        seen = ", ".join(
            f"{t or 'untagged'} ({', '.join(sorted(set(s)))})" for t, s in tags.items()
        )
        return None, f"app services run different or untagged images: {seen}; set DEPLOY_TAG"
    record = read()
    if record:
        return record["tag"], "last deploy record"
    return None, "nothing is running and no deploy is recorded; set DEPLOY_TAG"
