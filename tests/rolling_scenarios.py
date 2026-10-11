"""Rolling-only deploy scenarios on a FakeHost, shared by the parity test and its recorder.

fixtures/rolling_master.json holds what master (0.3.0) did in each scenario:
every command, its env, the exit code, and whether the lock was left behind.
To re-record it from a master checkout:

    git worktree add /tmp/deploy-master master
    poetry run python tests/rolling_scenarios.py /tmp/deploy-master \
        > tests/fixtures/rolling_master.json

Scenarios declare what's running and what fails, not a call sequence, so the
same scenario runs unchanged against master's code and this branch's even
though this branch issues extra commands (the pre-deploy snapshot).
"""

import copy
import os
import sys

CONFIG = {
    "services": {
        "web": {
            "image": "ghcr.io/myorg/myapp:${DEPLOY_TAG:-latest}",
            "labels": {"deploy.role": "app"},
            "healthcheck": {"test": ["CMD", "true"]},
        },
        "worker": {
            "image": "ghcr.io/myorg/myapp:${DEPLOY_TAG:-latest}",
            "labels": {"deploy.role": "app", "deploy.order": "200"},
            "healthcheck": {"test": ["CMD", "true"]},
        },
        "postgres": {"image": "postgres:16", "labels": {"deploy.role": "accessory"}},
    }
}

V1 = {"web": ["v1"], "worker": ["v1"]}


def _skip_worker_health(config):
    labels = config["services"]["worker"]["labels"]
    labels["deploy.healthcheck.skip"] = "true"
    return config


def _twins(host):
    """Old and new containers on the deploy tag with the same CreatedAt: unidentifiable."""
    host.same_created = True


# name -> (running, setup(host) or None, deploy kwargs, config transform or None)
SCENARIOS = {
    "happy": (V1, None, {}, None),
    "health_abort": (V1, lambda h: h.unhealthy_services.add(("web", "v2")), {}, None),
    "second_service_health_abort": (
        V1,
        lambda h: h.unhealthy_services.add(("worker", "v2")),
        {},
        None,
    ),
    "pull_fail": (V1, lambda h: h.unpullable.add("v2"), {}, None),
    "scale_up_fail": (V1, lambda h: h.fail("--scale web=2 web", times=1), {}, None),
    "count_mismatch": (V1, lambda h: h.stuck.add("web"), {}, None),
    "unidentifiable": ({"web": ["v2"], "worker": ["v2"]}, _twins, {}, None),
    "same_tag_redeploy": ({"web": ["v2"], "worker": ["v2"]}, None, {}, None),
    "healthcheck_skip": (V1, None, {}, _skip_worker_health),
    "service_filter": (V1, None, {"services_filter": ["worker"]}, None),
    "no_tag_current_head": (V1, None, {"tag": None}, None),
    "restore_fail": (
        V1,
        lambda h: (h.unpullable.add("v2"), h.fail("git checkout --detach prev123abc")),
        {},
        None,
    ),
    "cutover_stop_fail": (V1, lambda h: h.fail("docker stop --time 30 web001"), {}, None),
    "dirty_tree": (V1, lambda h: setattr(h, "dirty", True), {}, None),
    "compose_config_fail": (V1, lambda h: h.fail("docker compose config"), {}, None),
}


def record(deploy_module, process_module, name, workdir):
    """Run one scenario against the given flow_deploy modules; return the observation."""
    from tests.fake_host import COMPOSE_CMD, FakeHost

    running, setup, kwargs, transform = SCENARIOS[name]
    config = copy.deepcopy(CONFIG)
    if transform:
        config = transform(config)
    host = FakeHost(config, running=running)
    if setup:
        setup(host)
    process_module.run = host.run
    process_module.run_streaming = lambda *a, **k: 0
    deploy_module.time.sleep = lambda _s: None
    os.makedirs(os.path.join(workdir, ".git"), exist_ok=True)
    os.chdir(workdir)
    code = deploy_module.deploy(cmd=COMPOSE_CMD, **{"tag": "v2", **kwargs})
    return {
        "code": code,
        "calls": [[args, env] for args, env in zip(host.calls, host.envs)],
        "lock_left": os.path.exists(".git/deploy-lock"),
    }


if __name__ == "__main__":
    import json
    import tempfile

    # The target checkout's flow_deploy must win over this one's.
    sys.path.insert(0, os.path.join(sys.argv[1], "src"))
    sys.path.insert(1, os.getcwd())
    from flow_deploy import deploy as deploy_mod
    from flow_deploy import process

    results = {}
    devnull = open(os.devnull, "w")
    for name in SCENARIOS:
        out, errs = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = devnull
        try:
            with tempfile.TemporaryDirectory() as d:
                results[name] = record(deploy_mod, process, name, d)
        finally:
            sys.stdout, sys.stderr = out, errs
    print(json.dumps(results, indent=1))
