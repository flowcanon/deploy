"""What a cancelled CI job does to a remote deploy.

deploy-action runs `ssh host "cd dir && flow-deploy deploy --tag X"` with no
pty. These tests run a stand-in for flow-deploy (images/sshd/probe.py, which
logs through flow_deploy.log and records the signals deploy() handles) over
real ssh, kill the client mid-run as a cancelled job does, and check what the
remote process received.

Finding: without a pty, sshd sends the remote process no signal when the
client disconnects. The deploy runs on (output to the closed session is
swallowed) and finishes, or reverts on its own failure. With `ssh -tt` the
remote process gets SIGHUP, which flow-deploy treats as an interrupt.
"""

import signal
import subprocess
import time
import uuid

import pytest

from tests.integration.conftest import IMAGES, SRC, run

PROBE_SECONDS = 4  # killed ~1s in; long enough to tell running on from dead


@pytest.fixture(scope="module")
def sshd(docker, tmp_path_factory):
    key = tmp_path_factory.mktemp("ssh") / "id"
    run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)])
    run(["docker", "build", "-q", "-t", "flowdeploy-it-sshd", str(IMAGES / "sshd")])
    name = f"flowdeploy-it-sshd-{uuid.uuid4().hex[:8]}"
    run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-p",
            "127.0.0.1::22",
            "-v",
            f"{SRC}:/src:ro",
            "flowdeploy-it-sshd",
        ]
    )
    try:
        port = run(["docker", "port", name, "22"]).stdout.strip().split(":")[-1]
        run(["docker", "cp", f"{key}.pub", f"{name}:/home/deploy/.ssh/authorized_keys"])
        run(
            [
                "docker",
                "exec",
                name,
                "sh",
                "-c",
                "chown deploy /home/deploy/.ssh/authorized_keys"
                " && chmod 600 /home/deploy/.ssh/authorized_keys",
            ]
        )
        yield {"name": name, "port": port, "key": str(key)}
    finally:
        run(["docker", "rm", "-f", name], check=False)


def probe_log(sshd):
    out = run(["docker", "exec", sshd["name"], "cat", "/tmp/probe.log"], check=False).stdout
    return out.strip().splitlines()


def cancel_mid_run(sshd, tty_flag, kill_with):
    """Start the probe over ssh, kill the client mid-run, return the probe's record."""
    run(["docker", "exec", sshd["name"], "rm", "-f", "/tmp/probe.log"])
    client = subprocess.Popen(
        [
            "ssh",
            tty_flag,
            "-i",
            sshd["key"],
            "-p",
            sshd["port"],
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "BatchMode=yes",
            "deploy@127.0.0.1",
            f"cd /tmp && python3 /probe.py {PROBE_SECONDS}",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 30
    while "started" not in probe_log(sshd):
        assert time.time() < deadline, "probe never started"
        time.sleep(0.1)
    time.sleep(1)
    client.send_signal(kill_with)
    client.wait(timeout=10)
    time.sleep(PROBE_SECONDS + 2)  # long enough for the probe to finish if it's alive
    return probe_log(sshd)


@pytest.mark.parametrize("kill_with", [signal.SIGKILL, signal.SIGTERM], ids=["SIGKILL", "SIGTERM"])
def test_without_a_pty_the_remote_deploy_gets_no_signal_and_runs_on(sshd, kill_with):
    record = cancel_mid_run(sshd, "-T", kill_with)
    assert record == ["started", "completed"], record


def test_with_a_forced_pty_the_remote_deploy_gets_sighup(sshd):
    record = cancel_mid_run(sshd, "-tt", signal.SIGKILL)
    assert "signal SIGHUP" in record, record
