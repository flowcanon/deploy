"""Stands in for a remote `flow-deploy deploy` to see what an ssh disconnect does.

Logs through flow_deploy.log (mounted at /src), like a deploy, so stdout
behaves exactly as flow-deploy's does once the session is gone. Installs
handlers for the signals deploy() handles and records each one it receives
to /tmp/probe.log instead of acting on it, then keeps running for SECONDS so
we can see whether it would have finished.

Usage: python3 /probe.py SECONDS
"""

import signal
import sys
import time

sys.path.insert(0, "/src")
from flow_deploy import log  # noqa: E402

RECORD = "/tmp/probe.log"


def note(line):
    with open(RECORD, "a") as f:
        f.write(line + "\n")


def handler(signum, frame):
    note(f"signal {signal.Signals(signum).name}")


for name in ("SIGTERM", "SIGINT", "SIGHUP"):
    signal.signal(getattr(signal, name), handler)

seconds = float(sys.argv[1])
note("started")
deadline = time.time() + seconds
while time.time() < deadline:
    log.info("draining...")  # what a deploy prints while it works
    time.sleep(0.2)
note("completed")
