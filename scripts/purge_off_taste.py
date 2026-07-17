#!/usr/bin/env python3
"""
Purge AzuraCast tracks that don't match the personal taste profile.

Scores every track of the live library (via data/embeddings.npy) against
data/taste_profile.npy — the same score and threshold as the production
taste filter in classify.py — and deletes the ones below.

Dry-run by default: prints the hit list. Use --apply to actually delete
(each deletion is recorded in TrackDB so the 60-day cooldown prevents
re-download; embeddings are pruned by the next classify run).

Usage:
  python3 scripts/purge_off_taste.py                # dry-run
  python3 scripts/purge_off_taste.py --apply        # delete
  python3 scripts/purge_off_taste.py --threshold 0.64 --apply
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from audio_embeddings import EmbeddingStore  # noqa: E402
from classify import ClassifyClient  # noqa: E402
from config import TASTE_FILTER  # noqa: E402
from settings import get_settings  # noqa: E402
from taste_profile import load_taste_profile  # noqa: E402
from track_db import TrackDB, normalize_track_key  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

PIPELINE_DIR = Path(__file__).parent.parent
DATA_DIR = PIPELINE_DIR / "data"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold", type=float, default=TASTE_FILTER.threshold,
                        help="score below which a track is purged "
                             f"(default: production threshold {TASTE_FILTER.threshold})")
    parser.add_argument("--apply", action="store_true",
                        help="actually delete (default: dry-run)")
    args = parser.parse_args()

    profile = load_taste_profile(DATA_DIR)
    if profile is None or profile.size < TASTE_FILTER.min_profile_size:
        logger.error("Taste profile missing or too small — aborting")
        return 1

    store = EmbeddingStore(DATA_DIR)
    keys, matrix = store.all()
    if matrix is None or not keys:
        logger.error("No library embeddings (data/embeddings.npy) — aborting")
        return 1

    # Score every embedded track with the exact production scoring.
    scores = {
        key: profile.score(matrix[i], k=TASTE_FILTER.k)
        for i, key in enumerate(keys)
    }
    hits = sorted(
        ((s, key) for key, s in scores.items() if s < args.threshold)
    )
    logger.info("Library: %d embedded tracks — %d below threshold %.2f",
                len(keys), len(hits), args.threshold)
    if not hits:
        logger.info("Nothing to purge.")
        return 0

    settings = get_settings()
    client = ClassifyClient(
        base_url=settings.azuracast_url,
        api_key=settings.azuracast_api_key,
        station_id=settings.azuracast_station_id,
        timeout=settings.http_timeout,
    )
    if not client.health_check():
        logger.error("AzuraCast is not reachable. Aborting.")
        return 1

    # Map track_key → file id from the live library.
    files = client.get_all_files()
    by_key: dict[str, dict] = {}
    for f in files:
        artist, title = f.get("artist") or "", f.get("title") or ""
        if artist and title:
            by_key[normalize_track_key(artist, title)] = f

    deleted = missing = 0
    with TrackDB(DATA_DIR / "tracks.db") as track_db:
        for score, key in hits:
            f = by_key.get(key)
            if f is None:
                # Embedded but no longer on the server (already rotated out).
                logger.info("  [%.3f] %s — absent du serveur, ignoré", score, key)
                missing += 1
                continue
            label = f"[{score:.3f}] {f.get('artist')} — {f.get('title')}"
            if not args.apply:
                logger.info("  DRY-RUN  %s", label)
                continue
            if client.delete_file(f["id"]):
                track_db.record_deletion(key)
                deleted += 1
                logger.info("  SUPPRIMÉ %s", label)
            else:
                logger.warning("  ÉCHEC    %s", label)

    logger.info("")
    if args.apply:
        logger.info("Purge terminée : %d supprimés, %d déjà absents, seuil %.2f",
                    deleted, missing, args.threshold)
    else:
        logger.info("Dry-run : %d candidats à la purge (relancer avec --apply)",
                    len(hits) - missing)
    return 0


if __name__ == "__main__":
    sys.exit(main())
