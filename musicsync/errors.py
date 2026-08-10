"""Exception types shared across the package."""

from __future__ import annotations


class MusicSyncError(Exception):
    """Base class for all errors raised by this package."""


class ConfigError(MusicSyncError):
    """Raised when the environment configuration is missing or malformed."""


class ApiError(MusicSyncError):
    """Raised when an upstream API returns an error we cannot recover from."""


class AuthError(ApiError):
    """Raised when credentials are rejected or a token cannot be refreshed."""


class SafetyAbort(MusicSyncError):
    """Raised when a mirror would be destructive based on incomplete data."""
