"""Docker HEALTHCHECK: is the scheduler still completing runs?

The sync loop rewrites a heartbeat file at the end of every pass. If that file
is older than two intervals plus a grace period, the loop is wedged and the
container should be reported unhealthy so Docker or Portainer can restart it.
"""

from __future__ import annotations

import os
import sys
import time

from .config import env_bool, env_int, env_str

GRACE_S = 600


def main() -> int:
    if env_bool("RUN_ONCE", False):
        # A one-shot container has no ongoing liveness to check.
        return 0

    data_dir = env_str("DATA_DIR", "/data") or "/data"
    heartbeat = os.path.join(data_dir, "heartbeat")
    interval_s = env_int("SYNC_INTERVAL_MINUTES", 60, minimum=1) * 60
    deadline = 2 * interval_s + GRACE_S

    try:
        mtime = os.path.getmtime(heartbeat)
    except OSError:
        # The first run may still be in progress; only fail once it is overdue.
        # /proc/1 is the sync loop itself, so its mtime is the container start
        # time -- /proc/self would be this short-lived healthcheck process.
        try:
            started = os.path.getmtime("/proc/1")
        except OSError:
            return 0
        if time.time() - started > deadline:
            print("no heartbeat file and first run is overdue", file=sys.stderr)
            return 1
        return 0

    age = time.time() - mtime
    if age > deadline:
        print(f"heartbeat is {age:.0f}s old (limit {deadline}s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
