"""A fake Docker host for unit tests.

Answers the docker, compose, and git commands flow-deploy runs from in-memory
state, so a test declares what's running and what should fail, runs a deploy,
and asserts on the outcome instead of on the exact sequence of calls:

    host = FakeHost(compose, running={"web": ["v1"], "worker": ["v1", "v1"]})
    host.unhealthy.add("v2")
    host.install(monkeypatch)
    assert deploy(tag="v2", cmd=COMPOSE_CMD) == 1
    assert host.running() == {"web": ["v1"], "worker": ["v1", "v1"]}

Failures are injected by substring: host.fail("docker stop old_") fails every
docker stop whose command line contains that text.
"""

import hashlib
import itertools
import json
from dataclasses import dataclass

import yaml

from flow_deploy import process

COMPOSE_CMD = ["docker", "compose"]
REPO = "ghcr.io/myorg/myapp"


def image_id_for(tag: str) -> str:
    return "sha256:" + hashlib.sha256(tag.encode()).hexdigest()


@dataclass
class Ctr:
    id: str
    service: str
    tag: str  # the DEPLOY_TAG it was created from
    image_id: str
    created: int
    state: str = "running"
    ref: str = ""  # what docker ps shows as Image; defaults to REPO:tag
    exit_code: int | None = None


def ok(out: str = "") -> process.Result:
    return process.Result(0, out, "")


def fail(msg: str = "injected failure") -> process.Result:
    return process.Result(1, "", msg)


class FakeHost:
    def __init__(self, compose: dict, running: dict | None = None, project: str = "proj"):
        self.compose = compose
        self.project = project
        self.sha = "prev123abc"
        self.dirty = False
        self.ctrs: list[Ctr] = []
        self.images: dict[str, str] = {}  # local tags -> image id
        # What the registry holds under tags not pulled yet (e.g. a commit SHA
        # CI pushed); anything else pulls as image_id_for(tag).
        self.registry: dict[str, str] = {}
        self.not_refs: set[str] = {"latest"}  # image tags that aren't git refs
        self.unpullable: set[str] = set()  # tags compose pull can't fetch
        self.unhealthy: set[str] = set()  # tags whose containers never become healthy
        self.unhealthy_services: set[tuple[str, str]] = set()  # (service, tag) pairs
        self.stuck: set[str] = set()  # services whose compose up succeeds but changes nothing
        self.same_created = False  # docker ps shows one CreatedAt for every container
        self.ignores_sigterm: set[str] = set()  # services docker stop has to SIGKILL
        # Like a fail-closed script/prod: a compose call without DEPLOY_TAG fails.
        self.wrapper_requires_tag = True
        self.streamed: list[tuple[list[str], dict | None]] = []  # run_streaming calls
        self.other_projects: list[str] = []  # other projects running these services here
        self.foreign_services: list[tuple[str, str]] = []  # (project, service) running here
        self.failures: list[list] = []  # [substring, skip, remaining or None]
        self.hooks: list[tuple[str, object]] = []  # (substring, callback) run before a command
        self.calls: list[list[str]] = []
        self.envs: list[dict | None] = []
        self.new_sessions: list[list[str]] = []
        self._ids = itertools.count(1)
        self._clock = itertools.count(1)
        for service, tags in (running or {}).items():
            for tag in tags:
                self.add(service, tag)

    # -- setup -------------------------------------------------------------

    def add(self, service: str, tag: str, ref: str | None = None, state: str = "running") -> Ctr:
        image_id = self.images.setdefault(tag, image_id_for(tag))
        ctr = Ctr(
            id=f"{service}{next(self._ids):03d}",
            service=service,
            tag=tag,
            image_id=image_id,
            created=next(self._clock),
            state=state,
            ref=ref or f"{REPO}:{tag}",
        )
        self.ctrs.append(ctr)
        return ctr

    def fail(self, substring: str, times: int | None = None, skip: int = 0) -> None:
        """Fail commands containing ``substring``: after ``skip`` matches, ``times`` times."""
        self.failures.append([substring, skip, times])

    def before(self, substring: str, callback) -> None:
        """Run ``callback()`` just before the first command containing ``substring``."""
        self.hooks.append((substring, callback))

    def install(self, monkeypatch) -> "FakeHost":
        monkeypatch.setattr(process, "run", self.run)
        monkeypatch.setattr(process, "run_streaming", self.run_streaming)
        monkeypatch.setattr("flow_deploy.deploy.time.sleep", lambda _s: None)
        return self

    # -- observation -------------------------------------------------------

    def running(self) -> dict[str, list[str]]:
        """service -> sorted tags of its running containers."""
        out: dict[str, list[str]] = {}
        for ctr in self.ctrs:
            if ctr.state == "running":
                out.setdefault(ctr.service, []).append(ctr.tag)
        return {s: sorted(t) for s, t in out.items()}

    def running_images(self) -> dict[str, list[str]]:
        """service -> sorted image IDs of its running containers."""
        out: dict[str, list[str]] = {}
        for ctr in self.ctrs:
            if ctr.state == "running":
                out.setdefault(ctr.service, []).append(ctr.image_id)
        return {s: sorted(i) for s, i in out.items()}

    def commands(self) -> list[str]:
        return [" ".join(args) for args in self.calls]

    def index(self, *needles: str) -> int:
        """Position of the first command containing every needle."""
        cmds = self.commands()
        return next(i for i, c in enumerate(cmds) if all(n in c for n in needles))

    # -- the fake ----------------------------------------------------------

    def _untagged(self, args, env) -> bool:
        """A compose call a fail-closed wrapper would refuse: no DEPLOY_TAG."""
        is_compose = args[: len(COMPOSE_CMD)] == COMPOSE_CMD
        return self.wrapper_requires_tag and is_compose and not (env or {}).get("DEPLOY_TAG")

    def run_streaming(self, args, env=None, cwd=None):
        self.streamed.append((list(args), env))
        return 1 if self._untagged(args, env) else 0

    def run(self, args, env=None, cwd=None, new_session=False):
        self.calls.append(list(args))
        self.envs.append(env)
        if new_session:
            self.new_sessions.append(list(args))
        line = " ".join(args)
        for i, (substring, callback) in enumerate(self.hooks):
            if substring in line:
                self.hooks.pop(i)
                callback()
                break
        for rule in self.failures:
            substring, skip, times = rule
            if substring in line and times != 0:
                if skip:
                    rule[1] -= 1
                    continue
                if times is not None:
                    rule[2] -= 1
                return fail()
        if self._untagged(args, env):
            return fail("script/prod: set DEPLOY_TAG (flow-deploy tag couldn't tell)")
        tag = (env or {}).get("DEPLOY_TAG", "latest")
        if args[:1] == ["git"]:
            return self._git(args[1:])
        if args[: len(COMPOSE_CMD)] == COMPOSE_CMD:
            return self._compose(args[len(COMPOSE_CMD) :], tag)
        if args[:2] == ["docker", "ps"]:
            return self._ps(args[2:])
        if args[:2] == ["docker", "inspect"]:
            return self._inspect(args[2:])
        if args[:2] == ["docker", "stop"]:
            return self._stop(args[-1])
        if args[:2] == ["docker", "rm"]:
            return self._rm(args[-1])
        if args[:3] == ["docker", "image", "inspect"]:
            image = self.images.get(args[-1].rsplit(":", 1)[-1])
            return ok(image + "\n") if image else fail("No such image")
        raise AssertionError(f"FakeHost: unexpected command {line!r}")

    def _git(self, args):
        if args[:1] == ["status"]:
            return ok(" M file\n" if self.dirty else "")
        if args[:1] == ["fetch"]:
            return ok()
        if args[:2] == ["rev-parse", "--verify"]:
            ref = args[-1].removesuffix("^{commit}")
            return fail() if ref in self.not_refs or ref.startswith("sha256") else ok(ref)
        if args[:1] == ["rev-parse"]:
            return ok(self.sha + "\n")
        if args[:2] == ["checkout", "--detach"]:
            self.sha = args[2]
            return ok()
        raise AssertionError(f"FakeHost: unexpected git {args}")

    def _compose(self, args, tag):
        if args[:1] == ["config"]:
            return ok(yaml.safe_dump(self._rendered(tag)))
        if args[:1] == ["pull"]:
            if tag in self.unpullable:
                return fail(f"manifest for {REPO}:{tag} not found")
            # A pull fetches what the registry has now, moving a re-pushed tag.
            self.images[tag] = self.registry.get(tag) or self.images.get(tag) or image_id_for(tag)
            return ok()
        if args[:1] == ["up"]:
            return self._up(args, tag)
        raise AssertionError(f"FakeHost: unexpected compose {args}")

    def _rendered(self, tag):
        services = {}
        for name, svc in self.compose["services"].items():
            svc = dict(svc)
            image = svc.get("image", f"{REPO}:${{DEPLOY_TAG:-latest}}")
            svc["image"] = image.replace("${DEPLOY_TAG:-latest}", tag).replace("${DEPLOY_TAG}", tag)
            services[name] = svc
        return {"name": self.project, "services": services}

    def _live(self, service):
        return [
            c for c in self.ctrs if c.service == service and c.state in ("running", "restarting")
        ]

    def _up(self, args, tag):
        service = args[-1]
        replicas = 1
        if "--scale" in args:
            replicas = int(args[args.index("--scale") + 1].split("=")[1])
        if tag not in self.images:  # compose pulls a missing image
            if tag in self.unpullable:
                return fail(f"No such image: {REPO}:{tag}")
            self.images[tag] = self.registry.get(tag, image_id_for(tag))
        if service in self.stuck:
            return ok()
        live = self._live(service)
        if "--no-recreate" not in args:
            for ctr in [c for c in live if c.tag != tag]:
                self.ctrs.remove(ctr)
                live.remove(ctr)
                live.append(self.add(service, tag))
        while len(live) > replicas:  # compose removes the newest extras
            newest = max(live, key=lambda c: c.created)
            self.ctrs.remove(newest)
            live.remove(newest)
        # Like compose, start stopped containers before creating new ones.
        stopped = [c for c in self.ctrs if c.service == service and c.state == "exited"]
        while len(live) < replicas and stopped:
            ctr = stopped.pop(0)
            ctr.state = "running"
            live.append(ctr)
        while len(live) < replicas:
            live.append(self.add(service, tag))
        return ok()

    def _ps(self, args):
        if any(a.startswith("label=com.docker.compose.project.working_dir=") for a in args):
            # Compose project names of everything running in this directory.
            rows = [(self.project, c.service) for c in self.ctrs if c.state == "running"]
            rows += [(p, c.service) for p in self.other_projects for c in self.ctrs]
            rows += self.foreign_services
            return ok("".join(f"{p} {s}\n" for p, s in rows))
        service = None
        statuses = []
        for flag, value in zip(args, args[1:]):
            if flag == "--filter" and value.startswith("label=com.docker.compose.service="):
                service = value.split("=", 2)[2]
            if flag == "--filter" and value.startswith("status="):
                statuses.append(value.split("=", 1)[1])
        statuses = statuses or ["running"]
        lines = [
            json.dumps(
                {
                    "ID": c.id,
                    "Image": c.ref,
                    "CreatedAt": f"2024-01-01 00:00:{1 if self.same_created else c.created:02d}",
                    "State": c.state,
                    "Labels": f"com.docker.compose.service={c.service}",
                }
            )
            for c in self.ctrs
            if c.service == service and c.state in statuses
        ]
        return ok("".join(line + "\n" for line in lines))

    def _inspect(self, args):
        fmt = args[args.index("--format") + 1]
        ids = args[args.index("--format") + 2 :]
        by_id = {c.id: c for c in self.ctrs}
        if fmt == "{{.State.ExitCode}}":
            ctr = by_id.get(ids[0])
            return ok(f"{ctr.exit_code}\n") if ctr else fail("No such container")
        if fmt == "{{.State.Health.Status}}":
            ctr = by_id.get(ids[0])
            if ctr is None:
                return fail("No such container")
            sick = ctr.tag in self.unhealthy or (ctr.service, ctr.tag) in self.unhealthy_services
            return ok("unhealthy\n" if sick else "healthy\n")
        if any(i not in by_id for i in ids):
            return fail("No such container")
        return ok("".join(f"{i}{'0' * 8} {by_id[i].image_id}\n" for i in ids))

    def _stop(self, ctr_id):
        for ctr in self.ctrs:
            if ctr.id == ctr_id:
                ctr.state = "exited"
                ctr.exit_code = 137 if ctr.service in self.ignores_sigterm else 0
                return ok()
        return fail("No such container")

    def _rm(self, ctr_id):
        for ctr in self.ctrs:
            if ctr.id == ctr_id:
                if ctr.state == "running":  # as docker rm without -f
                    return fail("cannot remove a running container")
                self.ctrs.remove(ctr)
                return ok()
        return fail("No such container")
