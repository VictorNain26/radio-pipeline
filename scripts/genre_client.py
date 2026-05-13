"""
Multi-source genre client for AubeSonore.

Aggregates 3 backends (MusicBrainz / Discogs / Last.fm) to obtain reliable
genre tags for a track, then applies a hybrid blocklist + allowlist policy.

- MusicBrainz: canonical genre/style taxonomy (primary)
- Discogs: best coverage for electronic + hip-hop (with token: 60 req/min)
- Last.fm: crowd-sourced tags, covers obscure artists

Disk cache (data/genre_cache.json) persists lookups for 30 days.

Compatible with the previous lastfm_client.GenreResult dataclass: same fields,
so callers (download.py) keep working unchanged.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lastfm_client import LastFMClient

logger = logging.getLogger(__name__)

DEFAULT_CACHE_PATH = Path(__file__).parent.parent / "data" / "genre_cache.json"
CACHE_TTL_SECONDS = 30 * 24 * 3600  # 30 days
REQUEST_TIMEOUT = 8
USER_AGENT = "AubeSonore-RadioPipeline/2.0 (https://radio.aubesonore.fr)"


@dataclass
class GenreResult:
    """Result of a genre lookup (compatible with lastfm_client.GenreResult)."""
    artist: str
    title: str
    tags: list[str]
    top_tag: str | None
    is_blocked: bool
    blocked_reason: str | None = None
    sources_hit: list[str] = field(default_factory=list)
    has_allowlist_match: bool = False


# -----------------------------------------------------------------------------
# Per-source clients
# -----------------------------------------------------------------------------


@dataclass
class _Throttle:
    """Token-bucket-ish throttle: min interval between requests (thread-safe)."""
    min_interval: float
    _last: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last = time.monotonic()


class MusicBrainzClient:
    """
    Thin MusicBrainz client (REST, no external lib).

    Looks up the recording by artist + title and returns canonical
    genre + tag names. Respects MB's 1 req/sec rate limit.
    """

    BASE = "https://musicbrainz.org/ws/2"

    def __init__(self, user_agent: str = USER_AGENT, throttle: _Throttle | None = None) -> None:
        self.user_agent = user_agent
        self.throttle = throttle or _Throttle(min_interval=1.05)

    def _request(self, path: str, params: dict[str, str]) -> dict[str, Any] | None:
        self.throttle.wait()
        qs = urllib.parse.urlencode({**params, "fmt": "json"})
        url = f"{self.BASE}{path}?{qs}"
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError) as e:
            logger.debug("MusicBrainz request failed: %s", e)
            return None

    def get_tags(self, artist: str, title: str) -> list[str]:
        """
        Get genre+tag names for the best-matching recording.

        Strategy: artist+recording search → top hit → use both `genres` and
        `tags` arrays (genres = curated, tags = crowd-sourced).
        """
        query = f'artist:"{artist}" AND recording:"{title}"'
        data = self._request("/recording", {"query": query, "limit": "1", "inc": "genres+tags"})
        if not data or not data.get("recordings"):
            return []
        rec = data["recordings"][0]
        names: list[str] = []
        for g in rec.get("genres", []) or []:
            n = (g.get("name") or "").lower().strip()
            if n:
                names.append(n)
        for t in rec.get("tags", []) or []:
            n = (t.get("name") or "").lower().strip()
            if n and n not in names:
                names.append(n)
        return names


class DiscogsClient:
    """
    Thin Discogs client (REST, no external lib).

    With a Personal Access Token, rate limit is 60 req/min (~1/sec).
    Without token, 25 req/min — supported but slower.

    Uses /database/search?type=release with artist+track query, returns
    `genre` + `style` from the top release hit.
    """

    BASE = "https://api.discogs.com"

    def __init__(self, token: str | None = None, throttle: _Throttle | None = None) -> None:
        self.token = token
        self.throttle = throttle or _Throttle(min_interval=1.05)

    def _request(self, path: str, params: dict[str, str]) -> dict[str, Any] | None:
        self.throttle.wait()
        qs = urllib.parse.urlencode(params)
        url = f"{self.BASE}{path}?{qs}"
        headers = {"User-Agent": USER_AGENT}
        if self.token:
            headers["Authorization"] = f"Discogs token={self.token}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError) as e:
            logger.debug("Discogs request failed: %s", e)
            return None

    def get_tags(self, artist: str, title: str) -> list[str]:
        data = self._request("/database/search", {
            "type": "release",
            "artist": artist,
            "track": title,
            "per_page": "5",
        })
        if not data or not data.get("results"):
            return []
        names: list[str] = []
        for result in data["results"][:3]:
            for g in result.get("genre") or []:
                n = g.lower().strip()
                if n and n not in names:
                    names.append(n)
            for s in result.get("style") or []:
                n = s.lower().strip()
                if n and n not in names:
                    names.append(n)
        return names


# -----------------------------------------------------------------------------
# Disk cache
# -----------------------------------------------------------------------------


class _DiskCache:
    """Thread-safe JSON-backed cache with per-entry TTL."""

    def __init__(self, path: Path, ttl: int = CACHE_TTL_SECONDS) -> None:
        self.path = path
        self.ttl = ttl
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("Genre cache corrupted, starting fresh")
            self._data = {}

    def get(self, key: str) -> list[str] | None:
        with self._lock:
            entry = self._data.get(key)
            if not entry:
                return None
            if time.time() - entry.get("fetched_at", 0) > self.ttl:
                return None
            return entry.get("tags", [])

    def put(self, key: str, tags: list[str]) -> None:
        with self._lock:
            self._data[key] = {"tags": tags, "fetched_at": time.time()}
            self._dirty = True

    def flush(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
            self._dirty = False


# -----------------------------------------------------------------------------
# Aggregator
# -----------------------------------------------------------------------------


@dataclass
class GenreClient:
    """
    Multi-source genre filter.

    Policy:
      1. blocklist hit on UNION(tags) → reject
      2. allowlist hit on UNION(tags) → accept (passes filter)
      3. no tags at all → accept (downstream Essentia AGGRESSIVE_FILTER takes over)
    """
    blocked_genres: set[str]
    allowed_genres: set[str]
    lastfm: LastFMClient | None = None
    musicbrainz: MusicBrainzClient | None = None
    discogs: DiscogsClient | None = None
    cache: _DiskCache | None = None

    def _cache_key(self, artist: str, title: str) -> str:
        return f"{artist.strip().lower()}|{title.strip().lower()}"

    def _gather_tags(self, artist: str, title: str) -> tuple[list[str], list[str]]:
        """Return (all_tags, sources_hit)."""
        all_tags: list[str] = []
        sources_hit: list[str] = []
        seen: set[str] = set()

        def extend(src_name: str, tags: list[str]) -> None:
            added = False
            for t in tags:
                t = t.lower().strip()
                if t and t not in seen:
                    seen.add(t)
                    all_tags.append(t)
                    added = True
            if added:
                sources_hit.append(src_name)

        if self.musicbrainz:
            extend("musicbrainz", self.musicbrainz.get_tags(artist, title))
        if self.discogs:
            extend("discogs", self.discogs.get_tags(artist, title))
        if self.lastfm:
            extend("lastfm", self.lastfm.get_track_tags(artist, title))
            # Last.fm artist fallback only if no per-track tag was found yet
            if not all_tags:
                extend("lastfm-artist", self.lastfm.get_artist_tags(artist))

        return all_tags, sources_hit

    def check_genre(self, artist: str, title: str) -> GenreResult:
        cache_key = self._cache_key(artist, title)
        cached: list[str] | None = None
        if self.cache:
            cached = self.cache.get(cache_key)

        if cached is not None:
            tags = cached
            sources_hit = ["cache"]
        else:
            tags, sources_hit = self._gather_tags(artist, title)
            if self.cache:
                self.cache.put(cache_key, tags)

        # Hard blocklist on union
        blocked_tag: str | None = None
        for t in tags:
            if t in self.blocked_genres:
                blocked_tag = t
                break

        # Soft allowlist
        has_allow = any(t in self.allowed_genres for t in tags)

        return GenreResult(
            artist=artist,
            title=title,
            tags=tags[:15],
            top_tag=tags[0] if tags else None,
            is_blocked=blocked_tag is not None,
            blocked_reason=(
                f"Genre '{blocked_tag}' is blocklisted" if blocked_tag else None
            ),
            sources_hit=sources_hit,
            has_allowlist_match=has_allow,
        )

    def flush_cache(self) -> None:
        if self.cache:
            self.cache.flush()


def create_genre_client(
    lastfm_api_key: str | None,
    discogs_token: str | None,
    blocked_genres: list[str],
    allowed_genres: list[str],
    cache_path: Path | None = None,
    enable_musicbrainz: bool = True,
    enable_discogs: bool = True,
) -> GenreClient:
    """Factory wiring the 3 backends + disk cache."""
    lastfm = (
        LastFMClient(api_key=lastfm_api_key, blocked_genres=set())
        if lastfm_api_key else None
    )
    mb = MusicBrainzClient() if enable_musicbrainz else None
    dc = DiscogsClient(token=discogs_token) if enable_discogs else None
    cache = _DiskCache(cache_path or DEFAULT_CACHE_PATH)

    return GenreClient(
        blocked_genres=set(g.lower() for g in blocked_genres),
        allowed_genres=set(g.lower() for g in allowed_genres),
        lastfm=lastfm,
        musicbrainz=mb,
        discogs=dc,
        cache=cache,
    )
