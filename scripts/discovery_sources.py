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


def parse_dash_then_quoted(entry: Any) -> tuple[str, str] | None:
    """For 'Artist – "Track"' (Stereogum-style)."""
    return _parse_dash_title(entry.get("title") or "")


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


def _http_get_json(url: str, headers: dict[str, str] | None = None) -> Any | None:
    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError) as e:
        logger.warning("HTTP GET failed for %s: %s", url, e)
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
    require_track_artist_separator: bool = True


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
            try:
                parsed = feedparser.parse(
                    feed_cfg.url,
                    request_headers={"User-Agent": USER_AGENT},
                )
            except Exception as e:  # feedparser is very tolerant; just in case
                logger.warning("  RSS fetch failed (%s): %s", feed_cfg.label, e)
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
