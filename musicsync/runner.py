"""Wiring a single sync run together, plus its JSON representation.

Both entrypoints use this: the built-in scheduler and the HTTP server that
n8n drives. Clients and the cache are built per run so that a long-lived
server does not hold a SQLite connection or an HTTP session open for days.
"""

from __future__ import annotations

import os
from typing import Any

from . import __version__
from .cache import MatchCache
from .config import Config
from .deezer import DeezerClient
from .httpclient import HttpClient
from .spotify import RefreshTokenStore, SpotifyClient
from .sync import RunResult, Synchronizer


def execute_run(config: Config) -> RunResult:
    """Perform one full sync pass over every configured playlist."""
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
        return Synchronizer(config, deezer, spotify, cache).run()


def run_to_dict(run: RunResult, *, dry_run: bool = False) -> dict[str, Any]:
    """Shape a run for JSON consumers such as n8n.

    Everything a workflow might branch on lives under `totals`, so an IF node
    can read a single number instead of walking the playlist array.
    """
    playlists: list[dict[str, Any]] = []
    for result in run.playlists:
        playlists.append(
            {
                "deezer_id": result.pair.deezer_id,
                "deezer_title": result.deezer_title,
                "spotify_id": result.spotify_id,
                "spotify_name": result.spotify_name,
                "source_tracks": result.source_tracks,
                "matched_tracks": result.matched_tracks,
                "changed": result.changed,
                "added": result.added,
                "removed": result.removed,
                "reordered": result.reordered,
                "error": result.error,
                "skipped_reason": result.skipped_reason,
                "unmatched": [
                    {
                        "deezer_id": track.deezer_id,
                        "label": track.label,
                        "isrc": track.isrc,
                    }
                    for track in result.unmatched
                ],
            }
        )

    failed = sum(1 for result in run.playlists if result.error)
    skipped = sum(1 for result in run.playlists if result.skipped_reason)
    changed = sum(1 for result in run.playlists if result.changed)
    unmatched = sum(len(result.unmatched) for result in run.playlists)

    return {
        "version": __version__,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat(),
        "duration_s": round(run.duration_s, 2),
        "dry_run": dry_run,
        "totals": {
            "playlists": len(run.playlists),
            "failed": failed,
            "skipped": skipped,
            "changed": changed,
            "unmatched_tracks": unmatched,
        },
        "playlists": playlists,
    }
