from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeDeezerClient, FakeSpotifyClient, deezer_raw_track, make_config

from musicsync.cache import MatchCache
from musicsync.config import PlaylistPair
from musicsync.errors import MusicSyncError, SafetyAbort
from musicsync.matching import Candidate
from musicsync.sync import Synchronizer


def spotify_track(uri: str, name: str, artist: str, isrc: str, duration: int = 200):
    return Candidate(
        uri=uri, name=name, artists=[artist], duration_s=duration, isrc=isrc
    )


@pytest.fixture
def world(tmp_path: Path, cache: MatchCache):
    """A Deezer playlist of three tracks with exact Spotify counterparts."""
    deezer = FakeDeezerClient(
        {
            "100": {
                "title": "Rock",
                "tracks": [
                    deezer_raw_track("1", "Alpha", "Band A", 200, "AAA111"),
                    deezer_raw_track("2", "Beta", "Band B", 210, "BBB222"),
                    deezer_raw_track("3", "Gamma", "Band C", 220, "CCC333"),
                ],
            }
        }
    )
    spotify = FakeSpotifyClient(
        catalogue=[
            spotify_track("spotify:track:a", "Alpha", "Band A", "AAA111", 200),
            spotify_track("spotify:track:b", "Beta", "Band B", "BBB222", 210),
            spotify_track("spotify:track:c", "Gamma", "Band C", "CCC333", 220),
        ],
        playlists={"sp1": {"name": "Rock Mirror", "owner": "me", "uris": []}},
    )
    return deezer, spotify, cache


def build(world, tmp_path: Path, pair: PlaylistPair, **overrides):
    deezer, spotify, cache = world
    config = make_config([pair], tmp_path, **overrides)
    return Synchronizer(config, deezer, spotify, cache), spotify


class TestMirroring:
    def test_tracks_are_mirrored_in_deezer_order(self, world, tmp_path: Path) -> None:
        sync, spotify = build(
            world, tmp_path, PlaylistPair(deezer_id="100", spotify_id="sp1")
        )
        result = sync.sync_pair(PlaylistPair(deezer_id="100", spotify_id="sp1"))

        assert result.matched_tracks == 3
        assert spotify.playlists["sp1"]["uris"] == [
            "spotify:track:a", "spotify:track:b", "spotify:track:c",
        ]
        assert result.changed is True
        assert result.added == 3

    def test_deletions_are_mirrored(self, world, tmp_path: Path) -> None:
        deezer, spotify, _ = world
        spotify.playlists["sp1"]["uris"] = [
            "spotify:track:a", "spotify:track:zz", "spotify:track:b",
            "spotify:track:c",
        ]
        pair = PlaylistPair(deezer_id="100", spotify_id="sp1")
        sync, _ = build(world, tmp_path, pair)
        result = sync.sync_pair(pair)

        assert "spotify:track:zz" not in spotify.playlists["sp1"]["uris"]
        assert result.removed == 1
        assert result.added == 0

    def test_reordering_is_mirrored(self, world, tmp_path: Path) -> None:
        _, spotify, _ = world
        spotify.playlists["sp1"]["uris"] = [
            "spotify:track:c", "spotify:track:b", "spotify:track:a",
        ]
        pair = PlaylistPair(deezer_id="100", spotify_id="sp1")
        sync, _ = build(world, tmp_path, pair)
        result = sync.sync_pair(pair)

        assert spotify.playlists["sp1"]["uris"] == [
            "spotify:track:a", "spotify:track:b", "spotify:track:c",
        ]
        assert result.reordered is True

    def test_no_write_when_already_in_sync(self, world, tmp_path: Path) -> None:
        _, spotify, _ = world
        spotify.playlists["sp1"]["uris"] = [
            "spotify:track:a", "spotify:track:b", "spotify:track:c",
        ]
        pair = PlaylistPair(deezer_id="100", spotify_id="sp1")
        sync, _ = build(world, tmp_path, pair)
        result = sync.sync_pair(pair)

        assert result.changed is False
        assert spotify.writes == []

    def test_dry_run_does_not_write(self, world, tmp_path: Path) -> None:
        pair = PlaylistPair(deezer_id="100", spotify_id="sp1")
        sync, spotify = build(world, tmp_path, pair, dry_run=True)
        result = sync.sync_pair(pair)

        assert result.changed is True
        assert spotify.writes == []
        assert spotify.playlists["sp1"]["uris"] == []


class TestTargetResolution:
    def test_existing_playlist_found_by_name(self, world, tmp_path: Path) -> None:
        pair = PlaylistPair(deezer_id="100", spotify_name="Rock Mirror")
        sync, spotify = build(world, tmp_path, pair)
        sync.sync_pair(pair)

        assert spotify.created == []
        assert len(spotify.playlists["sp1"]["uris"]) == 3

    def test_playlist_created_when_missing(self, world, tmp_path: Path) -> None:
        pair = PlaylistPair(deezer_id="100", spotify_name="Brand New")
        sync, spotify = build(world, tmp_path, pair)
        sync.sync_pair(pair)

        assert spotify.created == ["Brand New"]

    def test_creation_can_be_disabled(self, world, tmp_path: Path) -> None:
        pair = PlaylistPair(deezer_id="100", spotify_name="Brand New")
        sync, _ = build(world, tmp_path, pair, create_missing=False)
        with pytest.raises(MusicSyncError, match="CREATE_MISSING_PLAYLISTS"):
            sync.sync_pair(pair)

    def test_falls_back_to_deezer_playlist_name(self, world, tmp_path: Path) -> None:
        pair = PlaylistPair(deezer_id="100")
        sync, spotify = build(world, tmp_path, pair)
        sync.sync_pair(pair)

        assert spotify.created == ["Rock"]

    def test_refuses_playlist_owned_by_somebody_else(
        self, world, tmp_path: Path
    ) -> None:
        _, spotify, _ = world
        spotify.playlists["sp2"] = {
            "name": "Not Mine", "owner": "someone-else", "uris": []
        }
        pair = PlaylistPair(deezer_id="100", spotify_id="sp2")
        sync, _ = build(world, tmp_path, pair)
        with pytest.raises(MusicSyncError, match="owned by"):
            sync.sync_pair(pair)


class TestSafety:
    def test_partial_deezer_read_aborts(self, world, tmp_path: Path) -> None:
        deezer, _, _ = world
        deezer.truncate_after = 1  # Simulate a page that never arrived.
        pair = PlaylistPair(deezer_id="100", spotify_id="sp1")
        sync, spotify = build(world, tmp_path, pair)

        with pytest.raises(SafetyAbort, match="only 1 could be read"):
            sync.sync_pair(pair)
        assert spotify.writes == []

    def test_partial_read_can_be_opted_into(self, world, tmp_path: Path) -> None:
        deezer, spotify, _ = world
        deezer.truncate_after = 2
        pair = PlaylistPair(deezer_id="100", spotify_id="sp1")
        sync, _ = build(world, tmp_path, pair, allow_partial_source=True)
        result = sync.sync_pair(pair)

        assert result.matched_tracks == 2
        assert len(spotify.playlists["sp1"]["uris"]) == 2

    def test_wiping_a_playlist_requires_opt_in(self, world, tmp_path: Path) -> None:
        deezer, spotify, _ = world
        spotify.catalogue = []  # Nothing will match any more.
        spotify.playlists["sp1"]["uris"] = ["spotify:track:a"]
        pair = PlaylistPair(deezer_id="100", spotify_id="sp1")
        sync, _ = build(world, tmp_path, pair)

        with pytest.raises(SafetyAbort, match="ALLOW_EMPTY_MIRROR"):
            sync.sync_pair(pair)
        assert spotify.playlists["sp1"]["uris"] == ["spotify:track:a"]

    def test_wiping_allowed_when_opted_in(self, world, tmp_path: Path) -> None:
        _, spotify, _ = world
        spotify.catalogue = []
        spotify.playlists["sp1"]["uris"] = ["spotify:track:a"]
        pair = PlaylistPair(deezer_id="100", spotify_id="sp1")
        sync, _ = build(world, tmp_path, pair, allow_empty_mirror=True)
        sync.sync_pair(pair)

        assert spotify.playlists["sp1"]["uris"] == []

    def test_unmatched_ratio_guard(self, world, tmp_path: Path) -> None:
        _, spotify, _ = world
        # Only one of three tracks is still findable.
        spotify.catalogue = [
            spotify_track("spotify:track:a", "Alpha", "Band A", "AAA111", 200)
        ]
        pair = PlaylistPair(deezer_id="100", spotify_id="sp1")
        sync, _ = build(world, tmp_path, pair, max_unmatched_ratio=0.5)

        with pytest.raises(SafetyAbort, match="MAX_UNMATCHED_RATIO"):
            sync.sync_pair(pair)
        assert spotify.writes == []

    def test_empty_deezer_playlist_clears_target_without_opt_in(
        self, tmp_path: Path, cache: MatchCache
    ) -> None:
        # An intentionally emptied Deezer playlist is a real state to mirror;
        # only a *failure to match* is treated as suspicious.
        deezer = FakeDeezerClient({"100": {"title": "Rock", "tracks": []}})
        spotify = FakeSpotifyClient(
            playlists={"sp1": {"name": "Rock Mirror", "owner": "me",
                               "uris": ["spotify:track:a"]}}
        )
        pair = PlaylistPair(deezer_id="100", spotify_id="sp1")
        sync, _ = build((deezer, spotify, cache), tmp_path, pair,
                        allow_empty_mirror=True)
        sync.sync_pair(pair)
        assert spotify.playlists["sp1"]["uris"] == []


class TestMatching:
    def test_isrc_is_preferred_over_search(self, world, tmp_path: Path) -> None:
        pair = PlaylistPair(deezer_id="100", spotify_id="sp1")
        sync, spotify = build(world, tmp_path, pair)
        sync.sync_pair(pair)

        assert all(call.startswith("isrc:") for call in spotify.search_calls)

    def test_falls_back_to_metadata_search(self, tmp_path: Path, cache: MatchCache):
        deezer = FakeDeezerClient(
            {
                "100": {
                    "title": "Rock",
                    "tracks": [deezer_raw_track("1", "Alpha", "Band A", 200, None)],
                }
            }
        )
        spotify = FakeSpotifyClient(
            catalogue=[
                Candidate(
                    uri="spotify:track:a", name="Alpha", artists=["Band A"],
                    duration_s=200, isrc="AAA111",
                )
            ],
            playlists={"sp1": {"name": "Rock Mirror", "owner": "me", "uris": []}},
        )
        pair = PlaylistPair(deezer_id="100", spotify_id="sp1")
        sync, _ = build((deezer, spotify, cache), tmp_path, pair)
        sync.sync_pair(pair)

        assert any(call.startswith("meta:") for call in spotify.search_calls)
        assert spotify.playlists["sp1"]["uris"] == ["spotify:track:a"]

    def test_unmatched_tracks_are_reported(self, world, tmp_path: Path) -> None:
        _, spotify, _ = world
        spotify.catalogue = [
            spotify_track("spotify:track:a", "Alpha", "Band A", "AAA111", 200)
        ]
        pair = PlaylistPair(deezer_id="100", spotify_id="sp1")
        sync, _ = build(world, tmp_path, pair)
        result = sync.sync_pair(pair)

        assert result.matched_tracks == 1
        assert [t.label for t in result.unmatched] == ["Band B - Beta", "Band C - Gamma"]

    def test_second_run_uses_the_cache(self, world, tmp_path: Path) -> None:
        pair = PlaylistPair(deezer_id="100", spotify_id="sp1")
        sync, spotify = build(world, tmp_path, pair)
        sync.sync_pair(pair)
        first_pass_calls = len(spotify.search_calls)
        assert first_pass_calls > 0

        sync.sync_pair(pair)
        assert len(spotify.search_calls) == first_pass_calls

    def test_cache_survives_a_restart(self, world, tmp_path: Path) -> None:
        deezer, spotify, _ = world
        pair = PlaylistPair(deezer_id="100", spotify_id="sp1")
        cache_path = str(tmp_path / "persist.sqlite3")

        with MatchCache(cache_path) as first:
            build((deezer, spotify, first), tmp_path, pair)[0].sync_pair(pair)
        calls_after_first = len(spotify.search_calls)

        with MatchCache(cache_path) as second:
            build((deezer, spotify, second), tmp_path, pair)[0].sync_pair(pair)
        assert len(spotify.search_calls) == calls_after_first


class TestRun:
    def test_one_failing_playlist_does_not_stop_the_others(
        self, world, tmp_path: Path
    ) -> None:
        deezer, spotify, cache = world
        deezer.playlists["200"] = {
            "title": "Chill",
            "tracks": [deezer_raw_track("1", "Alpha", "Band A", 200, "AAA111")],
        }
        spotify.playlists["sp2"] = {
            "name": "Not Mine", "owner": "someone-else", "uris": []
        }
        config = make_config(
            [
                PlaylistPair(deezer_id="200", spotify_id="sp2"),  # fails: not ours
                PlaylistPair(deezer_id="100", spotify_id="sp1"),  # succeeds
            ],
            tmp_path,
        )
        result = Synchronizer(config, deezer, spotify, cache).run()

        assert len(result.failed) == 1
        assert len(spotify.playlists["sp1"]["uris"]) == 3

    def test_heartbeat_and_report_are_written(self, world, tmp_path: Path) -> None:
        deezer, spotify, cache = world
        spotify.catalogue = []
        config = make_config(
            [PlaylistPair(deezer_id="100", spotify_id="sp1")],
            tmp_path,
            allow_empty_mirror=True,
        )
        Synchronizer(config, deezer, spotify, cache).run()

        assert Path(config.heartbeat_path).exists()
        report = Path(config.report_path).read_text(encoding="utf-8")
        assert "Band A - Alpha" in report
        assert "deezer:1" in report
