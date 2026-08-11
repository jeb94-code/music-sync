from __future__ import annotations

import pytest

from musicsync.config import (
    env_bool,
    env_float,
    env_int,
    load_config,
    parse_deezer_id,
    parse_playlists,
    redact,
)
from musicsync.errors import ConfigError


class TestParsePlaylists:
    def test_bare_id_targets_playlist_named_after_deezer(self) -> None:
        pairs = parse_playlists("908622995")
        assert len(pairs) == 1
        assert pairs[0].deezer_id == "908622995"
        assert pairs[0].spotify_id is None
        assert pairs[0].spotify_name is None

    def test_arrow_to_spotify_id(self) -> None:
        pairs = parse_playlists("908622995 -> 37i9dQZF1DXcBWIGoYBM5M")
        assert pairs[0].spotify_id == "37i9dQZF1DXcBWIGoYBM5M"
        assert pairs[0].spotify_name is None

    def test_arrow_to_quoted_name(self) -> None:
        pairs = parse_playlists('908622995 -> "Rock Mirror"')
        assert pairs[0].spotify_name == "Rock Mirror"
        assert pairs[0].spotify_id is None

    def test_name_with_comma_survives(self) -> None:
        pairs = parse_playlists('908622995 -> "Rock, Pop & More"')
        assert pairs[0].spotify_name == "Rock, Pop & More"

    def test_unquoted_multiword_name(self) -> None:
        pairs = parse_playlists("908622995 -> Rock Mirror")
        assert pairs[0].spotify_name == "Rock Mirror"

    def test_name_prefix_forces_name_even_when_id_shaped(self) -> None:
        pairs = parse_playlists("908622995 -> name:37i9dQZF1DXcBWIGoYBM5M")
        assert pairs[0].spotify_name == "37i9dQZF1DXcBWIGoYBM5M"
        assert pairs[0].spotify_id is None

    def test_spotify_url_and_uri(self) -> None:
        pairs = parse_playlists(
            "1 -> https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M\n"
            "2 -> spotify:playlist:37i9dQZF1DXcBWIGoYBM6N\n"
            "3 -> https://open.spotify.com/intl-de/playlist/37i9dQZF1DXcBWIGoYBM7O"
        )
        assert [p.spotify_id for p in pairs] == [
            "37i9dQZF1DXcBWIGoYBM5M",
            "37i9dQZF1DXcBWIGoYBM6N",
            "37i9dQZF1DXcBWIGoYBM7O",
        ]

    def test_deezer_url_is_not_split_at_its_own_colon(self) -> None:
        pairs = parse_playlists(
            'https://www.deezer.com/de/playlist/908622995 -> "Rock Mirror"'
        )
        assert pairs[0].deezer_id == "908622995"
        assert pairs[0].spotify_name == "Rock Mirror"

    def test_bare_deezer_url_without_target(self) -> None:
        pairs = parse_playlists("https://www.deezer.com/playlist/908622995")
        assert pairs[0].deezer_id == "908622995"
        assert pairs[0].spotify_name is None

    def test_colon_and_equals_separators(self) -> None:
        pairs = parse_playlists("1:37i9dQZF1DXcBWIGoYBM5M; 2=Chill Mirror")
        assert pairs[0].spotify_id == "37i9dQZF1DXcBWIGoYBM5M"
        assert pairs[1].spotify_name == "Chill Mirror"

    def test_multiple_entries_across_lines_and_semicolons(self) -> None:
        pairs = parse_playlists(
            """
            # first the rock one
            908622995 -> "Rock Mirror"; 123456789 -> "Chill Mirror"

            555 -> 37i9dQZF1DXcBWIGoYBM5M
            """
        )
        assert [p.deezer_id for p in pairs] == ["908622995", "123456789", "555"]

    def test_comments_and_blank_lines_ignored(self) -> None:
        pairs = parse_playlists("# nothing here\n\n908622995\n")
        assert len(pairs) == 1

    def test_empty_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="PLAYLISTS is empty"):
            parse_playlists("   \n # only a comment \n ")

    def test_duplicate_deezer_source_rejected(self) -> None:
        with pytest.raises(ConfigError, match="more than once"):
            parse_playlists("908622995 -> A; 908622995 -> B")

    def test_two_sources_into_one_spotify_id_rejected(self) -> None:
        with pytest.raises(ConfigError, match="more than one Deezer"):
            parse_playlists(
                "1 -> 37i9dQZF1DXcBWIGoYBM5M; 2 -> 37i9dQZF1DXcBWIGoYBM5M"
            )

    def test_two_sources_into_one_spotify_name_rejected(self) -> None:
        with pytest.raises(ConfigError, match="more than one Deezer"):
            parse_playlists('1 -> "Mirror"; 2 -> "mirror"')

    def test_invalid_deezer_id_rejected(self) -> None:
        with pytest.raises(ConfigError, match="not a Deezer playlist"):
            parse_playlists("not-an-id -> Something")


class TestParseDeezerId:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("908622995", "908622995"),
            ("https://www.deezer.com/playlist/908622995", "908622995"),
            ("https://www.deezer.com/de/playlist/908622995", "908622995"),
            ("https://deezer.com/en/playlist/12", "12"),
        ],
    )
    def test_accepted_forms(self, value: str, expected: str) -> None:
        assert parse_deezer_id(value) == expected

    def test_spotify_url_rejected(self) -> None:
        with pytest.raises(ConfigError):
            parse_deezer_id("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")


class TestEnvHelpers:
    def test_bool_variants(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for raw, expected in [
            ("true", True), ("TRUE", True), ("1", True), ("yes", True),
            ("on", True), ("false", False), ("0", False), ("no", False),
        ]:
            monkeypatch.setenv("FLAG", raw)
            assert env_bool("FLAG") is expected

    def test_bool_rejects_nonsense(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FLAG", "maybe")
        with pytest.raises(ConfigError):
            env_bool("FLAG")

    def test_blank_value_counts_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FLAG", "   ")
        assert env_bool("FLAG", True) is True
        monkeypatch.setenv("NUM", "")
        assert env_int("NUM", 42) == 42

    def test_int_minimum_enforced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NUM", "0")
        with pytest.raises(ConfigError, match=">= 1"):
            env_int("NUM", 60, minimum=1)

    def test_float_range_enforced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RATIO", "1.5")
        with pytest.raises(ConfigError, match="<= 1"):
            env_float("RATIO", 0.7, minimum=0.0, maximum=1.0)


class TestLoadConfig:
    def _base_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Clear anything the developer's own shell might have exported.
        for key in (
            "DEEZER_ACCESS_TOKEN", "DATA_DIR", "SYNC_INTERVAL_MINUTES", "RUN_ONCE",
            "DRY_RUN", "LOG_LEVEL", "SEARCH_LIMIT", "MATCH_THRESHOLD",
            "MODE", "API_TOKEN", "HTTP_PORT", "HTTP_BIND",
        ):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
        monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "secret")
        monkeypatch.setenv("SPOTIFY_REFRESH_TOKEN", "refresh")
        monkeypatch.setenv("PLAYLISTS", '908622995 -> "Rock Mirror"')

    def test_minimal_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._base_env(monkeypatch)
        monkeypatch.delenv("DEEZER_ACCESS_TOKEN", raising=False)
        config = load_config()
        assert config.spotify_client_id == "cid"
        assert config.deezer_access_token is None
        assert config.interval_minutes == 60
        assert config.dry_run is False
        assert len(config.playlists) == 1

    def test_missing_required_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._base_env(monkeypatch)
        monkeypatch.delenv("SPOTIFY_CLIENT_SECRET")
        with pytest.raises(ConfigError, match="SPOTIFY_CLIENT_SECRET"):
            load_config()

    def test_derived_paths_live_in_data_dir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._base_env(monkeypatch)
        monkeypatch.setenv("DATA_DIR", "/var/lib/music-sync")
        config = load_config()
        assert config.cache_path == "/var/lib/music-sync/match-cache.sqlite3"
        assert config.heartbeat_path == "/var/lib/music-sync/heartbeat"

    def test_search_limit_capped_at_spotify_maximum(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._base_env(monkeypatch)
        monkeypatch.setenv("SEARCH_LIMIT", "51")
        with pytest.raises(ConfigError, match="50 or less"):
            load_config()

    def test_defaults_to_schedule_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._base_env(monkeypatch)
        assert load_config().mode == "schedule"

    def test_run_once_still_selects_once_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._base_env(monkeypatch)
        monkeypatch.setenv("RUN_ONCE", "true")
        assert load_config().mode == "once"

    def test_explicit_mode_wins_over_run_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._base_env(monkeypatch)
        monkeypatch.setenv("RUN_ONCE", "true")
        monkeypatch.setenv("MODE", "schedule")
        assert load_config().mode == "schedule"

    def test_unknown_mode_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._base_env(monkeypatch)
        monkeypatch.setenv("MODE", "webhook")
        with pytest.raises(ConfigError, match="MODE must be one of"):
            load_config()

    def test_server_mode_requires_an_api_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._base_env(monkeypatch)
        monkeypatch.setenv("MODE", "server")
        with pytest.raises(ConfigError, match="API_TOKEN"):
            load_config()

    def test_server_mode_with_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._base_env(monkeypatch)
        monkeypatch.setenv("MODE", "server")
        monkeypatch.setenv("API_TOKEN", "s3cret")
        config = load_config()
        assert config.mode == "server"
        assert config.http_port == 8477


def test_redact_hides_the_tail() -> None:
    assert redact("abcdef1234") == "abcd******"
    assert redact("ab") == "**"
    assert redact(None) == "<unset>"
    assert "secret" not in redact("supersecrettoken")
