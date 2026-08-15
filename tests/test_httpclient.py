from __future__ import annotations

from typing import Any

import pytest
import requests

from musicsync.errors import ApiError, AuthError, ForbiddenError
from musicsync.httpclient import HttpClient


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        payload: Any = None,
        text: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or ("{}" if payload is not None else "")
        self.headers = headers or {}
        self.content = self.text.encode()

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str]] = []
        self.headers: dict[str, str] = {}

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        pass


@pytest.fixture
def slept() -> list[float]:
    return []


def make_client(responses: list[Any], slept: list[float], **kwargs: Any) -> HttpClient:
    client = HttpClient(sleep=slept.append, **kwargs)
    client.session = FakeSession(responses)  # type: ignore[assignment]
    return client


def test_successful_request_returns_json(slept: list[float]) -> None:
    client = make_client([FakeResponse(200, {"ok": True})], slept)
    assert client.get("https://example.test/x") == {"ok": True}
    assert slept == []


def test_empty_body_returns_none(slept: list[float]) -> None:
    client = make_client([FakeResponse(204)], slept)
    assert client.get("https://example.test/x") is None


def test_retries_on_500_then_succeeds(slept: list[float]) -> None:
    client = make_client(
        [FakeResponse(500, text="boom"), FakeResponse(200, {"ok": True})], slept
    )
    assert client.get("https://example.test/x") == {"ok": True}
    assert len(slept) == 1


def test_honours_retry_after_on_429(slept: list[float]) -> None:
    client = make_client(
        [
            FakeResponse(429, text="slow down", headers={"Retry-After": "3"}),
            FakeResponse(200, {"ok": True}),
        ],
        slept,
    )
    assert client.get("https://example.test/x") == {"ok": True}
    # Retry-After plus a small margin.
    assert slept == [4.0]


def test_absurd_retry_after_fails_fast(slept: list[float]) -> None:
    client = make_client(
        [FakeResponse(429, text="daily cap", headers={"Retry-After": "86400"})], slept
    )
    with pytest.raises(ApiError, match="exceeds"):
        client.get("https://example.test/x")
    assert slept == []


def test_401_raises_auth_error_without_retrying(slept: list[float]) -> None:
    client = make_client([FakeResponse(401, text="bad token")], slept)
    with pytest.raises(AuthError, match="401"):
        client.get("https://example.test/x")
    assert slept == []


def test_403_is_forbidden_not_auth(slept: list[float]) -> None:
    # A 403 means the token is fine but the request is not allowed, so callers
    # must be able to tell it apart -- refreshing the token cannot help.
    client = make_client([FakeResponse(403, text="premium required")], slept)
    with pytest.raises(ForbiddenError, match="403"):
        client.get("https://example.test/x")
    assert slept == []


def test_forbidden_is_not_caught_as_auth_error(slept: list[float]) -> None:
    client = make_client([FakeResponse(403, text="nope")], slept)
    try:
        client.get("https://example.test/x")
    except AuthError:  # pragma: no cover - would mean the split is broken
        raise AssertionError("403 must not be an AuthError")
    except ForbiddenError:
        pass


def test_404_is_not_retried(slept: list[float]) -> None:
    client = make_client([FakeResponse(404, text="nope")], slept)
    with pytest.raises(ApiError, match="404"):
        client.get("https://example.test/x")
    assert slept == []


def test_gives_up_after_max_retries(slept: list[float]) -> None:
    responses = [FakeResponse(503, text="down") for _ in range(4)]
    client = make_client(responses, slept, max_retries=3)
    with pytest.raises(ApiError, match="after 4 attempts"):
        client.get("https://example.test/x")
    assert len(slept) == 3


def test_connection_errors_are_retried(slept: list[float]) -> None:
    client = make_client(
        [requests.ConnectionError("no route"), FakeResponse(200, {"ok": True})], slept
    )
    assert client.get("https://example.test/x") == {"ok": True}
    assert len(slept) == 1


def test_invalid_json_is_reported(slept: list[float]) -> None:
    client = make_client([FakeResponse(200, None, text="<html>oops</html>")], slept)
    with pytest.raises(ApiError, match="invalid JSON"):
        client.get("https://example.test/x")


def test_tokens_in_query_strings_stay_out_of_errors(slept: list[float]) -> None:
    client = make_client([FakeResponse(404, text="nope")], slept)
    url = "https://api.deezer.com/user/me?access_token=supersecret"
    with pytest.raises(ApiError) as excinfo:
        client.get(url)
    assert "supersecret" not in str(excinfo.value)


def test_tokens_inside_connection_errors_are_scrubbed(slept: list[float]) -> None:
    # requests embeds the full URL -- query string included -- in its own
    # exception text, which is what actually reaches the container logs.
    boom = requests.ConnectionError(
        "HTTPSConnectionPool(host='api.deezer.com', port=443): Max retries "
        "exceeded with url: /playlist/1?access_token=supersecret (Caused by ...)"
    )
    client = make_client([boom], slept, max_retries=0)
    with pytest.raises(ApiError) as excinfo:
        client.get("https://api.deezer.com/playlist/1?access_token=supersecret")
    assert "supersecret" not in str(excinfo.value)
    assert "<redacted>" in str(excinfo.value)


def test_tokens_echoed_in_response_bodies_are_scrubbed(slept: list[float]) -> None:
    client = make_client(
        [FakeResponse(400, text='{"error":"bad code=supersecret"}')], slept
    )
    with pytest.raises(ApiError) as excinfo:
        client.get("https://example.test/x")
    assert "supersecret" not in str(excinfo.value)


class TestScrub:
    @pytest.mark.parametrize(
        "raw",
        [
            "url?access_token=abc123",
            "url?refresh_token=abc123",
            "url?client_secret=abc123&x=1",
            "url?secret=abc123",
            "url?code=abc123",
            "Authorization: Bearer abc123",
        ],
    )
    def test_secrets_are_removed(self, raw: str) -> None:
        from musicsync.httpclient import scrub

        assert "abc123" not in scrub(raw)

    def test_surrounding_text_is_preserved(self) -> None:
        from musicsync.httpclient import scrub

        assert scrub("get /playlist/9?access_token=xyz failed") == (
            "get /playlist/9?access_token=<redacted> failed"
        )

    def test_harmless_text_is_untouched(self) -> None:
        from musicsync.httpclient import scrub

        assert scrub("connection refused") == "connection refused"
