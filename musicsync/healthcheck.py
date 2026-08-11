"""Docker HEALTHCHECK, one check per run mode.

Scheduled containers prove liveness by finishing runs: the loop rewrites a
heartbeat file after every pass, and a file older than two intervals means the
loop is wedged. Server-mode containers are idle between triggers, so there is
no heartbeat to expect -- what matters is that the endpoint still answers.
"""

from __future__ import annotations

import os
import sys
import time
import urllib.error
import urllib.request

from .config import env_int, env_str, resolve_mode

GRACE_S = 600


def _check_server() -> int:
    port = env_int("HTTP_PORT", 8477, minimum=1)
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            if response.status == 200:
                return 0
            print(f"{url} returned {response.status}", file=sys.stderr)
            return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"{url} unreachable: {exc}", file=sys.stderr)
        return 1


def _check_heartbeat() -> int:
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


def main() -> int:
    mode = resolve_mode()
    if mode == "once":
        # A one-shot container has no ongoing liveness to check.
        return 0
    if mode == "server":
        return _check_server()
    return _check_heartbeat()


if __name__ == "__main__":
    raise SystemExit(main())
