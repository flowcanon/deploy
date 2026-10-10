"""Rolling-only deploy scenarios, shared by the parity test and its recorder.

Responses are plain (returncode, stdout, stderr) tuples so the recorder can run
them against another checkout's flow_deploy (master) without importing this
branch's code. To re-record fixtures/rolling_master.json from a master
checkout:

    git worktree add /tmp/deploy-master master
    poetry run python tests/rolling_scenarios.py /tmp/deploy-master \
        > tests/fixtures/rolling_master.json
"""

import json

CONFIG = """\
services:
  web:
    image: ghcr.io/myorg/myapp:latest
    labels:
      deploy.role: app
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
  worker:
    image: ghcr.io/myorg/myapp:latest
    labels:
      deploy.role: app
      deploy.order: "200"
    healthcheck:
      test: ["CMD", "celery", "inspect", "ping"]
  postgres:
    image: postgres:16
    labels:
      deploy.role: accessory
"""

SKIP_CONFIG = CONFIG.replace(
    '      deploy.order: "200"\n',
    '      deploy.order: "200"\n      deploy.healthcheck.skip: "true"\n',
)


def _ctr(cid, tag, created):
    return json.dumps({"ID": cid, "Image": f"ghcr.io/myorg/myapp:{tag}", "CreatedAt": created})


WEB = _ctr("old_web_111", "old", "2024-01-01") + "\n" + _ctr("new_web_222", "abc123", "2024-01-02")
WRK = _ctr("old_wrk_333", "old", "2024-01-01") + "\n" + _ctr("new_wrk_444", "abc123", "2024-01-02")
# Same tag on both: old/new told apart by CreatedAt.
WEB_SAME_TAG = (
    _ctr("old_web_111", "abc123", "2024-01-01") + "\n" + _ctr("new_web_222", "abc123", "2024-01-02")
)
# Same tag and same CreatedAt: can't tell them apart.
WEB_TWINS = _ctr("a1", "abc123", "2024-01-01") + "\n" + _ctr("b2", "abc123", "2024-01-01")


def ok(out=""):
    return (0, out, "")


def err(msg="error"):
    return (1, "", msg)


GIT = [ok(""), ok(), ok("prev123\n"), ok()]
GIT_NO_TAG = [ok("head456\n"), ok(""), ok(), ok("head456\n")]  # rev-parse, status, fetch, rev-parse


def roll(ps, health="healthy\n", stop=None):
    return [ok(), ok(), ok(ps), ok(health), stop or ok(), ok(), ok()]


def roll_skip(ps):
    return [ok(), ok(), ok(ps), ok(), ok(), ok()]


# name -> (responses, deploy kwargs)
SCENARIOS = {
    "happy": (GIT + [ok(CONFIG)] + roll(WEB) + roll(WRK), {}),
    "health_abort": (
        GIT + [ok(CONFIG), ok(), ok(), ok(WEB), ok("unhealthy\n"), ok(), ok(), ok(), ok()],
        {},
    ),
    "second_service_health_abort": (
        GIT
        + [ok(CONFIG)]
        + roll(WEB)
        + [ok(), ok(), ok(WRK), ok("unhealthy\n")]
        + [ok(), ok(), ok(), ok()],
        {},
    ),
    "pull_fail": (GIT + [ok(CONFIG), err("pull"), ok()], {}),
    "scale_up_fail": (GIT + [ok(CONFIG), ok(), err("port"), ok()], {}),
    "count_mismatch": (
        GIT + [ok(CONFIG), ok(), ok(), ok(WEB.split("\n")[0]), ok(), ok()],
        {},
    ),
    "unidentifiable": (GIT + [ok(CONFIG), ok(), ok(), ok(WEB_TWINS), ok(), ok()], {}),
    "same_tag_redeploy": (GIT + [ok(CONFIG)] + roll(WEB_SAME_TAG) + roll(WRK), {}),
    "healthcheck_skip": (GIT + [ok(SKIP_CONFIG)] + roll(WEB) + roll_skip(WRK), {}),
    "service_filter": (GIT + [ok(CONFIG)] + roll(WRK), {"services_filter": ["worker"]}),
    "no_tag_current_head": (GIT_NO_TAG + [ok(CONFIG)] + roll(WEB) + roll(WRK), {"tag": None}),
    "restore_fail": (GIT + [ok(CONFIG), err("pull"), err("checkout")], {}),
    "cutover_stop_fail": (
        GIT + [ok(CONFIG)] + roll(WEB, stop=err("No such container")) + roll(WRK),
        {},
    ),
    "dirty_tree": ([ok(" M file")], {}),
    "compose_config_fail": (GIT + [err("bad yaml"), ok()], {}),
}


def record(deploy_module, process_module, name, workdir):
    """Run one scenario against the given flow_deploy modules; return the observation."""
    import os

    responses, kwargs = SCENARIOS[name]
    queue = [process_module.Result(*r) for r in responses]
    calls = []

    def fake_run(args, env=None, cwd=None):
        calls.append([args, env])
        return queue.pop(0) if queue else process_module.Result(0, "", "")

    process_module.run = fake_run
    process_module.run_streaming = lambda *a, **k: 0
    deploy_module.time.sleep = lambda _s: None
    os.makedirs(os.path.join(workdir, ".git"), exist_ok=True)
    os.chdir(workdir)
    kwargs = {"tag": "abc123", **kwargs}
    code = deploy_module.deploy(cmd=["docker", "compose"], **kwargs)
    return {
        "code": code,
        "calls": calls,
        "lock_left": os.path.exists(".git/deploy-lock"),
    }


if __name__ == "__main__":
    import os
    import sys
    import tempfile

    sys.path.insert(0, os.path.join(sys.argv[1], "src"))
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
