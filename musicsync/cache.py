"""Persistent match cache.

Hourly runs would otherwise re-resolve every track on every pass. The cache
lives on the Docker volume, so a restart or image rebuild keeps the work.

Two things are stored:
  * Deezer track detail (ISRC and metadata), which never changes for a
    given track ID and is therefore cached indefinitely.
  * The Deezer track -> Spotify URI mapping. Misses are cached too, but only
    for a limited time, since Spotify's catalogue gains tracks over time.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS track_map (
    deezer_track_id TEXT PRIMARY KEY,
    spotify_uri     TEXT,
    method          TEXT,
    matched_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS deezer_track (
    deezer_track_id TEXT PRIMARY KEY,
    isrc            TEXT,
    title           TEXT,
    artist          TEXT,
    duration        INTEGER,
    fetched_at      INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class CachedMatch:
    spotify_uri: str | None
    method: str | None
    matched_at: int


class MatchCache:
    def __init__(self, path: str, *, negative_ttl_days: int = 7) -> None:
        self.path = path
        self.negative_ttl_s = negative_ttl_days * 86400
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        # WAL keeps the file readable while a run is writing to it.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "MatchCache":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- Deezer track detail ------------------------------------------------

    def get_track_detail(self, deezer_track_id: str) -> sqlite3.Row | None:
        cursor = self.conn.execute(
            "SELECT isrc, title, artist, duration FROM deezer_track "
            "WHERE deezer_track_id = ?",
            (deezer_track_id,),
        )
        return cursor.fetchone()

    def put_track_detail(
        self,
        deezer_track_id: str,
        *,
        isrc: str | None,
        title: str,
        artist: str,
        duration: int,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO deezer_track "
            "(deezer_track_id, isrc, title, artist, duration, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (deezer_track_id, isrc, title, artist, duration, int(time.time())),
        )

    # -- Deezer -> Spotify mapping -----------------------------------------

    def get_match(self, deezer_track_id: str) -> CachedMatch | None:
        cursor = self.conn.execute(
            "SELECT spotify_uri, method, matched_at FROM track_map "
            "WHERE deezer_track_id = ?",
            (deezer_track_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        # Re-check past failures once the negative entry has aged out.
        if row["spotify_uri"] is None:
            if time.time() - row["matched_at"] > self.negative_ttl_s:
                return None
        return CachedMatch(
            spotify_uri=row["spotify_uri"],
            method=row["method"],
            matched_at=row["matched_at"],
        )

    def put_match(
        self, deezer_track_id: str, spotify_uri: str | None, method: str
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO track_map "
            "(deezer_track_id, spotify_uri, method, matched_at) VALUES (?, ?, ?, ?)",
            (deezer_track_id, spotify_uri, method, int(time.time())),
        )

    def commit(self) -> None:
        self.conn.commit()
