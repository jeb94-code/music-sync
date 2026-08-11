"""HTTP front end so an external scheduler (n8n, Home Assistant, cron) can
drive the sync instead of the built-in loop.

Endpoints:

    GET  /health          liveness, no authentication
    GET  /status          last run summary, plus whether one is in progress
    POST /sync            run now, returns the summary
    POST /sync?wait=false start a run and return immediately (poll /status)

Every endpoint except /health requires the token from API_TOKEN, sent either
as `X-Auth-Token: <token>` or `Authorization: Bearer <token>`. A run rewrites
playlists, so an unauthenticated trigger on the LAN is not acceptable.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from .config import Config
from .errors import MusicSyncError
from .runner import execute_run, run_to_dict
from .sync import summarize

log = logging.getLogger(__name__)


class SyncService:
    """Serialises runs and remembers the most recent result."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._running = False
        self._last_result: dict[str, Any] | None = None
        self._state_lock = threading.Lock()

    @property
    def running(self) -> bool:
        with self._state_lock:
            return self._running

    @property
    def last_result(self) -> dict[str, Any] | None:
        with self._state_lock:
            return self._last_result

    def run(self) -> dict[str, Any]:
        """Execute a run. Raises RuntimeError if one is already in progress."""
        # Non-blocking: a second trigger should be told to come back later
        # rather than queue up behind a run that may take minutes.
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("a sync is already running")
        with self._state_lock:
            self._running = True
        try:
            run = execute_run(self.config)
            payload = run_to_dict(run, dry_run=self.config.dry_run)
            for line in summarize(run).splitlines():
                log.info("%s", line)
            with self._state_lock:
                self._last_result = payload
            return payload
        except MusicSyncError as exc:
            payload = {"error": str(exc), "version": __version__}
            with self._state_lock:
                self._last_result = payload
            raise
        finally:
            with self._state_lock:
                self._running = False
            self._lock.release()

    def run_in_background(self) -> None:
        if self.running:
            raise RuntimeError("a sync is already running")

        def target() -> None:
            try:
                self.run()
            except RuntimeError:
                log.info("Background run skipped; another run started first")
            except Exception:  # noqa: BLE001 - the thread must not take the server down
                log.exception("Background sync failed")

        threading.Thread(target=target, name="music-sync-run", daemon=True).start()


class _Handler(BaseHTTPRequestHandler):
    service: SyncService
    api_token: str

    server_version = f"music-sync/{__version__}"
    sys_version = ""

    # -- helpers ------------------------------------------------------------

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Auth-Token", "")
        if not supplied:
            header = self.headers.get("Authorization", "")
            if header.lower().startswith("bearer "):
                supplied = header[7:].strip()
        # compare_digest keeps the check constant-time.
        return bool(supplied) and secrets.compare_digest(supplied, self.api_token)

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._send_json(401, {"error": "missing or invalid API token"})
        return False

    # -- routes -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - name mandated by the base class
        path = urlparse(self.path).path.rstrip("/") or "/"

        if path in ("/health", "/"):
            self._send_json(200, {"status": "ok", "version": __version__})
            return

        if path == "/status":
            if not self._require_auth():
                return
            self._send_json(
                200,
                {
                    "running": self.service.running,
                    "last_run": self.service.last_result,
                    "version": __version__,
                },
            )
            return

        self._send_json(404, {"error": f"no such endpoint: {path}"})

    def do_POST(self) -> None:  # noqa: N802 - name mandated by the base class
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path != "/sync":
            self._send_json(404, {"error": f"no such endpoint: {path}"})
            return
        if not self._require_auth():
            return

        # Drain any request body so the connection can be reused.
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)

        wait = (parse_qs(parsed.query).get("wait", ["true"])[0]).lower()
        if wait in ("false", "0", "no"):
            try:
                self.service.run_in_background()
            except RuntimeError as exc:
                self._send_json(409, {"error": str(exc)})
                return
            self._send_json(202, {"status": "started"})
            return

        try:
            payload = self.service.run()
        except RuntimeError as exc:
            self._send_json(409, {"error": str(exc)})
            return
        except MusicSyncError as exc:
            self._send_json(500, {"error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - report, do not kill the server
            log.exception("Sync request failed")
            self._send_json(500, {"error": repr(exc)})
            return

        self._send_json(200, payload)

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s - %s", self.client_address[0], fmt % args)


def create_server(config: Config) -> ThreadingHTTPServer:
    """Build the bound HTTP server without starting it."""
    if not config.api_token:
        raise MusicSyncError(
            "API_TOKEN must be set when MODE=server. A request to /sync rewrites "
            "your Spotify playlists, so the endpoint is never left unauthenticated."
        )

    handler = type(
        "BoundHandler",
        (_Handler,),
        {"service": SyncService(config), "api_token": config.api_token},
    )
    httpd = ThreadingHTTPServer((config.http_bind, config.http_port), handler)
    httpd.daemon_threads = True
    return httpd


def serve(config: Config, stop: threading.Event | None = None) -> None:
    """Run the HTTP server until `stop` is set (or forever if it is None).

    serve_forever() blocks and cannot be interrupted from a signal handler, so
    it runs on its own thread while the caller waits on the event. Without
    this, `docker stop` would sit through its full timeout and then SIGKILL.
    """
    httpd = create_server(config)
    log.info(
        "Listening on %s:%d - POST /sync to trigger, GET /status for the last run",
        *httpd.server_address[:2],
    )

    thread = threading.Thread(
        target=httpd.serve_forever, name="music-sync-http", daemon=True
    )
    thread.start()
    try:
        if stop is not None:
            stop.wait()
        else:
            thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
