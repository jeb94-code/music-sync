from __future__ import annotations

import pytest

from musicsync import auth as auth_module
from musicsync.auth import (
    DEFAULT_AUTH_PORT,
    auth_port,
    deezer_redirect_uri,
    spotify_redirect_uri,
)
from musicsync.errors import AuthError, ConfigError


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("AUTH_PORT", "SPOTIFY_REDIRECT_URI", "DEEZER_REDIRECT_URI"):
        monkeypatch.delenv(key, raising=False)


class TestAuthPort:
    def test_default_avoids_the_usual_home_server_ports(self) -> None:
        # 8080 is taken on most home servers (Nextcloud, dashboards, ...),
        # and 8477 is this project's own server-mode port.
        assert auth_port() == DEFAULT_AUTH_PORT
        assert DEFAULT_AUTH_PORT not in (80, 443, 3000, 5678, 8000, 8080, 8096,
                                         8123, 8477, 9000, 9443)

    def test_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUTH_PORT", "9123")
        assert auth_port() == 9123

    def test_rejects_nonsense(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUTH_PORT", "not-a-port")
        with pytest.raises(ConfigError):
            auth_port()


class TestRedirectUris:
    def test_port_change_reaches_the_redirect_uri(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The classic setup failure: the port is moved but the redirect still
        # points at the old one, and Spotify answers INVALID_CLIENT.
        monkeypatch.setenv("AUTH_PORT", "9123")
        port = auth_port()
        assert spotify_redirect_uri(port) == "http://127.0.0.1:9123/callback"
        assert deezer_redirect_uri(port) == "http://localhost:9123/callback"

    def test_spotify_uses_the_loopback_ip_not_localhost(self) -> None:
        # Spotify rejects http://localhost for http redirects.
        uri = spotify_redirect_uri(DEFAULT_AUTH_PORT)
        assert uri.startswith("http://127.0.0.1:")
        assert "localhost" not in uri

    def test_explicit_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SPOTIFY_REDIRECT_URI", "https://example.test/cb")
        assert spotify_redirect_uri(1234) == "https://example.test/cb"


class TestMain:
    def test_defaults_to_spotify_only(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Deezer no longer hands out apps, so asking for one by default would
        # fail every new setup.
        called: list[str] = []
        monkeypatch.setattr(
            auth_module, "authorize_spotify",
            lambda _http: called.append("spotify") or "refresh-abc",
        )
        monkeypatch.setattr(
            auth_module, "authorize_deezer",
            lambda _http: called.append("deezer") or "deezer-abc",
        )

        assert auth_module.main([]) == 0
        assert called == ["spotify"]
        out = capsys.readouterr().out
        assert "SPOTIFY_REFRESH_TOKEN=refresh-abc" in out
        assert "DEEZER_ACCESS_TOKEN" not in out

    def test_a_completed_consent_survives_a_later_failure(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Losing a token you just approved in the browser, because the *next*
        # provider failed, would mean doing the whole flow again.
        def boom(_http: object) -> str:
            raise AuthError("Deezer app id missing")

        monkeypatch.setattr(
            auth_module, "authorize_spotify", lambda _http: "refresh-abc"
        )
        monkeypatch.setattr(auth_module, "authorize_deezer", boom)

        assert auth_module.main(["both"]) == 1
        assert "SPOTIFY_REFRESH_TOKEN=refresh-abc" in capsys.readouterr().out

    def test_nothing_obtained_prints_no_token_block(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def boom(_http: object) -> str:
            raise AuthError("nope")

        monkeypatch.setattr(auth_module, "authorize_spotify", boom)
        assert auth_module.main(["spotify"]) == 1
        assert "Add these to your Portainer" not in capsys.readouterr().out
