"""
Environment configuration with validation.

Uses Pydantic for type-safe configuration with automatic validation.
Loads from environment variables and .env file.
"""

import logging
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Try pydantic v2, fallback to v1 if needed
try:
    from pydantic import Field, field_validator, model_validator
    from pydantic_settings import BaseSettings, SettingsConfigDict
    PYDANTIC_V2 = True
except ImportError:
    from pydantic import BaseSettings, Field, validator, root_validator  # type: ignore
    PYDANTIC_V2 = False

logger = logging.getLogger(__name__)


def is_loopback_host(hostname: str | None) -> bool:
    """True if the host resolves trivially to the local machine (no network exposure)."""
    if not hostname:
        return False
    host = hostname.strip("[]").lower()
    return host == "localhost" or host == "::1" or host.startswith("127.")

# Get project root directory
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent


class Settings(BaseSettings):
    """
    Application settings with validation.

    All settings are loaded from environment variables.
    Use a .env file in the project root for local development.
    """

    # AzuraCast configuration
    azuracast_url: str = Field(
        ...,
        description="AzuraCast server URL (must be HTTPS in production)",
        examples=["https://radio.aubesonore.fr"],
    )
    azuracast_api_key: str = Field(
        ...,
        min_length=10,
        description="AzuraCast API key",
    )
    azuracast_station_id: int = Field(
        default=1,
        ge=1,
        description="AzuraCast station ID",
    )

    # Spotify API configuration (for reliable audio features)
    spotify_client_id: str | None = Field(
        default=None,
        description="Spotify API Client ID",
    )
    spotify_client_secret: str | None = Field(
        default=None,
        description="Spotify API Client Secret",
    )

    # Optional API keys for metadata enrichment
    acoustid_api_key: str | None = Field(
        default=None,
        description="AcoustID API key for audio fingerprinting",
    )
    lastfm_api_key: str | None = Field(
        default=None,
        description="Last.fm API key for genre lookup",
    )
    discogs_token: str | None = Field(
        default=None,
        description="Discogs Personal Access Token (raises rate limit to 60 req/min)",
    )

    # Pipeline settings
    max_tracks_per_run: int = Field(
        default=30,
        ge=1,
        le=100,
        description="Maximum tracks to discover per run",
    )
    download_timeout: int = Field(
        default=300,
        ge=30,
        description="Download timeout in seconds",
    )
    http_timeout: int = Field(
        default=30,
        ge=5,
        description="HTTP request timeout in seconds",
    )
    max_retries: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Maximum HTTP retry attempts",
    )

    # Feature flags
    ssl_verify: bool = Field(
        default=True,
        description="Verify SSL certificates (disable only for local testing)",
    )
    debug: bool = Field(
        default=False,
        description="Enable debug logging",
    )

    if PYDANTIC_V2:
        model_config = SettingsConfigDict(
            env_file=str(PROJECT_ROOT / ".env"),
            env_file_encoding="utf-8",
            case_sensitive=False,
            extra="ignore",
        )

        @field_validator("azuracast_url")
        @classmethod
        def validate_url(cls, v: str) -> str:
            """Validate AzuraCast URL format and security."""
            v = v.rstrip("/")
            parsed = urlparse(v)

            if not parsed.scheme:
                raise ValueError("URL must include scheme (https://)")

            if parsed.scheme not in ("http", "https"):
                raise ValueError(f"Invalid URL scheme: {parsed.scheme}")

            if not parsed.netloc:
                raise ValueError("URL must include hostname")

            # HTTP is only acceptable when traffic never leaves the machine
            if parsed.scheme == "http" and not is_loopback_host(parsed.hostname):
                logger.warning("Using HTTP toward a non-local host is insecure.")

            return v

        @model_validator(mode="after")
        def validate_security(self) -> "Settings":
            """Validate security-related settings."""
            parsed = urlparse(self.azuracast_url)

            # Require HTTPS in production, except toward loopback (no network exposure)
            if not self.debug and parsed.scheme == "http" and not is_loopback_host(parsed.hostname):
                raise ValueError(
                    "HTTP toward a remote host is not allowed in production. "
                    "Use HTTPS, or DEBUG=1 for testing."
                )

            # Warn about disabled SSL verification
            if not self.ssl_verify:
                logger.warning(
                    "SSL verification is disabled. This is insecure and should only be used for local testing."
                )

            return self
    else:
        # Pydantic v1 compatibility
        class Config:
            env_file = str(PROJECT_ROOT / ".env")
            env_file_encoding = "utf-8"
            case_sensitive = False
            extra = "ignore"

        @validator("azuracast_url")
        def validate_url(cls, v: str) -> str:
            v = v.rstrip("/")
            parsed = urlparse(v)
            if not parsed.scheme or not parsed.netloc:
                raise ValueError("Invalid URL format")
            return v


@lru_cache()
def get_settings() -> Settings:
    """
    Get cached settings instance.

    Returns:
        Validated Settings instance.

    Raises:
        ValidationError: If configuration is invalid.
    """
    try:
        return Settings()  # type: ignore
    except Exception:
        logger.error("Configuration error", exc_info=True)
        raise


def validate_environment() -> tuple[bool, list[str]]:
    """
    Validate environment configuration.

    Returns:
        Tuple of (is_valid, list of error messages).
    """
    errors: list[str] = []

    try:
        settings = get_settings()

        # Additional runtime checks
        parsed = urlparse(settings.azuracast_url)

        if parsed.scheme == "http" and not settings.debug and not is_loopback_host(parsed.hostname):
            errors.append("HTTP toward a remote host is insecure. Use HTTPS for production.")

        if not settings.ssl_verify and not settings.debug:
            errors.append("SSL verification disabled in production.")

    except Exception as e:
        errors.append(str(e))

    return len(errors) == 0, errors


def print_config_status() -> None:
    """Print configuration status for debugging."""
    try:
        settings = get_settings()

        print("Configuration Status:")
        print(f"  AzuraCast URL: {settings.azuracast_url}")
        print(f"  Station ID: {settings.azuracast_station_id}")
        key = settings.azuracast_api_key
        masked = f"{'*' * 8}...{key[-4:]}" if len(key) >= 4 else "***"
        print(f"  API Key: {masked}")
        print(f"  Spotify: {'configured' if settings.spotify_client_id and settings.spotify_client_secret else 'not set'}")
        print(f"  AcoustID: {'configured' if settings.acoustid_api_key else 'not set'}")
        print(f"  Last.fm: {'configured' if settings.lastfm_api_key else 'not set'}")
        print(f"  Discogs: {'configured' if settings.discogs_token else 'not set (using anonymous rate limit)'}")
        print(f"  SSL Verify: {settings.ssl_verify}")
        print(f"  Debug: {settings.debug}")

        is_valid, errors = validate_environment()
        if is_valid:
            print("  Status: OK")
        else:
            print("  Status: ERRORS")
            for error in errors:
                print(f"    - {error}")

    except Exception:
        logger.error("Configuration error", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    print_config_status()
