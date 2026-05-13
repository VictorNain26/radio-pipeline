"""
Last.fm API client for genre/tag lookup.

Best practices 2026:
- Pre-download genre filtering to save bandwidth
- Crowd-sourced tags for reliable genre identification
- Caching to reduce API calls
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Constants
LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"
REQUEST_TIMEOUT = 10
MAX_CACHE_SIZE = 1000


@dataclass
class GenreResult:
    """Result of genre lookup."""
    artist: str
    title: str
    tags: list[str]
    top_tag: str | None
    is_blocked: bool
    blocked_reason: str | None = None


@dataclass
class LastFMClient:
    """
    Last.fm API client for genre lookup.

    Uses track.getTopTags and artist.getTopTags for reliable genre info.
    """
    api_key: str
    blocked_genres: set[str] = field(default_factory=set)
    _cache: dict[str, list[str]] = field(default_factory=dict, init=False)

    def _make_request(self, method: str, params: dict[str, str]) -> dict[str, Any] | None:
        """
        Make a Last.fm API request.

        Args:
            method: API method (e.g., 'track.getTopTags')
            params: Additional parameters.

        Returns:
            JSON response or None on error.
        """
        all_params = {
            "method": method,
            "api_key": self.api_key,
            "format": "json",
            **params,
        }

        url = f"{LASTFM_API_URL}?{urllib.parse.urlencode(all_params)}"

        headers = {"User-Agent": "RadioPipeline/2.0 (AubeSonore)"}
        req = urllib.request.Request(url, headers=headers)

        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
                data = json.loads(response.read().decode("utf-8"))
                if "error" in data:
                    logger.debug("Last.fm API error: %s", data.get("message", "Unknown"))
                    return None
                return data
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError) as e:
            logger.debug("Last.fm request failed: %s", e)
            return None

    def _cache_put(self, key: str, value: list[str]) -> None:
        """Store a value in the cache, clearing it if max size is exceeded."""
        if len(self._cache) >= MAX_CACHE_SIZE:
            self._cache.clear()
        self._cache[key] = value

    def _extract_tags(self, data: dict[str, Any] | None) -> list[str]:
        """Extract lowercase tag names from a Last.fm toptags response."""
        if not data or "toptags" not in data or "tag" not in data["toptags"]:
            return []
        raw_tags = data["toptags"]["tag"]
        if not isinstance(raw_tags, list):
            return []
        return [t["name"].lower() for t in raw_tags if "name" in t]

    def get_track_tags(self, artist: str, title: str) -> list[str]:
        """
        Get tags for a specific track.

        Args:
            artist: Artist name.
            title: Track title.

        Returns:
            List of tag names (lowercase).
        """
        cache_key = f"track:{artist.lower()}:{title.lower()}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        data = self._make_request("track.getTopTags", {
            "artist": artist,
            "track": title,
        })
        tags = self._extract_tags(data)
        self._cache_put(cache_key, tags)
        return tags

    def get_artist_tags(self, artist: str) -> list[str]:
        """
        Get tags for an artist (fallback if track has no tags).

        Args:
            artist: Artist name.

        Returns:
            List of tag names (lowercase).
        """
        cache_key = f"artist:{artist.lower()}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        data = self._make_request("artist.getTopTags", {
            "artist": artist,
        })
        tags = self._extract_tags(data)
        self._cache_put(cache_key, tags)
        return tags

    def check_genre(self, artist: str, title: str) -> GenreResult:
        """
        Check if a track's genre is allowed.

        Tries track tags first, falls back to artist tags.

        Args:
            artist: Artist name.
            title: Track title.

        Returns:
            GenreResult with tags and blocked status.
        """
        # Try track-specific tags first
        tags = self.get_track_tags(artist, title)

        # Fallback to artist tags if track has no tags
        if not tags:
            tags = self.get_artist_tags(artist)

        top_tag = tags[0] if tags else None

        # Check against blocked genres
        blocked_tag = None
        for tag in tags[:10]:  # Check top 10 tags
            if tag in self.blocked_genres:
                blocked_tag = tag
                break

        return GenreResult(
            artist=artist,
            title=title,
            tags=tags[:10],
            top_tag=top_tag,
            is_blocked=blocked_tag is not None,
            blocked_reason=f"Genre '{blocked_tag}' is blacklisted" if blocked_tag else None,
        )


def create_lastfm_client(api_key: str, blocked_genres: list[str] | None = None) -> LastFMClient:
    """
    Factory function to create a Last.fm client.

    Args:
        api_key: Last.fm API key.
        blocked_genres: List of genres to block.

    Returns:
        Configured LastFMClient instance.
    """
    blocked = set(g.lower() for g in (blocked_genres or []))
    return LastFMClient(api_key=api_key, blocked_genres=blocked)
