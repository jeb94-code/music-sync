"""Exception types shared across the package."""

from __future__ import annotations


class MusicSyncError(Exception):
    """Base class for all errors raised by this package."""


class ConfigError(MusicSyncError):
    """Raised when the environment configuration is missing or malformed."""


class ApiError(MusicSyncError):
    """Raised when an upstream API returns an error we cannot recover from."""


class AuthError(ApiError):
    """Raised when credentials are rejected or a token cannot be refreshed.

    This means the token itself is the problem (HTTP 401), so minting a fresh
    one is worth trying.
    """


class ForbiddenError(ApiError):
    """Raised when the credentials are valid but the request is not allowed.

    HTTP 403. Deliberately not an AuthError: the token is fine, so refreshing
    it changes nothing. Account state or missing scopes cause these, and they
    need a human, not a retry.
    """


class SafetyAbort(MusicSyncError):
    """Raised when a mirror would be destructive based on incomplete data."""
