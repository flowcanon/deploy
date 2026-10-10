"""Rolling deploys behave exactly as on master (0.3.0).

fixtures/rolling_master.json was recorded by running these scenarios against
master's flow_deploy: every docker/compose command, its env, the exit code, and
whether the lock was left behind. A project that doesn't use drain-first must
see the same commands in the same order. The one intentional difference is
noted in DELIBERATE_CHANGES.
"""

import json
from pathlib import Path

import pytest

from flow_deploy import process
from flow_deploy.deploy import deploy
from tests.test_deploy import _chdir

MASTER = json.loads((Path(__file__).parent / "fixtures" / "rolling_master.json").read_text())

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


def _ctr(cid, tag, created):
    return json.dumps({"ID": cid, "Image": f"ghcr.io/myorg/myapp:{tag}", "CreatedAt": created})


WEB = _ctr("old_web_111", "old", "2024-01-01") + "\n" + _ctr("new_web_222", "abc123", "2024-01-02")
WRK = _ctr("old_wrk_333", "old", "2024-01-01") + "\n" + _ctr("new_wrk_444", "abc123", "2024-01-02")


def ok(out=""):
    return process.Result(0, out, "")


def err(msg="error"):
    return process.Result(1, "", msg)


GIT = [ok(""), ok(), ok("prev123\n"), ok()]


def roll(ps, health="healthy\n", stop=None):
    return [ok(), ok(), ok(ps), ok(health), stop or ok(), ok(), ok()]


SCENARIOS = {
    "happy": GIT + [ok(CONFIG)] + roll(WEB) + roll(WRK),
    "health_abort": GIT
    + [ok(CONFIG), ok(), ok(), ok(WEB), ok("unhealthy\n"), ok(), ok(), ok(), ok()],
    "pull_fail": GIT + [ok(CONFIG), err("pull"), ok()],
    "scale_up_fail": GIT + [ok(CONFIG), ok(), err("port"), ok()],
    "count_mismatch": GIT + [ok(CONFIG), ok(), ok(), ok(WEB.split("\n")[0]), ok(), ok()],
    "restore_fail": GIT + [ok(CONFIG), err("pull"), err("checkout")],
    "cutover_stop_fail": GIT + [ok(CONFIG)] + roll(WEB, stop=err("No such container")) + roll(WRK),
    "dirty_tree": [ok(" M file")],
    "compose_config_fail": GIT + [err("bad yaml"), ok()],
}

# Agreed departures from master, by scenario: what changed and why.
DELIBERATE_CHANGES = {
    # A failed git restore exits 3 ("needs a person") instead of 1, and the
    # lock is released: master's retained lock was broken by the next deploy
    # anyway, since acquire() clears locks whose PID has exited.
    "restore_fail": {"code": 3, "lock_left": False},
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_rolling_deploy_matches_master(name, mock_process, monkeypatch, tmp_path):
    assert set(SCENARIOS) == set(MASTER), "scenarios and fixture out of sync"
    _chdir(monkeypatch, tmp_path)
    monkeypatch.setattr("flow_deploy.deploy.time.sleep", lambda _s: None)
    mock_process.responses.extend(SCENARIOS[name])

    code = deploy(tag="abc123", cmd=["docker", "compose"])

    calls = [[args, env] for _, args, env, _ in mock_process.calls]
    assert calls == MASTER[name]["calls"], f"{name}: docker/compose commands differ from master"
    expected = {"code": MASTER[name]["code"], "lock_left": MASTER[name]["lock_left"]}
    expected.update(DELIBERATE_CHANGES.get(name, {}))
    assert code == expected["code"]
    assert (tmp_path / ".git" / "deploy-lock").exists() == expected["lock_left"]
