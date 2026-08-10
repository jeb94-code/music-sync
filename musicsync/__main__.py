"""Container entrypoint: run the mirror once, or on a schedule."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from types import FrameType

from . import __version__
from .cache import MatchCache
from .config import Config, format_playlists, load_config, redact
from .deezer import DeezerClient
from .errors import ConfigError, MusicSyncError
from .httpclient import HttpClient
from .spotify import RefreshTokenStore, SpotifyClient
from .sync import Synchronizer, summarize

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
    log.info("Received %s; shutting down after the current step", signal.Signals(signum).name)
    _shutdown.set()


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
        "Interval: %s | dry-run: %s | data dir: %s",
        "run once" if config.run_once else f"every {config.interval_minutes} min",
        config.dry_run,
        config.data_dir,
    )


def run_once(config: Config) -> int:
    """Perform a single sync pass. Returns the number of failed playlists."""
    os.makedirs(config.data_dir, exist_ok=True)
    with HttpClient(
        timeout=config.http_timeout_s,
        max_retries=config.http_max_retries,
        user_agent=f"music-sync/{__version__}",
    ) as http, MatchCache(config.cache_path) as cache:
        spotify = SpotifyClient(
            http,
            client_id=config.spotify_client_id,
            client_secret=config.spotify_client_secret,
            refresh_token=config.spotify_refresh_token,
            token_store=RefreshTokenStore(
                os.path.join(config.data_dir, "spotify-token.json")
            ),
        )
        deezer = DeezerClient(http, config.deezer_access_token)
        synchronizer = Synchronizer(config, deezer, spotify, cache)
        result = synchronizer.run()

    for line in summarize(result).splitlines():
        log.info("%s", line)
    return len(result.failed)


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

    if config.run_once:
        try:
            return 1 if run_once(config) else 0
        except MusicSyncError as exc:
            log.error("Sync failed: %s", exc)
            return 1

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


if __name__ == "__main__":
    raise SystemExit(main())
