#!/usr/bin/env python3
"""
One-shot: backfill iTunes cover art on AzuraCast library files that have none.

Usage:
    python3 scripts/backfill_covers.py [--dry-run] [--limit N]

--dry-run  Fetch covers from iTunes but don't upload to AzuraCast.
--limit N  Cap at N files (default: all).

Rate-limited at 2s/track to stay within iTunes Search API fair-use.
"""

import argparse
import logging
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from download import fetch_itunes_cover
from http_client import AzuraCastClient
from settings import get_settings, validate_environment

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

INTER_TRACK_DELAY = 2.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    is_valid, errors = validate_environment()
    if not is_valid:
        for e in errors:
            logger.error("Config: %s", e)
        return 1

    settings = get_settings()
    client = AzuraCastClient(
        base_url=settings.azuracast_url,
        api_key=settings.azuracast_api_key,
        station_id=settings.azuracast_station_id,
    )

    if not client.health_check():
        logger.error("AzuraCast unreachable.")
        return 1

    all_files = client.get_station_files()
    missing = [
        f for f in all_files
        if not (f.get("art") or "").strip()
        and (f.get("artist") or "").strip()
        and (f.get("title") or "").strip()
    ]
    logger.info("Files without artwork: %d / %d total", len(missing), len(all_files))

    if args.limit:
        missing = missing[: args.limit]
        logger.info("Capped at %d", args.limit)

    if not missing:
        logger.info("Nothing to do.")
        return 0

    updated = skipped = failed = 0

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for i, f in enumerate(missing, 1):
            artist = f["artist"].strip()
            title = f["title"].strip()
            file_id = f["id"]
            logger.info("[%d/%d] %s - %s", i, len(missing), artist, title)

            cover = fetch_itunes_cover(artist, title, tmp_path)

            if not cover:
                logger.info("  No cover found on iTunes")
                skipped += 1
            elif args.dry_run:
                logger.info("  [dry-run] Cover found (%d bytes), skipping upload", cover.stat().st_size)
                cover.unlink(missing_ok=True)
                updated += 1
            else:
                if client.update_file_art(file_id, cover):
                    logger.info("  Uploaded OK")
                    updated += 1
                else:
                    logger.warning("  Upload failed")
                    failed += 1
                cover.unlink(missing_ok=True)

            if i < len(missing):
                time.sleep(INTER_TRACK_DELAY)

    logger.info("=== Done ===")
    logger.info("Updated : %d", updated)
    logger.info("Not found: %d", skipped)
    logger.info("Failed  : %d", failed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
