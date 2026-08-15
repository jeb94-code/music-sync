"""Shared fixtures and in-memory doubles for the API clients."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from musicsync.cache import MatchCache  # noqa: E402
from musicsync.config import Config, PlaylistPair  # noqa: E402
from musicsync.matching import Candidate  # noqa: E402
from musicsync.spotify import SpotifyPlaylist  # noqa: E402


def make_config(playlists: list[PlaylistPair], tmp_path: Path, **overrides: Any) -> Config:
    defaults: dict[str, Any] = {
        "spotify_client_id": "client-id",
        "spotify_client_secret": "client-secret",
        "spotify_refresh_token": "refresh-token",
        "deezer_access_token": "deezer-token",
        "playlists": playlists,
        "data_dir": str(tmp_path / "data"),
    }
    defaults.update(overrides)
    return Config(**defaults)


def deezer_raw_track(
    track_id: str,
    title: str,
    artist: str,
    duration: int = 200,
    isrc: str | None = None,
    album: str = "An Album",
) -> dict[str, Any]:
    return {
        "id": int(track_id),
        "title": title,
        "duration": duration,
        "isrc": isrc,
        "artist": {"id": 1, "name": artist},
        "album": {"id": 1, "title": album},
        "type": "track",
    }


class FakeDeezerClient:
    """Serves playlists from a dict, mimicking Deezer's response shapes."""

    def __init__(self, playlists: dict[str, dict[str, Any]]) -> None:
        self.playlists = playlists
        self.track_calls: list[str] = []
        # Lets a test simulate a truncated read without touching pagination.
        self.truncate_after: int | None = None
        # Richer payloads for /track/{id}, mirroring the real API: the
        # single-track endpoint returns fields the playlist listing omits.
        self.detail_overrides: dict[str, dict[str, Any]] = {}

    def get_playlist(self, playlist_id: str) -> dict[str, Any]:
        playlist = self.playlists[playlist_id]
        return {
            "id": int(playlist_id),
            "title": playlist["title"],
            "nb_tracks": playlist.get("nb_tracks", len(playlist["tracks"])),
        }

    def iter_playlist_tracks(self, playlist_id: str) -> Iterator[dict[str, Any]]:
        tracks = self.playlists[playlist_id]["tracks"]
        if self.truncate_after is not None:
            tracks = tracks[: self.truncate_after]
        yield from tracks

    def get_track(self, track_id: str) -> dict[str, Any]:
        self.track_calls.append(track_id)
        if track_id in self.detail_overrides:
            return self.detail_overrides[track_id]
        for playlist in self.playlists.values():
            for raw in playlist["tracks"]:
                if str(raw["id"]) == str(track_id):
                    return raw
        raise AssertionError(f"unknown track {track_id}")


class FakeSpotifyClient:
    """In-memory Spotify with a searchable catalogue."""

    def __init__(
        self,
        catalogue: list[Candidate] | None = None,
        playlists: dict[str, dict[str, Any]] | None = None,
        user_id: str = "me",
    ) -> None:
        self.catalogue = catalogue or []
        self.playlists = playlists or {}
        self.user_id = user_id
        self.market = "DE"
        self.search_calls: list[str] = []
        self.writes: list[tuple[str, list[str]]] = []
        self.created: list[str] = []

    # -- playlists ---------------------------------------------------------

    def get_playlist(self, playlist_id: str) -> SpotifyPlaylist:
        playlist = self.playlists[playlist_id]
        return SpotifyPlaylist(
            id=playlist_id,
            name=playlist["name"],
            owner_id=playlist.get("owner", self.user_id),
            track_total=len(playlist["uris"]),
            snapshot_id="snap",
        )

    def find_playlist_by_name(self, name: str) -> SpotifyPlaylist | None:
        for playlist_id, playlist in self.playlists.items():
            if playlist["name"].casefold() != name.casefold():
                continue
            if playlist.get("owner", self.user_id) != self.user_id:
                continue
            return self.get_playlist(playlist_id)
        return None

    def create_playlist(
        self, name: str, *, description: str = "", public: bool = False
    ) -> SpotifyPlaylist:
        playlist_id = f"new{len(self.playlists) + 1}"
        self.playlists[playlist_id] = {
            "name": name, "owner": self.user_id, "uris": []
        }
        self.created.append(name)
        return self.get_playlist(playlist_id)

    def get_playlist_track_uris(self, playlist_id: str) -> list[str]:
        return list(self.playlists[playlist_id]["uris"])

    def replace_playlist_tracks(self, playlist_id: str, uris: list[str]) -> None:
        self.playlists[playlist_id]["uris"] = list(uris)
        self.writes.append((playlist_id, list(uris)))

    # -- search ------------------------------------------------------------

    def search_by_isrc(self, isrc: str) -> list[Candidate]:
        self.search_calls.append(f"isrc:{isrc}")
        return [c for c in self.catalogue if (c.isrc or "").upper() == isrc.upper()]

    def search_by_metadata(self, title: str, artist: str, limit: int) -> list[Candidate]:
        self.search_calls.append(f"meta:{title}|{artist}")
        needle = title.casefold()
        return [c for c in self.catalogue if needle in c.name.casefold()][:limit]


@pytest.fixture
def cache(tmp_path: Path) -> Iterator[MatchCache]:
    with MatchCache(str(tmp_path / "cache.sqlite3")) as match_cache:
        yield match_cache
