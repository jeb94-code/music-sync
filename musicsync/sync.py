"""The mirror itself.

Deezer is the master. For every configured pair the Spotify playlist is made
to match the Deezer playlist exactly -- same tracks, same order, removals
included. Spotify-side edits do not survive a run; that is the point.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .cache import MatchCache
from .config import Config, PlaylistPair
from .deezer import DeezerClient, DeezerTrack, all_artist_names, parse_track
from .errors import MusicSyncError, SafetyAbort
from .matching import Candidate, best_match
from .spotify import SpotifyClient, SpotifyPlaylist

log = logging.getLogger(__name__)

# ISRC hits are authoritative, so they bypass the similarity threshold.
_MATCH_ISRC = "isrc"
_MATCH_SEARCH = "search"
_MATCH_CACHE = "cache"

# Artist lists are stored in one cache column; ASCII unit separator cannot
# occur in an artist name, unlike a comma or a slash.
_ARTIST_DELIMITER = "\x1f"


@dataclass
class UnmatchedTrack:
    deezer_id: str
    label: str
    isrc: str | None


@dataclass
class PlaylistResult:
    pair: PlaylistPair
    deezer_title: str = ""
    spotify_name: str = ""
    spotify_id: str = ""
    source_tracks: int = 0
    matched_tracks: int = 0
    unmatched: list[UnmatchedTrack] = field(default_factory=list)
    changed: bool = False
    added: int = 0
    removed: int = 0
    reordered: bool = False
    skipped_reason: str | None = None
    error: str | None = None


@dataclass
class RunResult:
    started_at: datetime
    finished_at: datetime
    playlists: list[PlaylistResult] = field(default_factory=list)

    @property
    def failed(self) -> list[PlaylistResult]:
        return [result for result in self.playlists if result.error]

    @property
    def duration_s(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


class Synchronizer:
    def __init__(
        self,
        config: Config,
        deezer: DeezerClient,
        spotify: SpotifyClient,
        cache: MatchCache,
    ) -> None:
        self.config = config
        self.deezer = deezer
        self.spotify = spotify
        self.cache = cache

    # -- Track resolution ---------------------------------------------------

    def _deezer_track_detail(self, track_id: str) -> tuple[str | None, list[str]]:
        """Return (isrc, artist names) for a Deezer track, using the cache.

        Only the single-track endpoint exposes the full contributor list, and
        the ISRC for the rare track whose playlist entry lacks one. Callers
        should avoid this for tracks that can be resolved from the listing
        alone -- it costs one request each. Results never change, so they are
        cached permanently.
        """
        cached = self.cache.get_track_detail(track_id)
        if cached is not None:
            names = (cached["artist"] or "").split(_ARTIST_DELIMITER)
            return cached["isrc"], [name for name in names if name]

        raw = self.deezer.get_track(track_id)
        isrc = (raw.get("isrc") or "").strip() or None
        artists = all_artist_names(raw)
        self.cache.put_track_detail(
            track_id,
            isrc=isrc,
            title=str(raw.get("title") or ""),
            artist=_ARTIST_DELIMITER.join(artists),
            duration=int(raw.get("duration") or 0),
        )
        return isrc, artists

    def _resolve_track(self, track: DeezerTrack) -> tuple[str | None, str]:
        """Map one Deezer track to a Spotify URI. Returns (uri, method)."""
        cached = self.cache.get_match(track.id)
        if cached is not None:
            return cached.spotify_uri, _MATCH_CACHE

        # The playlist listing already carries the ISRC for virtually every
        # track, and an ISRC hit needs nothing else -- so the per-track
        # endpoint is only consulted when the listing came up short. That is
        # the difference between one request per track and one per playlist
        # page on a first run.
        isrc = track.isrc
        artists = [track.artist] if track.artist else []
        if not isrc:
            isrc, detail_artists = self._deezer_track_detail(track.id)
            if detail_artists:
                artists = detail_artists

        if isrc:
            by_isrc = self.spotify.search_by_isrc(isrc)
            exact = next(
                (
                    candidate
                    for candidate in by_isrc
                    if (candidate.isrc or "").upper() == isrc.upper()
                ),
                None,
            )
            chosen = exact or (by_isrc[0] if by_isrc else None)
            if chosen:
                self.cache.put_match(track.id, chosen.uri, _MATCH_ISRC)
                return chosen.uri, _MATCH_ISRC

        # Falling back to search: the full credit list is worth one request
        # here, because a missing featured artist is what makes these fail.
        if len(artists) <= 1:
            _, detail_artists = self._deezer_track_detail(track.id)
            if detail_artists:
                artists = detail_artists

        candidates: list[Candidate] = self.spotify.search_by_metadata(
            track.title, artists[0] if artists else track.artist,
            self.config.search_limit,
        )
        match = best_match(
            title=track.title,
            artists=artists or [track.artist],
            duration_s=track.duration,
            candidates=candidates,
            threshold=self.config.match_threshold,
            duration_tolerance_s=self.config.duration_tolerance_s,
        )
        if match:
            log.debug(
                "Matched %s -> %s (%s)", track.label, match.candidate.name, match.reason
            )
            self.cache.put_match(track.id, match.candidate.uri, _MATCH_SEARCH)
            return match.candidate.uri, _MATCH_SEARCH

        self.cache.put_match(track.id, None, _MATCH_SEARCH)
        return None, _MATCH_SEARCH

    # -- Deezer side --------------------------------------------------------

    def _load_deezer_playlist(self, playlist_id: str) -> tuple[str, list[DeezerTrack]]:
        meta = self.deezer.get_playlist(playlist_id)
        title = str(meta.get("title") or f"Deezer {playlist_id}")
        declared_total = int(meta.get("nb_tracks") or 0)

        tracks: list[DeezerTrack] = []
        raw_count = 0
        for raw in self.deezer.iter_playlist_tracks(playlist_id):
            raw_count += 1
            parsed = parse_track(raw)
            if parsed is None:
                log.warning(
                    "Skipping unusable Deezer entry in playlist %s: %r",
                    playlist_id, raw.get("id"),
                )
                continue
            tracks.append(parsed)

        # A truncated page would look like "the user deleted everything", and we
        # would faithfully mirror that deletion. Refuse instead.
        if declared_total and raw_count < declared_total:
            message = (
                f"Deezer reported {declared_total} tracks for playlist {playlist_id} "
                f"but only {raw_count} could be read."
            )
            if not self.config.allow_partial_source:
                raise SafetyAbort(
                    f"{message} Refusing to mirror a partial playlist; will retry "
                    "on the next run. If this repeats on every run, Deezer is "
                    "counting tracks it no longer serves -- set "
                    "ALLOW_PARTIAL_SOURCE=true to mirror what is readable."
                )
            log.warning("%s Mirroring anyway (ALLOW_PARTIAL_SOURCE).", message)
        return title, tracks

    # -- Spotify side -------------------------------------------------------

    def _resolve_target_playlist(
        self, pair: PlaylistPair, deezer_title: str
    ) -> SpotifyPlaylist:
        if pair.spotify_id:
            playlist = self.spotify.get_playlist(pair.spotify_id)
            if playlist.owner_id and playlist.owner_id != self.spotify.user_id:
                raise MusicSyncError(
                    f"Spotify playlist {pair.spotify_id} is owned by "
                    f"{playlist.owner_id}, not by you. music-sync can only write to "
                    "playlists you own."
                )
            return playlist

        name = pair.spotify_name or deezer_title
        existing = self.spotify.find_playlist_by_name(name)
        if existing:
            return existing

        if not self.config.create_missing:
            raise MusicSyncError(
                f"No Spotify playlist named {name!r} was found and "
                "CREATE_MISSING_PLAYLISTS is disabled."
            )
        if self.config.dry_run:
            log.info("[dry-run] would create Spotify playlist %r", name)
            return SpotifyPlaylist(
                id="", name=name, owner_id=self.spotify.user_id,
                track_total=0, snapshot_id="",
            )

        log.info("Creating Spotify playlist %r", name)
        return self.spotify.create_playlist(
            name,
            description=self.config.playlist_description,
            public=self.config.public_playlists,
        )

    # -- One playlist -------------------------------------------------------

    def sync_pair(self, pair: PlaylistPair) -> PlaylistResult:
        result = PlaylistResult(pair=pair)
        deezer_title, deezer_tracks = self._load_deezer_playlist(pair.deezer_id)
        result.deezer_title = deezer_title
        result.source_tracks = len(deezer_tracks)
        log.info(
            "Deezer playlist %r (%s): %d tracks",
            deezer_title, pair.deezer_id, len(deezer_tracks),
        )

        desired: list[str] = []
        for index, track in enumerate(deezer_tracks, start=1):
            uri, _method = self._resolve_track(track)
            if uri:
                desired.append(uri)
            else:
                # An unmatched track has already been through _resolve_track,
                # so its detail is either on the listing or in the cache.
                isrc = track.isrc or self._deezer_track_detail(track.id)[0]
                result.unmatched.append(
                    UnmatchedTrack(deezer_id=track.id, label=track.label, isrc=isrc)
                )
                log.info("No Spotify match for %s", track.label)
            if index % 50 == 0:
                self.cache.commit()
                log.info("  ... resolved %d/%d tracks", index, len(deezer_tracks))
        self.cache.commit()
        result.matched_tracks = len(desired)

        if deezer_tracks:
            unmatched_ratio = len(result.unmatched) / len(deezer_tracks)
            if unmatched_ratio > self.config.max_unmatched_ratio:
                raise SafetyAbort(
                    f"{len(result.unmatched)}/{len(deezer_tracks)} tracks "
                    f"({unmatched_ratio:.0%}) could not be matched, which exceeds "
                    f"MAX_UNMATCHED_RATIO={self.config.max_unmatched_ratio:.0%}. "
                    "Not touching the Spotify playlist."
                )

        target = self._resolve_target_playlist(pair, deezer_title)
        result.spotify_name = target.name
        result.spotify_id = target.id

        current = (
            self.spotify.get_playlist_track_uris(target.id) if target.id else []
        )
        if current == desired:
            log.info(
                "Spotify playlist %r already matches (%d tracks)",
                target.name, len(desired),
            )
            return result

        current_set, desired_set = set(current), set(desired)
        result.added = len(desired_set - current_set)
        result.removed = len(current_set - desired_set)
        result.reordered = (
            current_set == desired_set and len(current) == len(desired)
        )
        result.changed = True

        if not desired and current and not self.config.allow_empty_mirror:
            raise SafetyAbort(
                f"Mirroring would empty Spotify playlist {target.name!r} "
                f"({len(current)} tracks would be removed) because no Deezer track "
                "could be matched. Set ALLOW_EMPTY_MIRROR=true if that is intended."
            )

        log.info(
            "Spotify playlist %r: %d -> %d tracks (+%d/-%d%s)",
            target.name, len(current), len(desired), result.added, result.removed,
            ", reordered" if result.reordered else "",
        )

        if self.config.dry_run:
            log.info("[dry-run] not writing to Spotify")
            return result

        self.spotify.replace_playlist_tracks(target.id, desired)
        log.info("Updated Spotify playlist %r", target.name)
        return result

    # -- All playlists ------------------------------------------------------

    def run(self) -> RunResult:
        started_at = datetime.now(timezone.utc)
        results: list[PlaylistResult] = []

        for pair in self.config.playlists:
            try:
                results.append(self.sync_pair(pair))
            except SafetyAbort as exc:
                log.error("Skipping %s: %s", pair.describe(), exc)
                result = PlaylistResult(pair=pair, skipped_reason=str(exc))
                results.append(result)
            except MusicSyncError as exc:
                # One broken playlist should not stop the others.
                log.error("Failed %s: %s", pair.describe(), exc)
                results.append(PlaylistResult(pair=pair, error=str(exc)))
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                log.exception("Unexpected failure on %s", pair.describe())
                results.append(PlaylistResult(pair=pair, error=repr(exc)))

        run = RunResult(
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            playlists=results,
        )
        self._write_report(run)
        self._touch_heartbeat()
        return run

    def _write_report(self, run: RunResult) -> None:
        unmatched = [
            (result, track) for result in run.playlists for track in result.unmatched
        ]
        if not unmatched:
            return
        try:
            os.makedirs(self.config.data_dir, exist_ok=True)
            with open(self.config.report_path, "w", encoding="utf-8") as handle:
                handle.write(
                    f"# Tracks with no Spotify match, {run.finished_at.isoformat()}\n"
                )
                for result, track in unmatched:
                    handle.write(
                        f"{result.deezer_title}\t{track.label}\t"
                        f"deezer:{track.deezer_id}\tisrc:{track.isrc or '-'}\n"
                    )
        except OSError as exc:
            log.warning("Could not write %s: %s", self.config.report_path, exc)

    def _touch_heartbeat(self) -> None:
        try:
            os.makedirs(self.config.data_dir, exist_ok=True)
            with open(self.config.heartbeat_path, "w", encoding="utf-8") as handle:
                handle.write(str(int(time.time())))
        except OSError as exc:
            log.warning("Could not update heartbeat file: %s", exc)


def summarize(run: RunResult) -> str:
    lines = [
        f"Sync finished in {run.duration_s:.1f}s "
        f"({len(run.playlists)} playlist(s))",
    ]
    for result in run.playlists:
        if result.error:
            lines.append(f"  FAILED  {result.pair.describe()}: {result.error}")
        elif result.skipped_reason:
            lines.append(f"  SKIPPED {result.pair.describe()}: {result.skipped_reason}")
        elif result.changed:
            lines.append(
                f"  UPDATED {result.spotify_name!r}: {result.matched_tracks} tracks "
                f"(+{result.added}/-{result.removed}, "
                f"{len(result.unmatched)} unmatched)"
            )
        else:
            lines.append(
                f"  OK      {result.spotify_name!r}: {result.matched_tracks} tracks "
                f"already in sync ({len(result.unmatched)} unmatched)"
            )
    return "\n".join(lines)
