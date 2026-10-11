"""Rolling deploys behave exactly as on master (0.3.0).

fixtures/rolling_master.json was recorded by running tests/rolling_scenarios.py
against master's flow_deploy: every docker/compose/git command, its env, the
exit code, and whether the lock was left behind. A project that doesn't use
drain-first must see the same commands in the same order. Agreed departures
are listed in DELIBERATE_CHANGES; signals are covered separately in
test_deploy_failures.py (master exited without restoring git; this branch
restores it).
"""

import json
from pathlib import Path

import pytest

from flow_deploy import deploy as deploy_mod
from flow_deploy import process
from tests.rolling_scenarios import SCENARIOS, record

MASTER = json.loads((Path(__file__).parent / "fixtures" / "rolling_master.json").read_text())

# Agreed departures from master, by scenario.
DELIBERATE_CHANGES = {
    # A failed git restore exits 3 ("needs a person") instead of 1, and the
    # lock is released: master's retained lock was broken by the next deploy
    # anyway, since acquire() clears locks whose PID has exited.
    "restore_fail": {"code": 3, "lock_left": False},
}


def deliberate_command_changes(calls):
    """master's commands, with the agreed command-level fix applied.

    Scale-back gains --no-recreate. On master, scale-back after a failed
    health check ran `compose up --scale svc=1` with the new tag, which
    recreated the surviving old container on the new, failing image. Found
    by tests/integration (test_rolling_service_fails_its_healthcheck).
    """
    fixed = []
    for args, env in calls:
        if args[-6:-2] == ["up", "-d", "--no-deps", "--scale"] and args[-2].endswith("=1"):
            args = args[:-3] + ["--no-recreate"] + args[-3:]  # after --no-deps
        fixed.append([args, env])
    return fixed


def test_scenarios_and_fixture_agree():
    assert set(SCENARIOS) == set(MASTER)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_rolling_deploy_matches_master(name, monkeypatch, tmp_path, capsys):
    # record() swaps process.run itself; restore it after the test.
    monkeypatch.setattr(process, "run", process.run)
    monkeypatch.setattr(process, "run_streaming", process.run_streaming)
    monkeypatch.setattr(deploy_mod.time, "sleep", deploy_mod.time.sleep)
    monkeypatch.chdir(tmp_path)

    observed = record(deploy_mod, process, name, str(tmp_path))

    expected_calls = deliberate_command_changes(MASTER[name]["calls"])
    assert observed["calls"] == expected_calls, f"{name}: commands differ from master"
    expected = {"code": MASTER[name]["code"], "lock_left": MASTER[name]["lock_left"]}
    expected.update(DELIBERATE_CHANGES.get(name, {}))
    assert observed["code"] == expected["code"]
    assert observed["lock_left"] == expected["lock_left"]
