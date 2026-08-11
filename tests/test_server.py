from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
import requests
from conftest import make_config

from musicsync import server as server_module
from musicsync.config import PlaylistPair
from musicsync.errors import ApiError
from musicsync.sync import PlaylistResult, RunResult, UnmatchedTrack

TOKEN = "test-token-value"


def fake_run(*, failed: bool = False, unmatched: int = 0) -> RunResult:
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = PlaylistResult(
        pair=PlaylistPair(deezer_id="100", spotify_id="sp1"),
        deezer_title="Rock",
        spotify_name="Rock Mirror",
        spotify_id="sp1",
        source_tracks=3,
        matched_tracks=3,
        changed=True,
        added=3,
        error="boom" if failed else None,
        unmatched=[
            UnmatchedTrack(deezer_id=str(i), label=f"Band - Track {i}", isrc=None)
            for i in range(unmatched)
        ],
    )
    return RunResult(
        started_at=started,
        finished_at=started + timedelta(seconds=5),
        playlists=[result],
    )


@pytest.fixture
def live_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """A real HTTP server on an ephemeral port, with the sync stubbed out."""
    calls: list[int] = []

    def stub(_config: Any) -> RunResult:
        calls.append(1)
        return fake_run()

    monkeypatch.setattr(server_module, "execute_run", stub)

    config = make_config(
        [PlaylistPair(deezer_id="100", spotify_id="sp1")],
        tmp_path,
        mode="server",
        api_token=TOKEN,
        http_bind="127.0.0.1",
        http_port=0,  # let the OS pick a free port
    )
    httpd = server_module.create_server(config)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    class Handle:
        base = f"http://127.0.0.1:{port}"
        runs = calls

        def get(self, path: str, **kwargs: Any) -> requests.Response:
            return requests.get(f"{self.base}{path}", timeout=10, **kwargs)

        def post(self, path: str, **kwargs: Any) -> requests.Response:
            return requests.post(f"{self.base}{path}", timeout=10, **kwargs)

    try:
        yield Handle()
    finally:
        httpd.shutdown()
        httpd.server_close()


AUTH = {"X-Auth-Token": TOKEN}


class TestAuth:
    def test_health_needs_no_token(self, live_server: Any) -> None:
        response = live_server.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_sync_without_token_is_rejected(self, live_server: Any) -> None:
        response = live_server.post("/sync")
        assert response.status_code == 401
        assert live_server.runs == []

    def test_sync_with_wrong_token_is_rejected(self, live_server: Any) -> None:
        response = live_server.post("/sync", headers={"X-Auth-Token": "nope"})
        assert response.status_code == 401
        assert live_server.runs == []

    def test_status_without_token_is_rejected(self, live_server: Any) -> None:
        assert live_server.get("/status").status_code == 401

    def test_bearer_header_is_accepted(self, live_server: Any) -> None:
        response = live_server.post(
            "/sync", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert response.status_code == 200


class TestSync:
    def test_sync_returns_a_summary(self, live_server: Any) -> None:
        response = live_server.post("/sync", headers=AUTH)
        assert response.status_code == 200

        payload = response.json()
        assert payload["totals"] == {
            "playlists": 1, "failed": 0, "skipped": 0,
            "changed": 1, "unmatched_tracks": 0,
        }
        assert payload["playlists"][0]["spotify_name"] == "Rock Mirror"
        assert payload["playlists"][0]["added"] == 3
        assert live_server.runs == [1]

    def test_status_is_empty_before_the_first_run(self, live_server: Any) -> None:
        payload = live_server.get("/status", headers=AUTH).json()
        assert payload["running"] is False
        assert payload["last_run"] is None

    def test_status_reports_the_last_run(self, live_server: Any) -> None:
        live_server.post("/sync", headers=AUTH)
        payload = live_server.get("/status", headers=AUTH).json()
        assert payload["running"] is False
        assert payload["last_run"]["totals"]["playlists"] == 1

    def test_async_trigger_returns_immediately(self, live_server: Any) -> None:
        response = live_server.post("/sync?wait=false", headers=AUTH)
        assert response.status_code == 202
        assert response.json()["status"] == "started"

        deadline = threading.Event()
        for _ in range(50):
            if live_server.get("/status", headers=AUTH).json()["last_run"]:
                break
            deadline.wait(0.1)
        assert live_server.get("/status", headers=AUTH).json()["last_run"] is not None

    def test_unknown_endpoints_are_404(self, live_server: Any) -> None:
        assert live_server.get("/nope").status_code == 404
        assert live_server.post("/nope", headers=AUTH).status_code == 404

    def test_trailing_slash_is_tolerated(self, live_server: Any) -> None:
        assert live_server.post("/sync/", headers=AUTH).status_code == 200


class TestFailureHandling:
    def test_api_errors_become_500(
        self, live_server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_config: Any) -> RunResult:
            raise ApiError("deezer is down")

        monkeypatch.setattr(server_module, "execute_run", boom)
        response = live_server.post("/sync", headers=AUTH)
        assert response.status_code == 500
        assert "deezer is down" in response.json()["error"]

    def test_a_failed_playlist_still_returns_200_with_a_count(
        self, live_server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Workflows branch on totals.failed rather than on the HTTP status,
        # so a partially failed run must not look like a transport error.
        monkeypatch.setattr(
            server_module, "execute_run", lambda _config: fake_run(failed=True)
        )
        response = live_server.post("/sync", headers=AUTH)
        assert response.status_code == 200
        assert response.json()["totals"]["failed"] == 1

    def test_concurrent_runs_are_refused(
        self, live_server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        release = threading.Event()

        def slow(_config: Any) -> RunResult:
            release.wait(10)
            return fake_run()

        monkeypatch.setattr(server_module, "execute_run", slow)
        live_server.post("/sync?wait=false", headers=AUTH)

        for _ in range(50):
            if live_server.get("/status", headers=AUTH).json()["running"]:
                break
            threading.Event().wait(0.1)

        response = live_server.post("/sync", headers=AUTH)
        release.set()
        assert response.status_code == 409
        assert "already running" in response.json()["error"]


class TestServerConfig:
    def test_server_mode_demands_a_token(self, tmp_path: Path) -> None:
        config = make_config(
            [PlaylistPair(deezer_id="100")], tmp_path, mode="server", api_token=None
        )
        with pytest.raises(Exception, match="API_TOKEN"):
            server_module.create_server(config)
