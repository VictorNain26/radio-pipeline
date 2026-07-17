"""
Discovery sources for AubeSonore.

Pluggable architecture:
- HypeMachineSource: legacy "popular" endpoint
- RSSSource: any RSS/Atom feed, parser strategy per feed (em-dash, tilde, quoted)
- LastFMTagSource: tag.gettoptracks for indie/electronic/ambient/hip-hop/...
- ManualPicksSource: data/manual_picks.json (existing UX preserved)
- CustomFeedsSource: data/custom_feeds.json — paste any rss.app / generic RSS URLs

Each source returns a list of Track dicts in the canonical format used by
download.py. Orchestration (dedup, write) is done in discover.py.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypedDict

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (compatible; AubeSonore-RadioPipeline/2.0; "
    "+https://radio.aubesonore.fr)"
)


class Track(TypedDict):
    """Canonical track shape consumed by download.py."""
    id: str
    artist: str
    title: str
    cover: str | None
    search: str
    source: str  # added: provenance label for monitoring


# -----------------------------------------------------------------------------
# Title parsers
# -----------------------------------------------------------------------------

# Em-dash, en-dash, hyphen (with surrounding spaces), and middot.
_DASH_RE = re.compile(r"\s+[–—‒\-·]\s+")
# Tilde with spaces — A Closer Listen pattern: "Artist ~ Title"
_TILDE_RE = re.compile(r"\s+~\s+")
# Strip wrapping smart/straight quotes from the title side.
_QUOTE_STRIP_RE = re.compile(r"^[\"“”‘’']+|[\"“”‘’']+$")
# HTML entities seen in feeds (feedparser usually decodes these — kept as a safety net).
_ENTITY_MAP = {
    "&#8211;": "–",  # en dash
    "&#8212;": "—",  # em dash
    "&#8220;": "“",
    "&#8221;": "”",
    "&#038;": "&",
    "&amp;": "&",
}


def _decode_entities(s: str) -> str:
    for k, v in _ENTITY_MAP.items():
        s = s.replace(k, v)
    return s


def _clean(s: str) -> str:
    return _QUOTE_STRIP_RE.sub("", s).strip()


def _parse_dash_title(title: str) -> tuple[str, str] | None:
    """Parse 'Artist – Title' / 'Artist - Title'. Returns None if no match."""
    title = _decode_entities(title)
    parts = _DASH_RE.split(title, maxsplit=1)
    if len(parts) != 2:
        return None
    artist, track = _clean(parts[0]), _clean(parts[1])
    if not artist or not track:
        return None
    return artist, track


def _parse_tilde_title(title: str) -> tuple[str, str] | None:
    title = _decode_entities(title)
    parts = _TILDE_RE.split(title, maxsplit=1)
    if len(parts) != 2:
        return None
    artist, track = _clean(parts[0]), _clean(parts[1])
    if not artist or not track:
        return None
    return artist, track


# Entry-level parsers (take the whole feedparser entry dict).
EntryParser = Callable[[Any], tuple[str, str] | None]


def parse_dash(entry: Any) -> tuple[str, str] | None:
    return _parse_dash_title(entry.get("title") or "")


def parse_tilde(entry: Any) -> tuple[str, str] | None:
    return _parse_tilde_title(entry.get("title") or "")


# 'Artist – "Track"' (Stereogum-style): _clean() already strips the quotes,
# so this is genuinely the same parser — kept as a distinct config name.
parse_dash_then_quoted = parse_dash


def parse_pitchfork(entry: Any) -> tuple[str, str] | None:
    """
    Pitchfork track-review feed: title = "Track" only (smart quotes),
    artist embedded in the URL slug. Path is
    /reviews/tracks/{artist-slug}-{track-slug}/.

    Best-effort: artist casing is recovered via title-case which gets
    "Charli Xcx" instead of "Charli XCX", but it's good enough for
    YouTube fuzzy matching downstream.
    """
    title_raw = entry.get("title") or ""
    link = entry.get("link") or ""
    if not title_raw or not link:
        return None
    title = _clean(_decode_entities(title_raw))
    if not title:
        return None
    # Slugify the title to match Pitchfork's URL convention
    track_slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if not track_slug:
        return None
    path = link.rstrip("/").rsplit("/", 1)[-1]
    suffix = "-" + track_slug
    if not path.endswith(suffix):
        return None
    artist_slug = path[: -len(suffix)]
    if not artist_slug:
        return None
    artist = artist_slug.replace("-", " ").title()
    return artist, title


PARSERS: dict[str, EntryParser] = {
    "dash": parse_dash,
    "tilde": parse_tilde,
    "dash_quoted": parse_dash_then_quoted,
    "pitchfork": parse_pitchfork,
}


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _stable_id(source: str, artist: str, title: str) -> str:
    h = hashlib.sha1(f"{source}|{artist.lower()}|{title.lower()}".encode()).hexdigest()
    return f"{source[:6]}_{h[:16]}"


def _make_track(source: str, artist: str, title: str, cover: str | None = None) -> Track:
    artist = artist.strip()
    title = title.strip()
    return {
        "id": _stable_id(source, artist, title),
        "artist": artist,
        "title": title,
        "cover": cover,
        "search": f"{artist} - {title}",
        "source": source,
    }


def _redact_url(url: str) -> str:
    """Strip credential-bearing query values (api_key=...) before logging."""
    return re.sub(r"(api_key=)[^&]+", r"\1***", url)


def _http_get_json(url: str, headers: dict[str, str] | None = None) -> Any | None:
    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError) as e:
        logger.warning("HTTP GET failed for %s: %s", _redact_url(url), e)
        return None


def _http_get_bytes(url: str) -> bytes | None:
    """Fetch raw bytes with an enforced timeout (feedparser has none of its own)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        logger.warning("HTTP GET failed for %s: %s", _redact_url(url), e)
        return None


# -----------------------------------------------------------------------------
# Sources
# -----------------------------------------------------------------------------


class DiscoverySource:
    """Base class. Subclasses implement fetch()."""
    name: str = "base"

    def fetch(self) -> list[Track]:
        raise NotImplementedError


@dataclass
class HypeMachineSource(DiscoverySource):
    """HypeMachine 'popular' API — kept as one source among many."""
    count: int = 50
    name: str = "hypem"

    def fetch(self) -> list[Track]:
        url = f"https://api.hypem.com/v2/popular?mode=now&count={self.count}"
        data = _http_get_json(url)
        if not isinstance(data, list):
            logger.warning("HypeMachine: unexpected response")
            return []
        tracks: list[Track] = []
        for entry in data:
            artist = (entry.get("artist") or "").strip()
            title = (entry.get("title") or "").strip()
            if not artist or not title:
                continue
            cover = entry.get("thumb_url_large")
            tracks.append(_make_track(self.name, artist, title, cover))
        logger.info("  hypem: %d tracks", len(tracks))
        return tracks


@dataclass
class RSSFeedConfig:
    """Single feed config used by RSSSource."""
    url: str
    parser: str = "dash"                       # PARSERS key
    link_must_contain: str | None = None       # optional path filter (e.g. "/music/")
    label: str = ""                            # short label used as source name
    enabled: bool = True
    limit: int = 30                            # max items processed from the feed


@dataclass
class RSSSource(DiscoverySource):
    """
    Parses a list of RSS/Atom feeds via feedparser.

    feedparser is imported lazily so the module loads even if the lib is
    missing (we just disable RSS in that case).
    """
    feeds: list[RSSFeedConfig]
    name: str = "rss"

    def fetch(self) -> list[Track]:
        try:
            import feedparser  # type: ignore
        except ImportError:
            logger.warning(
                "RSS sources disabled: install feedparser (pip install feedparser>=6.0)"
            )
            return []

        all_tracks: list[Track] = []

        for feed_cfg in self.feeds:
            if not feed_cfg.enabled:
                continue
            parser_fn = PARSERS.get(feed_cfg.parser)
            if not parser_fn:
                logger.warning("RSS: unknown parser '%s' for %s", feed_cfg.parser, feed_cfg.label)
                continue

            logger.info("  RSS %s ...", feed_cfg.label or feed_cfg.url)
            # Fetch ourselves with a timeout — feedparser.parse(url) applies
            # none, so a silent feed used to block discovery indefinitely.
            raw = _http_get_bytes(feed_cfg.url)
            if raw is None:
                logger.warning("  RSS fetch failed (%s)", feed_cfg.label)
                continue
            try:
                parsed = feedparser.parse(raw)
            except Exception as e:  # feedparser is very tolerant; just in case
                logger.warning("  RSS parse failed (%s): %s", feed_cfg.label, e)
                continue

            if parsed.get("bozo") and not parsed.get("entries"):
                logger.warning("  RSS empty/invalid (%s)", feed_cfg.label)
                continue

            kept = 0
            for entry in parsed.entries[: feed_cfg.limit]:
                if feed_cfg.link_must_contain:
                    link = entry.get("link") or ""
                    if feed_cfg.link_must_contain not in link:
                        continue
                if not entry.get("title"):
                    continue
                parsed_pair = parser_fn(entry)
                if parsed_pair is None:
                    continue
                artist, track_title = parsed_pair
                all_tracks.append(
                    _make_track(feed_cfg.label or "rss", artist, track_title, None)
                )
                kept += 1
            logger.info("    %d kept", kept)

        return all_tracks


@dataclass
class LastFMTagSource(DiscoverySource):
    """
    Last.fm tag.gettoptracks: one of the most reliable indie/electronic/ambient/
    hip-hop discovery feeds. We pull N top tracks per tag.
    """
    api_key: str
    tags: list[str]
    per_tag_limit: int = 20
    name: str = "lastfm-tag"

    def fetch(self) -> list[Track]:
        out: list[Track] = []
        for tag in self.tags:
            url = (
                "https://ws.audioscrobbler.com/2.0/?method=tag.gettoptracks"
                f"&tag={urllib.parse.quote(tag)}"
                f"&limit={self.per_tag_limit}"
                f"&api_key={self.api_key}"
                "&format=json"
            )
            data = _http_get_json(url)
            if not data:
                continue
            entries = (data.get("tracks") or {}).get("track") or []
            if isinstance(entries, dict):
                entries = [entries]
            kept = 0
            for entry in entries:
                artist = ((entry.get("artist") or {}).get("name") or "").strip()
                title = (entry.get("name") or "").strip()
                if not artist or not title:
                    continue
                # Use highest-resolution image if available
                cover = None
                for img in entry.get("image") or []:
                    if img.get("size") in ("extralarge", "large") and img.get("#text"):
                        cover = img["#text"]
                        break
                out.append(_make_track(f"lastfm:{tag}", artist, title, cover))
                kept += 1
            logger.info("  lastfm tag=%s: %d tracks", tag, kept)
        return out


@dataclass
class PersonalArtistsSource(DiscoverySource):
    """
    Discovery driven by Victor's own library (taste profile seeds).

    Each run takes `seeds_per_run` artists from the seed list (round-robin
    via a persistent cursor file), asks Last.fm artist.getSimilar for each,
    keeps similar artists NOT already in the seed list (maximise novelty),
    and pulls their top tracks. Complements — not replaces — the RSS/tag
    sources.
    """
    api_key: str
    seeds: list[str]
    cursor_path: Path = Path("data/personal_seeds_cursor.json")
    seeds_per_run: int = 15
    similar_per_seed: int = 4
    tracks_per_artist: int = 2
    min_match: float = 0.35        # Last.fm similarity score floor
    name: str = "personal"

    def _read_cursor(self) -> int:
        try:
            return int(json.loads(self.cursor_path.read_text(encoding="utf-8"))["cursor"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return 0

    def _write_cursor(self, cursor: int) -> None:
        try:
            self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
            self.cursor_path.write_text(
                json.dumps({"cursor": cursor}), encoding="utf-8")
        except OSError as e:
            logger.warning("personal: cannot persist cursor: %s", e)

    def _pick_seeds(self) -> list[str]:
        if not self.seeds:
            return []
        n = min(self.seeds_per_run, len(self.seeds))
        start = self._read_cursor() % len(self.seeds)
        picked = [self.seeds[(start + i) % len(self.seeds)] for i in range(n)]
        self._write_cursor((start + n) % len(self.seeds))
        return picked

    def _get_similar(self, artist: str) -> list[str]:
        url = (
            "https://ws.audioscrobbler.com/2.0/?method=artist.getsimilar"
            f"&artist={urllib.parse.quote(artist)}"
            f"&limit={self.similar_per_seed * 3}"   # headroom for filtering
            f"&api_key={self.api_key}"
            "&format=json&autocorrect=1"
        )
        data = _http_get_json(url)
        if not data:
            return []
        entries = (data.get("similarartists") or {}).get("artist") or []
        if isinstance(entries, dict):
            entries = [entries]
        seed_names = {s.strip().lower() for s in self.seeds}
        out: list[str] = []
        for entry in entries:
            name = (entry.get("name") or "").strip()
            try:
                match = float(entry.get("match", 0))
            except (TypeError, ValueError):
                match = 0.0
            if not name or match < self.min_match:
                continue
            if name.lower() in seed_names:
                continue  # already in the personal library — prioritise novelty
            out.append(name)
            if len(out) >= self.similar_per_seed:
                break
        return out

    def _get_top_tracks(self, artist: str) -> list[tuple[str, str | None]]:
        url = (
            "https://ws.audioscrobbler.com/2.0/?method=artist.gettoptracks"
            f"&artist={urllib.parse.quote(artist)}"
            f"&limit={self.tracks_per_artist}"
            f"&api_key={self.api_key}"
            "&format=json&autocorrect=1"
        )
        data = _http_get_json(url)
        if not data:
            return []
        entries = (data.get("toptracks") or {}).get("track") or []
        if isinstance(entries, dict):
            entries = [entries]
        out: list[tuple[str, str | None]] = []
        for entry in entries[: self.tracks_per_artist]:
            title = (entry.get("name") or "").strip()
            if not title:
                continue
            cover = None
            for img in entry.get("image") or []:
                if img.get("size") in ("extralarge", "large") and img.get("#text"):
                    cover = img["#text"]
                    break
            out.append((title, cover))
        return out

    def fetch(self) -> list[Track]:
        seeds = self._pick_seeds()
        if not seeds:
            logger.info("  personal: no seed artists (taste profile not built)")
            return []
        out: list[Track] = []
        similar_seen: set[str] = set()
        for seed in seeds:
            for similar in self._get_similar(seed):
                key = similar.lower()
                if key in similar_seen:
                    continue
                similar_seen.add(key)
                for title, cover in self._get_top_tracks(similar):
                    out.append(_make_track(self.name, similar, title, cover))
        logger.info("  personal: %d seeds → %d similar artists → %d tracks",
                    len(seeds), len(similar_seen), len(out))
        return out


@dataclass
class ManualPicksSource(DiscoverySource):
    """Reads data/manual_picks.json. Same shape as before."""
    path: Path
    name: str = "manual"

    def fetch(self) -> list[Track]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Manual picks unreadable: %s", e)
            return []
        out: list[Track] = []
        for entry in raw or []:
            artist = (entry.get("artist") or "").strip()
            title = (entry.get("title") or "").strip()
            if not artist or not title:
                continue
            out.append(_make_track(self.name, artist, title, entry.get("cover")))
        if out:
            logger.info("  manual: %d picks", len(out))
        return out


@dataclass
class CustomFeedsSource(DiscoverySource):
    """
    Lets the user paste any RSS URL (e.g. from rss.app) in data/custom_feeds.json.

    Format of data/custom_feeds.json:
      [
        {"url": "https://rss.app/feeds/xxx.xml", "parser": "dash", "label": "myfeed",
         "link_must_contain": null, "limit": 20}
      ]
    """
    path: Path
    name: str = "custom"

    def fetch(self) -> list[Track]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Custom feeds unreadable: %s", e)
            return []
        if not raw:
            return []
        feeds = []
        for entry in raw:
            try:
                feeds.append(
                    RSSFeedConfig(
                        url=entry["url"],
                        parser=entry.get("parser", "dash"),
                        link_must_contain=entry.get("link_must_contain"),
                        label=entry.get("label", "custom"),
                        enabled=entry.get("enabled", True),
                        limit=int(entry.get("limit", 30)),
                    )
                )
            except (KeyError, TypeError) as e:
                logger.warning("Skipping invalid custom feed entry: %s (%s)", entry, e)
        if not feeds:
            return []
        return RSSSource(feeds=feeds).fetch()
