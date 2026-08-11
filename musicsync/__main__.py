"""Container entrypoint: run the mirror once, on a schedule, or on demand."""

from __future__ import annotations

import logging
import signal
import sys
import threading
from types import FrameType

from . import __version__
from .config import Config, format_playlists, load_config, redact
from .errors import ConfigError, MusicSyncError
from .runner import execute_run
from .sync import summarize

log = logging.getLogger("musicsync")

_shutdown = threading.Event()


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # requests/urllib3 chatter is noise at DEBUG and can echo URLs with tokens.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _handle_signal(signum: int, _frame: FrameType | None) -> None:
    log.info(
        "Received %s; shutting down after the current step",
        signal.Signals(signum).name,
    )
    _shutdown.set()


def _describe_mode(config: Config) -> str:
    if config.mode == "once":
        return "run once"
    if config.mode == "server":
        return f"HTTP trigger on {config.http_bind}:{config.http_port}"
    return f"every {config.interval_minutes} min"


def _log_startup(config: Config) -> None:
    log.info("music-sync %s starting", __version__)
    log.info("Spotify client id: %s", redact(config.spotify_client_id))
    log.info(
        "Deezer token: %s",
        redact(config.deezer_access_token) if config.deezer_access_token else
        "<unset> (only public playlists are readable)",
    )
    log.info("Mirroring Deezer -> Spotify, Deezer is master:")
    for line in format_playlists(config.playlists).splitlines():
        log.info("%s", line)
    log.info(
        "Mode: %s | dry-run: %s | data dir: %s",
        _describe_mode(config), config.dry_run, config.data_dir,
    )


def run_once(config: Config) -> int:
    """Perform a single sync pass. Returns the number of failed playlists."""
    result = execute_run(config)
    for line in summarize(result).splitlines():
        log.info("%s", line)
    return len(result.failed)


def _run_scheduled(config: Config) -> int:
    while not _shutdown.is_set():
        try:
            run_once(config)
        except MusicSyncError as exc:
            # Keep the container alive; the next tick may well succeed.
            log.error("Sync run failed: %s", exc)
        except Exception:  # noqa: BLE001 - a scheduled service must not die
            log.exception("Unexpected error during sync run")

        if _shutdown.is_set():
            break
        log.info("Next run in %d minutes", config.interval_minutes)
        _shutdown.wait(config.interval_minutes * 60)

    log.info("Stopped.")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        print("\nConfiguration is read from environment variables; see README.md.")
        return 0

    try:
        config = load_config()
    except ConfigError as exc:
        # Logging is not configured yet, so write straight to stderr.
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    _setup_logging(config.log_level)
    _log_startup(config)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    if config.mode == "once":
        try:
            return 1 if run_once(config) else 0
        except MusicSyncError as exc:
            log.error("Sync failed: %s", exc)
            return 1

    if config.mode == "server":
        # Imported here so scheduled containers never load the HTTP stack.
        from .server import serve

        try:
            serve(config, _shutdown)
        except MusicSyncError as exc:
            log.error("%s", exc)
            return 2
        except KeyboardInterrupt:
            pass
        log.info("Stopped.")
        return 0

    return _run_scheduled(config)


if __name__ == "__main__":
    raise SystemExit(main())
