"""Rolling deploys behave as on master (0.3.0), apart from listed, deliberate changes.

fixtures/rolling_master.json was recorded by running tests/rolling_scenarios.py
against master's flow_deploy on a FakeHost: every docker/compose/git command,
its env, the exit code, and whether the lock was left behind. A project that
doesn't use drain-first must see the same commands in the same order, once
the departures below are applied to master's record. Signals are covered
separately in test_deploy_failures.py (master exited without restoring git;
this branch restores it).
"""

import json
import re
from pathlib import Path

import pytest

from flow_deploy import deploy as deploy_mod
from flow_deploy import process
from tests.rolling_scenarios import SCENARIOS, record

MASTER = json.loads((Path(__file__).parent / "fixtures" / "rolling_master.json").read_text())

# Agreed departures from master's outcome, by scenario.
DELIBERATE_OUTCOMES = {
    # A failed git restore exits 3 ("needs a person") instead of 1, and the
    # lock is released: master's retained lock was broken by the next deploy
    # anyway, since acquire() clears locks whose PID has exited.
    "restore_fail": {"code": 3, "lock_left": False},
}


def is_snapshot(args):
    """The pre-deploy checks: what each app service runs and the git tag or
    SHA that restores it, and that no other compose project runs these
    services in this directory. Plus a revert's look for stopped leftovers
    that compose would otherwise restart.

    New on this branch, issued before anything changes, so a failure can
    restore what each service ran rather than what HEAD or :latest says.
    """
    line = " ".join(args)
    return (
        (args[:2] == ["docker", "ps"] and "status=restarting" in line)
        or (args[:2] == ["docker", "ps"] and "project.working_dir=" in line)
        or (args[:2] == ["docker", "ps"] and "status=created" in line)
        or args[:4] == ["docker", "inspect", "--format", "{{.Id}} {{.Image}}"]
        or args[:3] == ["docker", "image", "inspect"]
        or args[:3] == ["git", "rev-parse", "--verify"]
    )


def as_on_this_branch(calls, tag):
    """master's commands with the agreed command-level changes applied."""
    fixed = []
    for args, env in calls:
        if args == ["git", "fetch", "origin"]:
            # Version tags are fetched, and a re-pushed tag moves locally.
            args = ["git", "fetch", "--tags", "--force", "origin"]
        elif args[-1:] == ["config"]:
            # Rendered with the deploy tag, so image refs are the ones deployed.
            env = {"DEPLOY_TAG": tag}
        elif args[-6:-2] == ["up", "-d", "--no-deps", "--scale"] and args[-2].endswith("=1"):
            # Scale-back gains --no-recreate. On master, scale-back after a
            # failed health check recreated the surviving old container on the
            # new, failing image. Found by tests/integration.
            args = args[:-3] + ["--no-recreate"] + args[-3:]
        fixed.append([args, env])
    return fixed


def by_appearance(calls):
    """Container IDs renamed in order of first mention, per service.

    FakeHost numbers containers from one counter, so a container master
    created and this branch doesn't would shift every later ID.
    """
    names = {}
    out = []
    for args, env in calls:
        renamed = []
        for arg in args:
            m = re.fullmatch(r"(web|worker)\d{3}", arg)
            if m:
                seen = [v for k, v in names.items() if k.startswith(m.group(1))]
                arg = names.setdefault(arg, f"{m.group(1)}#{len(seen) + 1}")
            renamed.append(arg)
        out.append([renamed, env])
    return out


def test_scenarios_and_fixture_agree():
    assert set(SCENARIOS) == set(MASTER)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_rolling_deploy_matches_master(name, monkeypatch, tmp_path):
    # record() swaps process.run itself; restore it after the test.
    monkeypatch.setattr(process, "run", process.run)
    monkeypatch.setattr(process, "run_streaming", process.run_streaming)
    monkeypatch.setattr(deploy_mod.time, "sleep", deploy_mod.time.sleep)
    monkeypatch.chdir(tmp_path)

    observed = record(deploy_mod, process, name, str(tmp_path))

    calls = [[args, env] for args, env in observed["calls"] if not is_snapshot(args)]
    tag = SCENARIOS[name][2].get("tag", "v2") or "prev123abc"
    expected_calls = as_on_this_branch(MASTER[name]["calls"], tag)
    if name == "cutover_stop_fail":
        # Deliberate: no scale-back while the old container may still be
        # running. With two containers left, scale-back removes one, and
        # with --no-recreate compose might remove the new one.
        expected_calls = [c for c in expected_calls if c[0][-2:] != ["web=1", "web"]]
    if name == "second_service_health_abort":
        # Deliberate: a later service's failure rolls back services that had
        # already shipped. master left web on the new tag beside a worker on
        # the old one; this branch rolls web back to the image it ran.
        rollback = calls[len(expected_calls) :]
        calls = calls[: len(expected_calls)]
        composes = [env for args, env in rollback if args[:2] == ["docker", "compose"]]
        assert composes and all(env["DEPLOY_TAG"] == "v1" for env in composes)
        assert any(args[-1] == "web" and "--scale" in args for args, _ in rollback)
    assert by_appearance(calls) == by_appearance(expected_calls), f"{name}: commands differ"

    expected = {"code": MASTER[name]["code"], "lock_left": MASTER[name]["lock_left"]}
    expected.update(DELIBERATE_OUTCOMES.get(name, {}))
    assert observed["code"] == expected["code"]
    assert observed["lock_left"] == expected["lock_left"]
