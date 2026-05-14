#!/usr/bin/env python3
"""
Re-analyze existing AzuraCast library with MTG Arousal-Valence Ensemble.

Downloads tracks from AzuraCast, analyzes them with the new high-accuracy
models (~88%), updates ID3 tags, and re-uploads.

Usage:
    python reanalyze.py              # Re-analyze all tracks
    python reanalyze.py --dry-run    # Show what would be done without changes
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
TEMP_DIR = PIPELINE_DIR / "temp_reanalyze"


def download_file(client: AzuraCastClient, file_info: dict[str, Any], dest_path: Path) -> bool:
    """
    Download a file from AzuraCast.

    Args:
        client: AzuraCast client.
        file_info: File metadata from API.
        dest_path: Local destination path.

    Returns:
        True if successful.
    """
    file_id = file_info.get("id")
    if not file_id:
        logger.error("  No file ID found")
        return False

    try:
        # Ensure parent directory exists (defensive against mid-run deletion)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        # AzuraCast download endpoint: /file/{id}/play
        response = client.get(
            f"/api/station/{client.station_id}/file/{file_id}/play",
            stream=True,
        )

        if response.status_code != 200:
            logger.error("  Download failed: HTTP %s", response.status_code)
            return False

        with open(dest_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        return True

    except (ClientError, ServerError, HTTPConnectionError, OSError) as e:
        logger.error("  Download error: %s", e)
        return False


def upload_file(client: AzuraCastClient, local_path: Path, remote_path: str) -> bool:
    """
    Upload a file to AzuraCast, replacing the existing one.

    Uses the correct endpoint: /api/station/{stationId}/files/upload
    with multipart/form-data format.

    Args:
        client: AzuraCast client.
        local_path: Local file path.
        remote_path: Remote path in AzuraCast.

    Returns:
        True if successful.
    """
    try:
        # Extract directory from remote path
        remote_dir = str(Path(remote_path).parent)
        if remote_dir == ".":
            remote_dir = ""

        with open(local_path, "rb") as f:
            files = {"file": (Path(remote_path).name, f, "audio/mpeg")}
            data = {
                "path": remote_path,
                "currentDirectory": remote_dir,
            }

            # Use the correct upload endpoint
            response = client.post(
                f"/api/station/{client.station_id}/files/upload",
                files=files,
                data=data,
            )

        if response.status_code in (200, 201):
            return True

        logger.error("  Upload failed: HTTP %s", response.status_code)
        return False

    except (ClientError, ServerError, HTTPConnectionError, OSError) as e:
        logger.error("  Upload error: %s", e)
        return False


def update_file_metadata(
    client: AzuraCastClient,
    file_id: str | int,
    metadata: dict[str, Any],
) -> bool:
    """
    Update file metadata on AzuraCast via API.

    Args:
        client: AzuraCast client.
        file_id: File ID on AzuraCast.
        metadata: Metadata to update.

    Returns:
        True if successful.
    """
    try:
        response = client.put(
            f"/api/station/{client.station_id}/file/{file_id}",
            json=metadata,
        )

        if response.status_code in (200, 201):
            return True

        logger.warning("  Metadata update: HTTP %s", response.status_code)
        return False

    except (ClientError, ServerError, HTTPConnectionError, OSError) as e:
        logger.warning("  Metadata update error: %s", e)
        return False


def reanalyze_track(
    client: AzuraCastClient,
    file_info: dict[str, Any],
    temp_dir: Path,
    dry_run: bool = False,
) -> bool:
    """
    Re-analyze a single track.

    Args:
        client: AzuraCast client.
        file_info: File metadata from AzuraCast.
        temp_dir: Temporary directory for downloads.
        dry_run: If True, don't make changes.

    Returns:
        True if successful.
    """
    # Import here to avoid loading models if not needed
    from analyze import analyze_audio

    file_id = file_info.get("id") or file_info.get("unique_id")
    artist = file_info.get("artist", "Unknown")
    title = file_info.get("title", "Unknown")
    path = file_info.get("path", "")

    logger.info("  %s - %s", artist, title)

    if dry_run:
        logger.info("    [DRY-RUN] Would analyze and update")
        return True

    # Download to temp file
    temp_file = temp_dir / f"track_{file_id}.mp3"

    if not download_file(client, file_info, temp_file):
        return False

    # Analyze with new MTG models
    features = analyze_audio(str(temp_file))

    if not features:
        logger.error("    Analysis failed")
        temp_file.unlink(missing_ok=True)
        return False

    # Log results
    logger.info("    Valence: %+.2f | Arousal: %+.2f", features.valence, features.arousal)
    logger.info("    Mood: %s (%.0f%%) | Energy: %s", features.mood, features.confidence * 100, features.energy_level)

    # Best practice: ALWAYS write ID3 tags and re-upload
    # AzuraCast custom_fields require admin configuration, so we save metadata
    # directly in the file's ID3 tags for permanent storage
    from analyze import write_tags

    if not write_tags(str(temp_file), artist, title, features):
        logger.error("    Failed to write ID3 tags")
        return False

    if not upload_file(client, temp_file, path):
        logger.error("    Failed to re-upload file")
        return False

    logger.info("    OK (ID3 tags saved)")

    # Cleanup
    temp_file.unlink(missing_ok=True)

    return True


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Re-analyze AzuraCast library with MTG Arousal-Valence models"
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

    logger.info("=== Re-analyze AzuraCast Library ===")
    logger.info("Models: MTG Arousal-Valence Ensemble (~88% accuracy)")

    if args.dry_run:
        logger.info("Mode: DRY-RUN (no changes)")

    # Validate configuration
    is_valid, errors = validate_environment()
    if not is_valid:
        for error in errors:
            logger.error("Config error: %s", error)
        return 1

    settings = get_settings()

    # Check dependencies
    from analyze import check_dependencies, check_models

    if not check_dependencies():
        return 1

    if not check_models():
        return 1

    # Create AzuraCast client
    client = AzuraCastClient(
        base_url=settings.azuracast_url,
        api_key=settings.azuracast_api_key,
        station_id=settings.azuracast_station_id,
        timeout=settings.http_timeout,
    )

    # Health check
    if not client.health_check():
        logger.error("AzuraCast is not reachable")
        return 1

    logger.info("Connected to: %s", settings.azuracast_url)

    # Get all files
    try:
        files = client.get_station_files()
        logger.info("Library: %d tracks", len(files))
    except (ClientError, ServerError, HTTPConnectionError) as e:
        logger.error("Failed to fetch library: %s", e)
        return 1

    if not files:
        logger.info("No tracks in library")
        return 0

    # Apply limit if specified
    if args.limit > 0:
        files = files[:args.limit]
        logger.info("Processing: %d tracks (limited)", len(files))

    # Create temp directory
    TEMP_DIR.mkdir(exist_ok=True)

    # Process each track
    success_count = 0
    error_count = 0

    for i, file_info in enumerate(files, 1):
        artist = file_info.get("artist", "Unknown")
        title = file_info.get("title", "Unknown")
        logger.info("\n[%d/%d] %s - %s", i, len(files), artist, title)

        try:
            if reanalyze_track(client, file_info, TEMP_DIR, args.dry_run):
                success_count += 1
            else:
                error_count += 1
        except (ClientError, ServerError, HTTPConnectionError, OSError, RuntimeError) as e:
            logger.error("  Unexpected error: %s", e)
            error_count += 1

    # Cleanup temp directory
    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR, ignore_errors=True)

    # Summary
    logger.info("\n=== Results ===")
    logger.info("Success: %d/%d", success_count, len(files))
    if error_count > 0:
        logger.info("Errors: %d", error_count)

    return 0 if error_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
