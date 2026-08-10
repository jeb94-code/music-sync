"""A small HTTP wrapper with the retry behaviour both APIs need.

Spotify answers 429 with a Retry-After header; Deezer throttles at roughly
50 requests per 5 seconds and signals it inside a 200 response body. Both are
handled here so the API clients can stay readable.
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any, Callable

import requests

from .errors import ApiError, AuthError

log = logging.getLogger(__name__)

# Deezer authenticates via a query parameter, so tokens end up inside URLs --
# including inside the URLs that `requests` embeds in its exception messages.
# Everything that can reach a log or an exception is scrubbed through here.
_SECRET_PARAM_RE = re.compile(
    r"((?:access_token|refresh_token|client_secret|secret|code|api_key)=)"
    r"[^&\s\"'<>)]+",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)


def scrub(text: str) -> str:
    """Replace credential-looking values in arbitrary text with '<redacted>'."""
    scrubbed = _SECRET_PARAM_RE.sub(r"\1<redacted>", text or "")
    return _BEARER_RE.sub(r"\1<redacted>", scrubbed)


_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_BACKOFF_S = 60.0
# Long Retry-After values usually mean a daily cap; failing fast is better than
# holding the container hostage until the next scheduled run would start anyway.
_MAX_HONOURED_RETRY_AFTER_S = 300.0


class HttpClient:
    """Thin requests.Session wrapper with bounded exponential backoff."""

    def __init__(
        self,
        *,
        timeout: int = 30,
        max_retries: int = 5,
        user_agent: str = "music-sync",
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.sleep = sleep
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _backoff_seconds(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                # Spotify sends whole seconds; be liberal about stray whitespace.
                wait = float(retry_after.strip())
            except ValueError:
                wait = 0.0
            if wait > 0:
                if wait > _MAX_HONOURED_RETRY_AFTER_S:
                    raise ApiError(
                        f"Server asked us to wait {wait:.0f}s, which exceeds the "
                        f"{_MAX_HONOURED_RETRY_AFTER_S:.0f}s cap. Giving up on this run."
                    )
                # A small margin avoids landing exactly on the reset boundary.
                return wait + 1.0
        # 1, 2, 4, 8, ... with jitter so parallel stacks do not sync up.
        return min(2.0 ** attempt, _MAX_BACKOFF_S) + random.uniform(0, 0.5)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        auth: tuple[str, str] | None = None,
    ) -> Any:
        """Perform a request, retrying transient failures.

        Returns the decoded JSON body, or None for an empty response.
        """
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json,
                    data=data,
                    auth=auth,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                wait = self._backoff_seconds(attempt, None)
                log.warning(
                    "%s %s failed (%s); retrying in %.1fs (attempt %d/%d)",
                    method, _safe_url(url), scrub(str(exc)), wait,
                    attempt + 1, self.max_retries,
                )
                self.sleep(wait)
                continue

            if response.status_code in (401, 403):
                raise AuthError(
                    f"{method} {_safe_url(url)} returned {response.status_code}: "
                    f"{_body_excerpt(response)}"
                )

            if response.status_code in _RETRYABLE_STATUS:
                last_error = ApiError(
                    f"{method} {_safe_url(url)} returned {response.status_code}: "
                    f"{_body_excerpt(response)}"
                )
                if attempt >= self.max_retries:
                    break
                wait = self._backoff_seconds(
                    attempt, response.headers.get("Retry-After")
                )
                log.warning(
                    "%s %s returned %d; retrying in %.1fs (attempt %d/%d)",
                    method, _safe_url(url), response.status_code, wait,
                    attempt + 1, self.max_retries,
                )
                self.sleep(wait)
                continue

            if not response.ok:
                raise ApiError(
                    f"{method} {_safe_url(url)} returned {response.status_code}: "
                    f"{_body_excerpt(response)}"
                )

            if not response.content:
                return None
            try:
                return response.json()
            except ValueError as exc:
                raise ApiError(
                    f"{method} {_safe_url(url)} returned invalid JSON: "
                    f"{_body_excerpt(response)}"
                ) from exc

        raise ApiError(
            f"{method} {_safe_url(url)} failed after {self.max_retries + 1} attempts: "
            f"{scrub(str(last_error))}"
        )

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> Any:
        return self.request("PUT", url, **kwargs)


def _safe_url(url: str) -> str:
    """Strip query strings so access tokens never reach the logs."""
    return url.split("?", 1)[0]


def _body_excerpt(response: requests.Response, limit: int = 300) -> str:
    text = scrub((response.text or "").strip().replace("\n", " "))
    return text[:limit] + ("..." if len(text) > limit else "")
