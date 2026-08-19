#!/usr/bin/env python3
"""
AubeSonore discovery orchestrator (v3 — multi-source).

Aggregates tracks from:
- HypeMachine "popular" API (legacy, kept as one source among many)
- Curated RSS feeds (config.RSS_FEEDS) parsed by feedparser
- Last.fm tag charts (config.LASTFM_TAGS) — fills the hip-hop angle
- data/custom_feeds.json — arbitrary user-added RSS (e.g. rss.app)

Manual picks (data/manual_picks.json) are integrated as a regular
source — see ManualPicksSource in discovery_sources.py.

Dedup is done globally on a normalized (artist, title) key, capped to
config.DISCOVER_MAX_TRACKS. Writes tracks-to-download.json with the same
shape as before (TypedDict-compatible).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

# Make sibling imports work both as script and as module.
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from discovery_sources import (  # noqa: E402
    CustomFeedsSource,
    DiscoverySource,
    HypeMachineSource,
    LastFMTagSource,
    ManualPicksSource,
    PersonalArtistsSource,
    RSSFeedConfig,
    RSSSource,
    Track,
)
from settings import get_settings, validate_environment  # noqa: E402
from taste_profile import load_seed_artists  # noqa: E402
from track_db import normalize_track_key  # noqa: E402

try:
    from config import (  # noqa: E402
        DISCOVER_MAX_TRACKS,
        LASTFM_TAGS,
        LASTFM_TAG_LIMIT,
        RSS_FEEDS,
        TASTE_DISCOVERY_SEEDS_PER_RUN,
        TASTE_DISCOVERY_SIMILAR_PER_SEED,
        TASTE_DISCOVERY_TRACKS_PER_ARTIST,
        source_priority,
    )
except ImportError as e:
    print(f"Error: config.py missing discovery fields: {e}")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

PIPELINE_DIR = Path(__file__).parent.parent
OUTPUT_FILE = PIPELINE_DIR / "tracks-to-download.json"
CUSTOM_FEEDS_FILE = PIPELINE_DIR / "data" / "custom_feeds.json"
MANUAL_PICKS_FILE = PIPELINE_DIR / "data" / "manual_picks.json"

# A bigger HypeMachine batch keeps the legacy source competitive among many.
HYPEM_COUNT = 50


# Canonical normalization shared with download/track_db so the discovery
# dedup and the library dedup agree on what "the same track" means.
_normalize_key = normalize_track_key


def _build_sources() -> list[DiscoverySource]:
    settings = get_settings()
    sources: list[DiscoverySource] = [HypeMachineSource(count=HYPEM_COUNT)]

    # RSS sources from config — translated to discovery_sources.RSSFeedConfig
    rss_cfgs = [
        RSSFeedConfig(
            url=spec.url,
            parser=spec.parser,
            link_must_contain=spec.link_must_contain,
            label=spec.label,
            enabled=spec.enabled,
            limit=spec.limit,
        )
        for spec in RSS_FEEDS
    ]
    if rss_cfgs:
        sources.append(RSSSource(feeds=rss_cfgs))

    # Last.fm tag charts — only if API key is configured
    if settings.lastfm_api_key and LASTFM_TAGS:
        sources.append(
            LastFMTagSource(
                api_key=settings.lastfm_api_key,
                tags=list(LASTFM_TAGS),
                per_tag_limit=LASTFM_TAG_LIMIT,
            )
        )
    elif LASTFM_TAGS and not settings.lastfm_api_key:
        logger.warning("LASTFM_API_KEY not set — skipping Last.fm tag sources")

    # Personal-library discovery: seeds from the taste profile, rotated
    # through Last.fm getSimilar. Silently absent until the profile is
    # built (build_taste_profile.py) — the other sources keep running.
    if settings.lastfm_api_key:
        seed_artists = load_seed_artists(PIPELINE_DIR / "data")
        if seed_artists:
            sources.append(
                PersonalArtistsSource(
                    api_key=settings.lastfm_api_key,
                    seeds=seed_artists,
                    cursor_path=PIPELINE_DIR / "data" / "personal_seeds_cursor.json",
                    seeds_per_run=TASTE_DISCOVERY_SEEDS_PER_RUN,
                    similar_per_seed=TASTE_DISCOVERY_SIMILAR_PER_SEED,
                    tracks_per_artist=TASTE_DISCOVERY_TRACKS_PER_ARTIST,
                )
            )

    # Manual picks : editorial overrides (data/manual_picks.json).
    sources.append(ManualPicksSource(path=MANUAL_PICKS_FILE))

    # User-added custom feeds (rss.app etc.)
    sources.append(CustomFeedsSource(path=CUSTOM_FEEDS_FILE))

    return sources


def _dedupe_and_cap(tracks: list[Track], cap: int) -> list[Track]:
    """
    Déduplique et plafonne en servant les sources en tourniquet.

    L'ordre d'ajout faisait office de priorité : HypeMachine, ajoutée en
    premier avec 50 morceaux, saturait à elle seule le cap de 30. Mesuré le
    2026-08-19 — les 30 candidats retenus étaient hypem à 100 %. Les blogs
    RSS, PersonalArtists et surtout les picks manuels de Victor étaient
    calculés chaque nuit puis jetés sans jamais atteindre le téléchargement,
    et SOURCE_PRIORITY, appliqué plus loin dans download.py, opérait sur une
    liste déjà homogène : il était inerte.

    Trier simplement par priorité ne réglerait rien — PersonalArtists et ses
    116 candidats prendraient toutes les places à la place de HypeMachine.
    Le tourniquet, lui, garantit que chaque source est représentée : on sert
    un morceau par source à tour de rôle, dans l'ordre de SOURCE_PRIORITY,
    jusqu'au cap. Une source pauvre (un pick manuel) passe donc en entier,
    une source riche ne peut plus monopoliser.

    En cas de doublon entre sources, la mieux classée l'emporte : elle est
    servie la première.
    """
    buckets: dict[str, list[Track]] = {}
    for t in tracks:
        buckets.setdefault(t.get("source") or "", []).append(t)

    # sorted() est stable : à priorité égale, l'ordre d'ajout des sources
    # reste celui de discover.py.
    ordre = sorted(buckets, key=source_priority)
    curseurs = dict.fromkeys(ordre, 0)

    seen: set[str] = set()
    out: list[Track] = []
    while len(out) < cap:
        servi = False
        for source in ordre:
            if len(out) >= cap:
                break
            bucket = buckets[source]
            while curseurs[source] < len(bucket):
                t = bucket[curseurs[source]]
                curseurs[source] += 1
                key = _normalize_key(t["artist"], t["title"])
                if key in seen:
                    continue
                seen.add(key)
                out.append(t)
                servi = True
                break
        if not servi:      # toutes les sources sont épuisées
            break
    return out


def _save(tracks: list[Track]) -> bool:
    try:
        # Strip our internal `source` field to keep the file shape stable for
        # downstream consumers (download.py only reads canonical fields).
        slim: list[dict[str, Any]] = []
        for t in tracks:
            slim.append({
                "id": t["id"],
                "artist": t["artist"],
                "title": t["title"],
                "cover": t["cover"],
                "search": t["search"],
                "source": t.get("source", ""),
            })
        # Atomic write: a crash mid-write must not leave a truncated JSON
        # (load_tracks would silently return [] and lose the batch).
        tmp_file = OUTPUT_FILE.with_suffix(".json.tmp")
        tmp_file.write_text(
            json.dumps(slim, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp_file.replace(OUTPUT_FILE)
        return True
    except OSError as e:
        logger.error("Failed to write %s: %s", OUTPUT_FILE, e)
        return False


def main() -> int:
    logger.info("=== Discovery (multi-source) ===")

    is_valid, errors = validate_environment()
    if not is_valid:
        for err in errors:
            logger.error("Config: %s", err)
        return 1

    sources = _build_sources()

    all_tracks: list[Track] = []
    per_source_counts: dict[str, int] = {}
    for src in sources:
        before = len(all_tracks)
        try:
            fetched = src.fetch()
        except Exception as e:  # never let one bad source kill discovery
            logger.warning("Source %s crashed: %s", src.__class__.__name__, e)
            fetched = []
        all_tracks.extend(fetched)
        per_source_counts[src.__class__.__name__] = len(all_tracks) - before

    if not all_tracks:
        logger.warning("No tracks discovered from any source")
        # Don't error out: rotation still needs to run. Write empty list so
        # downstream consumers see an explicit "nothing new".
        _save([])
        return 0

    deduped = _dedupe_and_cap(all_tracks, DISCOVER_MAX_TRACKS)

    logger.info("")
    logger.info("Per-source contribution:")
    for name, count in per_source_counts.items():
        logger.info("  %-24s %3d", name, count)
    logger.info("")
    logger.info("Total after dedup: %d (cap %d)", len(deduped), DISCOVER_MAX_TRACKS)

    if not _save(deduped):
        return 1

    # Persist per-source contribution + dedup ratio for run.sh aggregation.
    stats_path = PIPELINE_DIR / "data" / "last_discover_stats.json"
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        stats_path.write_text(
            json.dumps({
                "raw_total": len(all_tracks),
                "deduped_total": len(deduped),
                "cap": DISCOVER_MAX_TRACKS,
                "per_source": per_source_counts,
            }, indent=2),
            encoding="utf-8",
        )
    except OSError:
        logger.warning("Could not write last_discover_stats.json")

    logger.info("Saved to %s", OUTPUT_FILE.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
