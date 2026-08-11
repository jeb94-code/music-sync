"""Configuration, sourced entirely from environment variables.

This repository is public, so nothing secret is ever read from a file that is
under version control. Credentials come from the container environment only
(Portainer stack env vars, an .env file kept outside the repo, or Docker
secrets exported into the environment).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .errors import ConfigError

# Spotify object IDs are 22-character base62 strings.
_SPOTIFY_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
_SPOTIFY_URI_RE = re.compile(r"^spotify:playlist:([A-Za-z0-9]{22})$")
_SPOTIFY_URL_RE = re.compile(
    r"^https?://open\.spotify\.com/(?:[a-z-]+/)?playlist/([A-Za-z0-9]{22})"
)
_DEEZER_URL_RE = re.compile(
    r"^https?://(?:www\.)?deezer\.com/(?:[a-z]{2}/)?playlist/(\d+)"
)
_DEEZER_ID_RE = re.compile(r"^\d+$")

# Entries may be separated by newlines or semicolons. Commas are deliberately
# not separators so that playlist names containing commas stay intact.
_ENTRY_SPLIT_RE = re.compile(r"[\n;]+")

_ARROW_SEPARATORS = ("->", "=>")
_INLINE_SEPARATORS = ":="


def env_str(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    """Read a string env var, treating blank/whitespace-only as unset."""
    raw = os.environ.get(name)
    value = raw.strip() if raw is not None else None
    if not value:
        if required:
            raise ConfigError(f"Required environment variable {name} is not set")
        return default
    return value


def env_bool(name: str, default: bool = False) -> bool:
    raw = env_str(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in {"1", "true", "yes", "y", "on"}:
        return True
    if lowered in {"0", "false", "no", "n", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean value, got {raw!r}")


def env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = env_str(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def env_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    raw = env_str(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be <= {maximum}, got {value}")
    return value


@dataclass(frozen=True)
class PlaylistPair:
    """One Deezer playlist and the Spotify playlist it is mirrored onto."""

    deezer_id: str
    spotify_id: str | None = None
    spotify_name: str | None = None

    def describe(self) -> str:
        if self.spotify_id:
            target = f"Spotify playlist {self.spotify_id}"
        elif self.spotify_name:
            target = f"Spotify playlist named {self.spotify_name!r}"
        else:
            target = "Spotify playlist named after the Deezer playlist"
        return f"Deezer {self.deezer_id} -> {target}"


def parse_deezer_id(token: str) -> str:
    """Accept a bare numeric ID or a deezer.com playlist URL."""
    token = token.strip()
    match = _DEEZER_URL_RE.match(token)
    if match:
        return match.group(1)
    if _DEEZER_ID_RE.match(token):
        return token
    raise ConfigError(
        f"{token!r} is not a Deezer playlist ID or URL. Expected digits such as "
        "'908622995' or a link like 'https://www.deezer.com/de/playlist/908622995'."
    )


def _split_entry(entry: str) -> tuple[str, str]:
    """Split one PLAYLISTS entry into its Deezer side and Spotify side.

    An explicit '->' or '=>' always wins. Without one, the Deezer side ends at
    the first ':', '=' or whitespace -- skipping past a leading URL scheme so
    that 'https://...' is not cut at its own colon.
    """
    for separator in _ARROW_SEPARATORS:
        index = entry.find(separator)
        if index != -1:
            return entry[:index].strip(), entry[index + len(separator):].strip()

    start = 0
    lowered = entry.lower()
    if lowered.startswith(("http://", "https://")):
        start = entry.index("://") + 3

    for index in range(start, len(entry)):
        char = entry[index]
        if char in _INLINE_SEPARATORS or char.isspace():
            return entry[:index].strip(), entry[index + 1:].strip()

    return entry.strip(), ""


def _parse_spotify_target(token: str) -> tuple[str | None, str | None]:
    """Return (spotify_id, spotify_name) for the right-hand side of a mapping."""
    token = token.strip()
    if not token:
        return None, None

    # A quoted target is always a name, even if it looks like an ID.
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        name = token[1:-1].strip()
        if not name:
            raise ConfigError("Spotify playlist name in PLAYLISTS is empty")
        return None, name

    if token.lower().startswith("name:"):
        name = token[len("name:"):].strip()
        if not name:
            raise ConfigError("Spotify playlist name in PLAYLISTS is empty")
        return None, name

    for pattern in (_SPOTIFY_URI_RE, _SPOTIFY_URL_RE):
        match = pattern.match(token)
        if match:
            return match.group(1), None

    if _SPOTIFY_ID_RE.match(token):
        return token, None

    # Anything else is treated as a playlist name to find or create.
    return None, token


def parse_playlists(raw: str) -> list[PlaylistPair]:
    """Parse the PLAYLISTS environment variable.

    Supported forms, one per line (or separated by ';'):

        908622995
        908622995 -> 37i9dQZF1DXcBWIGoYBM5M
        908622995 -> https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M
        908622995 -> "Rock Mirror"
        https://www.deezer.com/de/playlist/908622995 -> name:Rock Mirror

    Lines starting with '#' are comments.
    """
    pairs: list[PlaylistPair] = []
    seen_deezer: set[str] = set()
    seen_spotify_id: dict[str, str] = {}
    seen_spotify_name: dict[str, str] = {}

    for chunk in _ENTRY_SPLIT_RE.split(raw or ""):
        entry = chunk.strip()
        if not entry or entry.startswith("#"):
            continue

        left, right = _split_entry(entry)
        deezer_id = parse_deezer_id(left)
        spotify_id, spotify_name = _parse_spotify_target(right)

        if deezer_id in seen_deezer:
            raise ConfigError(
                f"Deezer playlist {deezer_id} appears more than once in PLAYLISTS"
            )
        if spotify_id and spotify_id in seen_spotify_id:
            raise ConfigError(
                f"Spotify playlist {spotify_id} is targeted by more than one Deezer "
                f"playlist ({seen_spotify_id[spotify_id]} and {deezer_id}); the second "
                "sync would overwrite the first"
            )
        if spotify_name and spotify_name.casefold() in seen_spotify_name:
            raise ConfigError(
                f"Spotify playlist name {spotify_name!r} is targeted by more than one "
                f"Deezer playlist ({seen_spotify_name[spotify_name.casefold()]} and "
                f"{deezer_id}); the second sync would overwrite the first"
            )

        seen_deezer.add(deezer_id)
        if spotify_id:
            seen_spotify_id[spotify_id] = deezer_id
        if spotify_name:
            seen_spotify_name[spotify_name.casefold()] = deezer_id

        pairs.append(
            PlaylistPair(
                deezer_id=deezer_id,
                spotify_id=spotify_id,
                spotify_name=spotify_name,
            )
        )

    if not pairs:
        raise ConfigError(
            "PLAYLISTS is empty. Set it to at least one Deezer playlist ID, "
            'for example: PLAYLISTS=908622995 -> "Rock Mirror"'
        )
    return pairs


@dataclass(frozen=True)
class Config:
    """Everything a sync run needs, resolved from the environment."""

    spotify_client_id: str
    spotify_client_secret: str
    spotify_refresh_token: str
    deezer_access_token: str | None
    playlists: list[PlaylistPair]

    data_dir: str = "/data"
    # "schedule" runs the built-in loop, "server" waits for HTTP triggers
    # (n8n and friends), "once" performs a single pass and exits.
    mode: str = "schedule"
    interval_minutes: int = 60
    dry_run: bool = False
    log_level: str = "INFO"

    # Server mode
    http_bind: str = "0.0.0.0"
    http_port: int = 8477
    api_token: str | None = None

    # Matching behaviour
    match_threshold: float = 0.72
    duration_tolerance_s: int = 8
    search_limit: int = 8

    # Playlist creation
    create_missing: bool = True
    public_playlists: bool = False
    playlist_description: str = (
        "Mirrored from Deezer by music-sync. Manual edits are overwritten."
    )

    # Safety valves
    allow_empty_mirror: bool = False
    allow_partial_source: bool = False
    max_unmatched_ratio: float = 1.0

    http_timeout_s: int = 30
    http_max_retries: int = 5

    @property
    def cache_path(self) -> str:
        return os.path.join(self.data_dir, "match-cache.sqlite3")

    @property
    def heartbeat_path(self) -> str:
        return os.path.join(self.data_dir, "heartbeat")

    @property
    def report_path(self) -> str:
        return os.path.join(self.data_dir, "unmatched.log")


VALID_MODES = ("schedule", "server", "once")


def resolve_mode() -> str:
    """Pick the run mode, honouring the older RUN_ONCE flag.

    RUN_ONCE predates MODE and is still documented for one-shot containers, so
    it keeps working; an explicit MODE always wins over it.
    """
    mode = env_str("MODE")
    if mode:
        normalized = mode.lower()
        if normalized not in VALID_MODES:
            raise ConfigError(
                f"MODE must be one of {', '.join(VALID_MODES)}, got {mode!r}"
            )
        return normalized
    return "once" if env_bool("RUN_ONCE", False) else "schedule"


def load_config() -> Config:
    """Build a Config from the process environment, validating as we go."""
    playlists_raw = env_str("PLAYLISTS", required=True)
    assert playlists_raw is not None  # required=True guarantees this

    config = Config(
        spotify_client_id=env_str("SPOTIFY_CLIENT_ID", required=True),  # type: ignore[arg-type]
        spotify_client_secret=env_str("SPOTIFY_CLIENT_SECRET", required=True),  # type: ignore[arg-type]
        spotify_refresh_token=env_str("SPOTIFY_REFRESH_TOKEN", required=True),  # type: ignore[arg-type]
        deezer_access_token=env_str("DEEZER_ACCESS_TOKEN"),
        playlists=parse_playlists(playlists_raw),
        data_dir=env_str("DATA_DIR", "/data"),  # type: ignore[arg-type]
        mode=resolve_mode(),
        interval_minutes=env_int("SYNC_INTERVAL_MINUTES", 60, minimum=1),
        http_bind=env_str("HTTP_BIND", "0.0.0.0"),  # type: ignore[arg-type]
        http_port=env_int("HTTP_PORT", 8477, minimum=1),
        api_token=env_str("API_TOKEN"),
        dry_run=env_bool("DRY_RUN", False),
        log_level=(env_str("LOG_LEVEL", "INFO") or "INFO").upper(),
        match_threshold=env_float("MATCH_THRESHOLD", 0.72, minimum=0.0, maximum=1.0),
        duration_tolerance_s=env_int("DURATION_TOLERANCE_SECONDS", 8, minimum=0),
        search_limit=env_int("SEARCH_LIMIT", 8, minimum=1),
        create_missing=env_bool("CREATE_MISSING_PLAYLISTS", True),
        public_playlists=env_bool("SPOTIFY_PLAYLISTS_PUBLIC", False),
        playlist_description=env_str(
            "SPOTIFY_PLAYLIST_DESCRIPTION",
            "Mirrored from Deezer by music-sync. Manual edits are overwritten.",
        ),  # type: ignore[arg-type]
        allow_empty_mirror=env_bool("ALLOW_EMPTY_MIRROR", False),
        allow_partial_source=env_bool("ALLOW_PARTIAL_SOURCE", False),
        max_unmatched_ratio=env_float(
            "MAX_UNMATCHED_RATIO", 1.0, minimum=0.0, maximum=1.0
        ),
        http_timeout_s=env_int("HTTP_TIMEOUT_SECONDS", 30, minimum=1),
        http_max_retries=env_int("HTTP_MAX_RETRIES", 5, minimum=0),
    )

    if config.search_limit > 50:
        raise ConfigError("SEARCH_LIMIT must be 50 or less (Spotify API maximum)")
    if config.mode == "server" and not config.api_token:
        raise ConfigError(
            "MODE=server requires API_TOKEN. A request to /sync rewrites your "
            "Spotify playlists, so the endpoint is never left unauthenticated. "
            "Generate one with: openssl rand -hex 32"
        )
    return config


def redact(value: str | None, keep: int = 4) -> str:
    """Render a secret for logs without disclosing it."""
    if not value:
        return "<unset>"
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * (len(value) - keep)}"


def format_playlists(pairs: list[PlaylistPair]) -> str:
    return "\n".join(f"  - {pair.describe()}" for pair in pairs)
