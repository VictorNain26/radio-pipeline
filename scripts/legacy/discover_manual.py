#!/usr/bin/env python3
"""
Secondary discovery source: manual picks.

Reads data/manual_picks.json and merges entries into tracks-to-download.json.
This provides a fallback when HypeMachine is down or to inject curated tracks.

Format of manual_picks.json:
[
  {"artist": "Artist Name", "title": "Track Title"},
  ...
]
"""

import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    pipeline_dir = Path(__file__).parent.parent
    manual_file = pipeline_dir / "data" / "manual_picks.json"
    tracks_file = pipeline_dir / "tracks-to-download.json"

    logger.info("=== Manual Picks ===")

    # Load manual picks
    if not manual_file.exists():
        logger.info("No manual_picks.json found, skipping")
        return 0

    try:
        picks = json.loads(manual_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to read manual_picks.json: %s", e)
        return 1

    if not picks:
        logger.info("No manual picks to add")
        return 0

    # Validate entries
    valid_picks = []
    for entry in picks:
        artist = entry.get("artist", "").strip()
        title = entry.get("title", "").strip()
        if artist and title:
            valid_picks.append({
                "id": f"manual_{artist}_{title}"[:64],
                "artist": artist,
                "title": title,
                "cover": None,
                "search": f"{artist} - {title}",
            })
        else:
            logger.warning("Skipping invalid entry: %s", entry)

    if not valid_picks:
        logger.info("No valid manual picks")
        return 0

    # Load existing tracks-to-download.json
    existing = []
    if tracks_file.exists():
        try:
            existing = json.loads(tracks_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = []

    # Deduplicate by search string
    existing_searches = {t.get("search", "").lower() for t in existing}
    new_picks = [p for p in valid_picks if p["search"].lower() not in existing_searches]

    if not new_picks:
        logger.info("All manual picks already in download queue")
        return 0

    # Merge
    merged = existing + new_picks
    tracks_file.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")

    logger.info("Added %d manual picks (%d total in queue)", len(new_picks), len(merged))
    for p in new_picks:
        logger.info("  + %s", p["search"])

    return 0


if __name__ == "__main__":
    sys.exit(main())
