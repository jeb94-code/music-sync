"""One-time interactive helper that mints the tokens the container needs.

Run it once from a machine with a browser:

    docker compose run --rm --service-ports auth

It opens a tiny local web server, walks you through the Spotify and Deezer
consent screens, and prints the values to paste into your Portainer stack
environment. Nothing is written to the repository.
"""

from __future__ import annotations

import argparse
import secrets
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from .config import env_int, env_str
from .errors import AuthError, ConfigError
from .httpclient import HttpClient
from .spotify import AUTHORIZE_URL, SCOPES, TOKEN_URL

DEEZER_AUTHORIZE_URL = "https://connect.deezer.com/oauth/auth.php"
DEEZER_TOKEN_URL = "https://connect.deezer.com/oauth/access_token.php"

# The helper binds this only for the minute the consent flow takes, but a home
# server rarely has 8080 free -- Nextcloud, Traefik dashboards and half the
# self-hosted world sit there. 8479 keeps clear of those and of this project's
# own server-mode port (8477), so the two can never collide.
DEFAULT_AUTH_PORT = 8479
# basic_access is required for /user/me; manage_library covers reading the
# user's own (including private) playlists; offline_access makes the token
# non-expiring, which is what an unattended container needs.
DEEZER_PERMS = "basic_access,manage_library,offline_access"

_SUCCESS_PAGE = """<!doctype html>
<meta charset="utf-8">
<title>music-sync</title>
<body style="font-family:system-ui;margin:3rem;max-width:34rem">
<h1>{provider} connected</h1>
<p>You can close this tab. The token was printed in the terminal where you
started the helper.</p>
</body>"""

_ERROR_PAGE = """<!doctype html>
<meta charset="utf-8">
<title>music-sync</title>
<body style="font-family:system-ui;margin:3rem;max-width:34rem">
<h1>Authorization failed</h1>
<p>{message}</p>
</body>"""


class _CallbackHandler(BaseHTTPRequestHandler):
    """Captures the single OAuth redirect and then lets the server stop."""

    query: dict[str, list[str]] = {}
    provider = "Provider"

    def do_GET(self) -> None:  # noqa: N802 - name mandated by BaseHTTPRequestHandler
        parsed = urllib.parse.urlparse(self.path)
        # Favicon requests would otherwise be mistaken for the callback.
        if parsed.path in ("/favicon.ico", "/robots.txt"):
            self.send_response(404)
            self.end_headers()
            return

        _CallbackHandler.query = urllib.parse.parse_qs(parsed.query)
        error = _CallbackHandler.query.get("error_reason") or _CallbackHandler.query.get(
            "error"
        )
        body = (
            _ERROR_PAGE.format(message=error[0])
            if error
            else _SUCCESS_PAGE.format(provider=_CallbackHandler.provider)
        )
        encoded = body.encode("utf-8")
        self.send_response(400 if error else 200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args: object) -> None:
        """Silence the default stderr access log."""


def _await_callback(port: int, provider: str) -> dict[str, list[str]]:
    _CallbackHandler.query = {}
    _CallbackHandler.provider = provider
    # 0.0.0.0 so the redirect still arrives when the helper runs in a container
    # with the port published to the host.
    server = HTTPServer(("0.0.0.0", port), _CallbackHandler)
    try:
        while not _CallbackHandler.query:
            server.handle_request()
    finally:
        server.server_close()
    return _CallbackHandler.query


def _prompt(url: str, redirect_uri: str) -> None:
    print()
    print("Open this URL in your browser and approve the access:")
    print()
    print(f"    {url}")
    print()
    print(f"Waiting for the redirect to {redirect_uri} ...")


def auth_port() -> int:
    return env_int("AUTH_PORT", DEFAULT_AUTH_PORT, minimum=1)


def spotify_redirect_uri(port: int) -> str:
    """Where Spotify sends the browser back.

    Must match the Redirect URI registered on the Spotify app character for
    character, which is why it is derived from the port rather than hardcoded:
    changing AUTH_PORT has to change this too, or consent fails with
    INVALID_CLIENT. Spotify only accepts http for the literal IPv4 loopback
    address, so `localhost` is not interchangeable here.
    """
    return env_str("SPOTIFY_REDIRECT_URI", f"http://127.0.0.1:{port}/callback")  # type: ignore[return-value]


def deezer_redirect_uri(port: int) -> str:
    return env_str("DEEZER_REDIRECT_URI", f"http://localhost:{port}/callback")  # type: ignore[return-value]


def authorize_spotify(http: HttpClient) -> str:
    client_id = env_str("SPOTIFY_CLIENT_ID", required=True)
    client_secret = env_str("SPOTIFY_CLIENT_SECRET", required=True)
    port = auth_port()
    redirect_uri = spotify_redirect_uri(port)
    state = secrets.token_urlsafe(16)

    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "state": state,
            # Always show the dialog so re-running the helper actually re-consents.
            "show_dialog": "true",
        }
    )
    _prompt(f"{AUTHORIZE_URL}?{query}", redirect_uri)

    params = _await_callback(port, "Spotify")
    if "error" in params:
        raise AuthError(f"Spotify authorization failed: {params['error'][0]}")
    if params.get("state", [None])[0] != state:
        raise AuthError("Spotify returned a mismatched state parameter; aborting")
    code = params.get("code", [None])[0]
    if not code:
        raise AuthError("Spotify did not return an authorization code")

    payload = http.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        auth=(client_id, client_secret),  # type: ignore[arg-type]
    )
    refresh_token = (payload or {}).get("refresh_token")
    if not refresh_token:
        raise AuthError(f"Spotify did not return a refresh token: {payload}")
    return str(refresh_token)


def authorize_deezer(http: HttpClient) -> str:
    app_id = env_str("DEEZER_APP_ID", required=True)
    app_secret = env_str("DEEZER_APP_SECRET", required=True)
    port = auth_port()
    redirect_uri = deezer_redirect_uri(port)

    query = urllib.parse.urlencode(
        {
            "app_id": app_id,
            "redirect_uri": redirect_uri,
            "perms": DEEZER_PERMS,
            "response_type": "code",
        }
    )
    _prompt(f"{DEEZER_AUTHORIZE_URL}?{query}", redirect_uri)

    params = _await_callback(port, "Deezer")
    if "error_reason" in params:
        raise AuthError(f"Deezer authorization failed: {params['error_reason'][0]}")
    code = params.get("code", [None])[0]
    if not code:
        raise AuthError("Deezer did not return an authorization code")

    # Deezer's token endpoint is a GET and answers with JSON when asked to.
    payload = http.get(
        DEEZER_TOKEN_URL,
        params={
            "app_id": app_id,
            "secret": app_secret,
            "code": code,
            "output": "json",
        },
    )
    access_token = (payload or {}).get("access_token")
    if not access_token:
        raise AuthError(f"Deezer did not return an access token: {payload}")
    if str((payload or {}).get("expires", "0")) != "0":
        print(
            "  Note: this token expires. Re-run the helper if syncing starts "
            "failing with an auth error.",
            file=sys.stderr,
        )
    return str(access_token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="musicsync.auth",
        description="Mint the Spotify refresh token and Deezer access token.",
    )
    parser.add_argument(
        "provider",
        nargs="?",
        default="both",
        choices=["spotify", "deezer", "both"],
        help="which provider to authorize (default: both)",
    )
    args = parser.parse_args(argv)

    results: dict[str, str] = {}
    try:
        with HttpClient(timeout=30, max_retries=2) as http:
            if args.provider in ("spotify", "both"):
                results["SPOTIFY_REFRESH_TOKEN"] = authorize_spotify(http)
            if args.provider in ("deezer", "both"):
                results["DEEZER_ACCESS_TOKEN"] = authorize_deezer(http)
    except (AuthError, ConfigError) as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        return 130

    print()
    print("=" * 68)
    print("Add these to your Portainer stack environment (never commit them):")
    print("=" * 68)
    for key, value in results.items():
        print(f"{key}={value}")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
