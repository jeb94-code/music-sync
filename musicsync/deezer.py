"""Deezer API client (the master side of the mirror).

Deezer reports errors inside a 200 response body rather than via status codes,
so every response is inspected. The public API is also rate limited to roughly
50 requests per 5 seconds, which the client paces itself against.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Iterator

from .errors import ApiError, AuthError
from .httpclient import HttpClient

log = logging.getLogger(__name__)

API_BASE = "https://api.deezer.com"
PAGE_SIZE = 100

# Deezer error codes worth naming; anything else is reported verbatim.
_QUOTA_CODES = {4}
_SERVICE_BUSY_CODES = {700}
_AUTH_CODES = {300, 200}
# 800 "no data" covers both a wrong ID and a playlist we may not read, since
# an unauthenticated caller cannot tell a private playlist from a missing one.
_NO_DATA_CODES = {800}

# 50 requests / 5 s is the documented ceiling; stay comfortably under it.
_MIN_REQUEST_INTERVAL_S = 0.12


@dataclass(frozen=True)
class DeezerTrack:
    id: str
    title: str
    artist: str
    album: str
    duration: int
    isrc: str | None = None

    @property
    def label(self) -> str:
        return f"{self.artist} - {self.title}"


class DeezerClient:
    def __init__(
        self,
        http: HttpClient,
        access_token: str | None = None,
        *,
        min_interval_s: float = _MIN_REQUEST_INTERVAL_S,
    ) -> None:
        self.http = http
        self.access_token = access_token
        self.min_interval_s = min_interval_s
        self._last_request_at = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval_s:
            self.http.sleep(self.min_interval_s - elapsed)
        self._last_request_at = time.monotonic()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        params = dict(params or {})
        if self.access_token:
            params["access_token"] = self.access_token

        for attempt in range(4):
            self._throttle()
            payload = self.http.get(f"{API_BASE}{path}", params=params)
            error = payload.get("error") if isinstance(payload, dict) else None
            if not error:
                return payload

            # Deezer sometimes returns `"error": []` for "no error".
            if isinstance(error, list) and not error:
                return payload

            code = error.get("code") if isinstance(error, dict) else None
            message = (
                error.get("message") if isinstance(error, dict) else str(error)
            ) or "unknown error"

            if code in _AUTH_CODES:
                raise AuthError(
                    f"Deezer rejected the request for {path}: {message} (code {code}). "
                    "Check DEEZER_ACCESS_TOKEN -- re-run the auth helper to mint a new one."
                )
            if code in _QUOTA_CODES or code in _SERVICE_BUSY_CODES:
                wait = 5.0 * (attempt + 1)
                log.warning(
                    "Deezer throttled %s (%s); waiting %.0fs", path, message, wait
                )
                self.http.sleep(wait)
                continue
            if code in _NO_DATA_CODES:
                hint = (
                    "Check the ID, and make sure the playlist is public"
                    if not self.access_token
                    else "Check the ID, and that this playlist belongs to the "
                         "account the token was issued for"
                )
                raise ApiError(
                    f"Deezer has no readable data for {path}. {hint}. "
                    "Deezer reports a private playlist and a missing one the "
                    "same way, so both look like this."
                )
            raise ApiError(f"Deezer error on {path}: {message} (code {code})")

        raise ApiError(f"Deezer kept throttling {path}; giving up for this run")

    # -- Playlists ----------------------------------------------------------

    def get_playlist(self, playlist_id: str) -> dict[str, Any]:
        payload = self._get(f"/playlist/{playlist_id}")
        if not isinstance(payload, dict) or "id" not in payload:
            raise ApiError(f"Unexpected Deezer playlist payload for {playlist_id}")
        return payload

    def iter_playlist_tracks(self, playlist_id: str) -> Iterator[dict[str, Any]]:
        """Yield raw track objects, following Deezer's index/limit pagination."""
        index = 0
        while True:
            payload = self._get(
                f"/playlist/{playlist_id}/tracks",
                params={"index": index, "limit": PAGE_SIZE},
            )
            data = payload.get("data") if isinstance(payload, dict) else None
            if not data:
                return
            for item in data:
                yield item
            if len(data) < PAGE_SIZE:
                return
            index += len(data)

    def get_track(self, track_id: str) -> dict[str, Any]:
        """Fetch full track detail. Only this endpoint exposes the ISRC."""
        return self._get(f"/track/{track_id}")


def parse_track(raw: dict[str, Any], isrc: str | None = None) -> DeezerTrack | None:
    """Convert a raw Deezer track object into our own shape.

    Returns None for entries we cannot mirror (missing ID or title).
    """
    track_id = raw.get("id")
    title = (raw.get("title") or "").strip()
    if track_id is None or not title:
        return None

    artist_obj = raw.get("artist") or {}
    artist = (artist_obj.get("name") or "").strip()
    album_obj = raw.get("album") or {}
    album = (album_obj.get("title") or "").strip()

    try:
        duration = int(raw.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0

    return DeezerTrack(
        id=str(track_id),
        title=title,
        artist=artist,
        album=album,
        duration=duration,
        isrc=(isrc or raw.get("isrc") or None),
    )


def all_artist_names(raw: dict[str, Any]) -> list[str]:
    """Every credited artist, main artist first.

    Deezer only fills `contributors` on the single-track endpoint, so callers
    fall back to the main artist when it is absent.
    """
    names: list[str] = []
    main = (raw.get("artist") or {}).get("name")
    if main:
        names.append(main.strip())
    for contributor in raw.get("contributors") or []:
        name = (contributor or {}).get("name")
        if name and name.strip() not in names:
            names.append(name.strip())
    return names
