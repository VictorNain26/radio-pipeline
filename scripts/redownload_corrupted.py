#!/usr/bin/env python3
"""
Re-download corrupted tracks identified by server audit.

This script takes the re-download list from audit_server.py and
queues them for re-download via the existing pipeline.

Usage:
    1. Run audit on server: python audit_server.py /path/to/media --fix
    2. Copy tracks-to-redownload.json to pipeline server
    3. Run: python redownload_corrupted.py tracks-to-redownload.json
"""

import argparse
import json
import logging
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

PIPELINE_DIR = Path(__file__).parent.parent
TRACKS_FILE = PIPELINE_DIR / "tracks-to-download.json"


def load_redownload_list(filepath: Path) -> list[dict]:
    """Load re-download list from audit."""
    if not filepath.exists():
        logger.error(f"File not found: {filepath}")
        return []

    try:
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            logger.error("Invalid format: expected a list")
            return []
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON: {e}")
        return []


def load_existing_queue() -> list[dict]:
    """Load existing download queue."""
    if not TRACKS_FILE.exists():
        return []

    try:
        with open(TRACKS_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_queue(tracks: list[dict]) -> None:
    """Save download queue."""
    with open(TRACKS_FILE, "w", encoding="utf-8") as f:
        json.dump(tracks, f, indent=2, ensure_ascii=False)


def convert_to_track_format(redownload_item: dict) -> dict:
    """Convert audit format to pipeline track format."""
    artist = redownload_item.get("artist", "Unknown")
    title = redownload_item.get("title", "Unknown")
    search = redownload_item.get("search", f"{artist} - {title}")

    return {
        "id": f"redownload_{hash(search) % 100000}",
        "artist": artist,
        "title": title,
        "cover": None,
        "search": search,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Queue corrupted tracks for re-download"
    )
    parser.add_argument(
        "redownload_file",
        type=str,
        help="Path to tracks-to-redownload.json from audit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be queued without saving",
    )

    args = parser.parse_args()

    logger.info("=== Re-download Corrupted Tracks ===")

    # Load re-download list
    redownload_list = load_redownload_list(Path(args.redownload_file))
    if not redownload_list:
        logger.info("No tracks to re-download")
        return 0

    logger.info(f"Found {len(redownload_list)} tracks to re-download")

    # Convert to pipeline format
    new_tracks = [convert_to_track_format(item) for item in redownload_list]

    # Load existing queue
    existing = load_existing_queue()
    existing_searches = {t.get("search", "").lower() for t in existing}

    # Filter out duplicates
    unique_tracks = [
        t for t in new_tracks
        if t["search"].lower() not in existing_searches
    ]

    if not unique_tracks:
        logger.info("All tracks already in queue")
        return 0

    logger.info(f"Adding {len(unique_tracks)} new tracks to queue")

    for track in unique_tracks:
        logger.info(f"  + {track['artist']} - {track['title']}")

    if args.dry_run:
        logger.info("Dry run - no changes made")
        return 0

    # Merge and save
    merged = existing + unique_tracks
    save_queue(merged)

    logger.info(f"\nQueued {len(unique_tracks)} tracks for re-download")
    logger.info(f"Total queue size: {len(merged)}")
    logger.info(f"\nRun 'python scripts/download.py' to start downloading")

    return 0


if __name__ == "__main__":
    sys.exit(main())
