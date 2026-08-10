"""Spotify API client (the mirrored side).

Authentication uses the Authorization Code flow. The container only ever holds
a refresh token, which `musicsync.auth` mints once interactively; access tokens
are derived from it at runtime and never leave memory.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Iterator

from .errors import ApiError, AuthError
from .httpclient import HttpClient
from .matching import Candidate

log = logging.getLogger(__name__)

API_BASE = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
AUTHORIZE_URL = "https://accounts.spotify.com/authorize"

PAGE_SIZE = 50
# Spotify accepts at most 100 track URIs per add/replace request.
TRACK_WRITE_CHUNK = 100

SCOPES = (
    "playlist-read-private "
    "playlist-read-collaborative "
    "playlist-modify-private "
    "playlist-modify-public"
)

# Refresh a little before expiry so a long run never trips over the boundary.
_TOKEN_MARGIN_S = 60


@dataclass
class SpotifyPlaylist:
    id: str
    name: str
    owner_id: str
    track_total: int
    snapshot_id: str


class RefreshTokenStore:
    """Remembers a rotated refresh token across container restarts.

    Spotify may hand back a new refresh token when the old one is redeemed.
    Without persistence the container would fall back to the (now dead) value
    in the environment after any restart. The token that came from the
    environment is recorded alongside it, so re-running the auth helper and
    pasting a fresh value into Portainer always wins over the stored one.
    """

    def __init__(self, path: str) -> None:
        self.path = path

    def load(self, env_token: str) -> str:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return env_token

        if payload.get("source") != env_token:
            # The environment was updated; the stored rotation is obsolete.
            return env_token
        stored = payload.get("current")
        if stored and stored != env_token:
            log.info("Using rotated Spotify refresh token from %s", self.path)
            return stored
        return env_token

    def save(self, env_token: str, current_token: str) -> None:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        temp_path = f"{self.path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump({"source": env_token, "current": current_token}, handle)
        os.replace(temp_path, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:  # Best effort; some volume drivers reject chmod.
            pass


class SpotifyClient:
    def __init__(
        self,
        http: HttpClient,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        token_store: RefreshTokenStore | None = None,
    ) -> None:
        self.http = http
        self.client_id = client_id
        self.client_secret = client_secret
        self._env_refresh_token = refresh_token
        self.token_store = token_store
        self.refresh_token = (
            token_store.load(refresh_token) if token_store else refresh_token
        )
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._user: dict[str, Any] | None = None

    # -- Auth ---------------------------------------------------------------

    def _basic_auth(self) -> tuple[str, str]:
        return (self.client_id, self.client_secret)

    def _refresh_access_token(self) -> None:
        payload = self.http.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
            auth=self._basic_auth(),
        )
        token = (payload or {}).get("access_token")
        if not token:
            raise AuthError(
                "Spotify did not return an access token. Check SPOTIFY_CLIENT_ID, "
                "SPOTIFY_CLIENT_SECRET and SPOTIFY_REFRESH_TOKEN."
            )
        self._access_token = token
        self._expires_at = time.time() + float(payload.get("expires_in", 3600))

        rotated = payload.get("refresh_token")
        if rotated and rotated != self.refresh_token:
            log.info("Spotify issued a rotated refresh token; persisting it")
            self.refresh_token = rotated
            if self.token_store:
                self.token_store.save(self._env_refresh_token, rotated)

    def _auth_headers(self) -> dict[str, str]:
        if not self._access_token or time.time() >= self._expires_at - _TOKEN_MARGIN_S:
            self._refresh_access_token()
        return {"Authorization": f"Bearer {self._access_token}"}

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        try:
            return self.http.request(
                method, url, headers=self._auth_headers(), **kwargs
            )
        except AuthError:
            # The token may have been revoked mid-run; mint a new one and retry once.
            log.info("Spotify rejected the access token; refreshing and retrying")
            self._access_token = None
            return self.http.request(
                method, url, headers=self._auth_headers(), **kwargs
            )

    # -- Account ------------------------------------------------------------

    @property
    def user(self) -> dict[str, Any]:
        if self._user is None:
            self._user = self._request("GET", "/me") or {}
        return self._user

    @property
    def user_id(self) -> str:
        user_id = self.user.get("id")
        if not user_id:
            raise ApiError("Spotify /me did not return a user id")
        return str(user_id)

    @property
    def market(self) -> str | None:
        country = self.user.get("country")
        return str(country) if country else None

    # -- Playlists ----------------------------------------------------------

    def get_playlist(self, playlist_id: str) -> SpotifyPlaylist:
        payload = self._request(
            "GET",
            f"/playlists/{playlist_id}",
            params={"fields": "id,name,owner(id),snapshot_id,tracks(total)"},
        ) or {}
        return SpotifyPlaylist(
            id=str(payload.get("id") or playlist_id),
            name=str(payload.get("name") or ""),
            owner_id=str((payload.get("owner") or {}).get("id") or ""),
            track_total=int((payload.get("tracks") or {}).get("total") or 0),
            snapshot_id=str(payload.get("snapshot_id") or ""),
        )

    def iter_my_playlists(self) -> Iterator[dict[str, Any]]:
        url: str | None = "/me/playlists"
        params: dict[str, Any] | None = {"limit": PAGE_SIZE}
        while url:
            payload = self._request("GET", url, params=params) or {}
            for item in payload.get("items") or []:
                if item:
                    yield item
            url = payload.get("next")
            params = None  # `next` already carries the pagination parameters.

    def find_playlist_by_name(self, name: str) -> SpotifyPlaylist | None:
        """Find an editable playlist of ours by name (case-insensitive)."""
        wanted = name.casefold().strip()
        me = self.user_id
        for item in self.iter_my_playlists():
            if (item.get("name") or "").casefold().strip() != wanted:
                continue
            owner_id = str((item.get("owner") or {}).get("id") or "")
            # Followed playlists show up here too, and we cannot write to those.
            if owner_id != me and not item.get("collaborative"):
                log.debug(
                    "Skipping playlist %r owned by %s -- not editable by us",
                    name, owner_id,
                )
                continue
            return SpotifyPlaylist(
                id=str(item.get("id")),
                name=str(item.get("name") or ""),
                owner_id=owner_id,
                track_total=int((item.get("tracks") or {}).get("total") or 0),
                snapshot_id=str(item.get("snapshot_id") or ""),
            )
        return None

    def create_playlist(
        self, name: str, *, description: str = "", public: bool = False
    ) -> SpotifyPlaylist:
        payload = self._request(
            "POST",
            f"/users/{self.user_id}/playlists",
            json={
                "name": name,
                "public": public,
                "description": description[:300],
            },
        ) or {}
        playlist_id = payload.get("id")
        if not playlist_id:
            raise ApiError(f"Spotify did not return an id for new playlist {name!r}")
        return SpotifyPlaylist(
            id=str(playlist_id),
            name=str(payload.get("name") or name),
            owner_id=str((payload.get("owner") or {}).get("id") or self.user_id),
            track_total=0,
            snapshot_id=str(payload.get("snapshot_id") or ""),
        )

    def get_playlist_track_uris(self, playlist_id: str) -> list[str]:
        """Current contents, in order.

        No market is passed: track relinking would rewrite the URIs and make
        them impossible to compare against what we previously added.
        """
        uris: list[str] = []
        url: str | None = f"/playlists/{playlist_id}/tracks"
        params: dict[str, Any] | None = {
            "fields": "items(is_local,track(uri,type)),next",
            "limit": TRACK_WRITE_CHUNK,
        }
        while url:
            payload = self._request("GET", url, params=params) or {}
            for item in payload.get("items") or []:
                track = (item or {}).get("track") or {}
                uri = track.get("uri")
                # Episodes and local files cannot be mirrored from Deezer.
                if not uri or track.get("type") != "track":
                    continue
                if item.get("is_local"):
                    continue
                uris.append(str(uri))
            url = payload.get("next")
            params = None
        return uris

    def replace_playlist_tracks(self, playlist_id: str, uris: list[str]) -> None:
        """Set the playlist contents to exactly `uris`, in order."""
        head, tail = uris[:TRACK_WRITE_CHUNK], uris[TRACK_WRITE_CHUNK:]
        # A PUT replaces everything, which also handles removals and reordering.
        self._request("PUT", f"/playlists/{playlist_id}/tracks", json={"uris": head})
        for start in range(0, len(tail), TRACK_WRITE_CHUNK):
            chunk = tail[start:start + TRACK_WRITE_CHUNK]
            self._request(
                "POST", f"/playlists/{playlist_id}/tracks", json={"uris": chunk}
            )

    # -- Search -------------------------------------------------------------

    def _search_tracks(self, query: str, limit: int) -> list[Candidate]:
        params: dict[str, Any] = {"q": query, "type": "track", "limit": limit}
        if self.market:
            params["market"] = self.market
        payload = self._request("GET", "/search", params=params) or {}
        items = ((payload.get("tracks") or {}).get("items")) or []
        return [candidate for candidate in map(to_candidate, items) if candidate]

    def search_by_isrc(self, isrc: str) -> list[Candidate]:
        return self._search_tracks(f"isrc:{isrc}", limit=5)

    def search_by_metadata(
        self, title: str, artist: str, limit: int
    ) -> list[Candidate]:
        """Try a field-scoped query first, then a looser free-text one."""
        queries = []
        if artist:
            queries.append(f'track:"{_escape(title)}" artist:"{_escape(artist)}"')
        queries.append(f'track:"{_escape(title)}"' if not artist else f"{title} {artist}")

        seen: set[str] = set()
        results: list[Candidate] = []
        for query in queries:
            for candidate in self._search_tracks(query, limit):
                if candidate.uri in seen:
                    continue
                seen.add(candidate.uri)
                results.append(candidate)
            if results:
                # The scoped query is the better signal; only fall through when
                # it returned nothing at all.
                break
        return results


def _escape(text: str) -> str:
    """Neutralise quotes so they cannot break out of a quoted search field."""
    return (text or "").replace('"', " ").strip()


def to_candidate(item: dict[str, Any] | None) -> Candidate | None:
    if not item or item.get("type") != "track":
        return None
    uri = item.get("uri")
    name = item.get("name")
    if not uri or not name:
        return None
    artists = [
        str(artist.get("name"))
        for artist in item.get("artists") or []
        if artist and artist.get("name")
    ]
    try:
        duration_s = int(item.get("duration_ms") or 0) // 1000
    except (TypeError, ValueError):
        duration_s = 0
    return Candidate(
        uri=str(uri),
        name=str(name),
        artists=artists,
        duration_s=duration_s,
        album=str((item.get("album") or {}).get("name") or ""),
        isrc=((item.get("external_ids") or {}).get("isrc") or None),
    )
