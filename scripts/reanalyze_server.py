#!/usr/bin/env python3
"""
Re-analyze tracks without mood classification in tracks.db.

Targets only tracks where mood IS NULL (not yet classified by the
MTG Arousal-Valence Ensemble). Downloads from AzuraCast, analyzes
with 3 MTG models, assigns playlists based on mood, and updates tracks.db.
No re-upload needed - only mood classification + playlist assignment.

Usage:
    python reanalyze_server.py              # Re-analyze all tracks without mood
    python reanalyze_server.py --dry-run    # Show what would be done
    python reanalyze_server.py --limit 5    # Process only 5 tracks
"""

import argparse
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

# Add parent directory for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from http_client import AzuraCastClient, ClientError, HTTPConnectionError, ServerError
from settings import get_settings, validate_environment
from track_db import TrackDB

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Constants
SCRIPT_DIR = Path(__file__).parent
PIPELINE_DIR = SCRIPT_DIR.parent
TEMP_DIR = PIPELINE_DIR / "temp_reanalyze_server"


def get_tracks_without_mood(track_db: TrackDB) -> list[dict]:
    """
    Query tracks.db for active tracks without mood classification.

    Returns:
        List of track dicts with track_key, artist, title, azuracast_file_id.
    """
    rows = track_db.conn.execute(
        """SELECT track_key, artist, title, azuracast_file_id
           FROM tracks
           WHERE mood IS NULL
             AND deleted_at IS NULL
             AND azuracast_file_id IS NOT NULL"""
    ).fetchall()
    return [dict(r) for r in rows]


def get_azuracast_file_info(
    client: AzuraCastClient,
    file_id: int,
    files_cache: dict[int, dict],
) -> dict[str, Any] | None:
    """Look up file info from AzuraCast by file_id."""
    return files_cache.get(file_id)


def reanalyze_track(
    client: AzuraCastClient,
    track: dict,
    file_info: dict[str, Any],
    track_db: TrackDB,
    playlist_map: dict[str, int],
    temp_dir: Path,
    dry_run: bool = False,
) -> bool:
    """
    Re-analyze a single track: download, analyze, assign playlists, update DB.

    No re-upload needed - we only need the mood for tracks.db and playlist assignment.

    Returns:
        True if successful.
    """
    from analyze import analyze_audio

    file_id = track["azuracast_file_id"]
    artist = track["artist"]
    title = track["title"]
    track_key = track["track_key"]

    if dry_run:
        logger.info("  [DRY-RUN] Would analyze and update")
        return True

    # 1. Download
    temp_file = temp_dir / f"track_{file_id}.mp3"
    if not client.download_file_to(file_id, temp_file):
        return False

    # 2. Analyze
    features = analyze_audio(str(temp_file))
    if not features:
        logger.error("    Analysis failed")
        temp_file.unlink(missing_ok=True)
        return False

    logger.info("    V: %+.2f | A: %+.2f | Mood: %s (%.0f%%)", features.valence, features.arousal, features.mood, features.confidence * 100)

    # Cleanup temp file (no re-upload needed)
    temp_file.unlink(missing_ok=True)

    # 3. Assign playlists based on mood + rotation tier (Discovery/Library)
    from classify import target_playlist_names

    tier = track_db.get_tier(track_key) or "MEDIUM"
    playlist_names = [
        n for n in target_playlist_names(features.mood, tier) if n in playlist_map
    ]
    playlist_ids = [playlist_map[n] for n in playlist_names]

    if playlist_ids:
        if client.assign_playlists(file_id, playlist_ids):
            logger.info("    Playlists: %s", ", ".join(playlist_names))
        else:
            logger.warning("    Playlist assignment failed")

    # 6. Update tracks.db
    track_db.update_mood(track_key, features.mood)

    return True


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Re-analyze tracks without mood in tracks.db"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit number of tracks to process (0 = all)",
    )
    args = parser.parse_args()

    logger.info("=== Re-analyze Tracks Without Mood ===")
    logger.info("Models: MTG Arousal-Valence Ensemble (~88% accuracy)")

    if args.dry_run:
        logger.info("Mode: DRY-RUN (no changes)")

    # Validate environment
    is_valid, errors = validate_environment()
    if not is_valid:
        for error in errors:
            logger.error("Config error: %s", error)
        return 1

    settings = get_settings()

    # Open tracks.db
    db_path = PIPELINE_DIR / "data" / "tracks.db"
    if not db_path.exists():
        logger.error("tracks.db not found at %s", db_path)
        return 1

    track_db = TrackDB(db_path)

    try:
        # Query tracks without mood
        tracks = get_tracks_without_mood(track_db)
        logger.info("Tracks without mood: %d", len(tracks))

        if not tracks:
            logger.info("Nothing to do")
            return 0

        if args.limit > 0:
            tracks = tracks[:args.limit]
            logger.info("Processing: %d tracks (limited)", len(tracks))

        if not args.dry_run:
            # Check analysis dependencies
            from analyze import check_dependencies, check_models
            if not check_dependencies():
                return 1
            if not check_models():
                return 1

        # Connect to AzuraCast
        client = AzuraCastClient(
            base_url=settings.azuracast_url,
            api_key=settings.azuracast_api_key,
            station_id=settings.azuracast_station_id,
            timeout=settings.http_timeout,
        )

        if not client.health_check():
            logger.error("AzuraCast is not reachable")
            return 1

        logger.info("Connected to: %s", settings.azuracast_url)

        # Fetch all files from AzuraCast for lookup
        try:
            all_files = client.get_station_files()
        except (ClientError, ServerError, HTTPConnectionError) as e:
            logger.error("Failed to fetch library: %s", e)
            return 1

        # Build file_id -> file_info cache
        files_cache: dict[int, dict] = {}
        for f in all_files:
            fid = f.get("id")
            if fid:
                files_cache[fid] = f

        # Get playlist map
        try:
            playlists_data = client.get_playlists()
            playlist_map = {p["name"]: p["id"] for p in playlists_data}
        except (ClientError, ServerError, HTTPConnectionError) as e:
            logger.error("Failed to fetch playlists: %s", e)
            return 1

        # Create temp directory
        TEMP_DIR.mkdir(exist_ok=True)

        # Process tracks
        success_count = 0
        error_count = 0
        skip_count = 0

        for i, track in enumerate(tracks, 1):
            file_id = track["azuracast_file_id"]
            artist = track["artist"]
            title = track["title"]

            logger.info("\n[%d/%d] %s - %s", i, len(tracks), artist, title)

            # Look up in AzuraCast
            file_info = files_cache.get(file_id)
            if not file_info:
                logger.warning("  File ID %s not found on AzuraCast (maybe deleted)", file_id)
                skip_count += 1
                continue

            try:
                if reanalyze_track(client, track, file_info, track_db, playlist_map, TEMP_DIR, args.dry_run):
                    success_count += 1
                else:
                    error_count += 1
            except (ClientError, ServerError, HTTPConnectionError, OSError, RuntimeError) as e:
                logger.error("  Unexpected error: %s", e)
                error_count += 1

        # Cleanup
        if TEMP_DIR.exists():
            shutil.rmtree(TEMP_DIR, ignore_errors=True)

        # Summary
        logger.info("\n=== Results ===")
        logger.info("Success: %d/%d", success_count, len(tracks))
        if skip_count > 0:
            logger.info("Skipped (not on server): %d", skip_count)
        if error_count > 0:
            logger.info("Errors: %d", error_count)

        # Show remaining
        remaining = len(get_tracks_without_mood(track_db))
        logger.info("Remaining without mood: %d", remaining)

        return 0 if error_count == 0 else 1

    finally:
        track_db.close()


if __name__ == "__main__":
    sys.exit(main())
