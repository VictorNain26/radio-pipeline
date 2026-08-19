#!/usr/bin/env python3
"""
Playlist Management Script for AzuraCast.

Creates daypart-based playlists and assigns existing tracks based on mood/energy.

Features:
- Creates the zone playlists defined in config.DAYPARTS (currently 4:
  Dawn/Day/Dusk/Night), each scheduled for its time window every day
- Deletes old/orphaned playlists
- Assigns existing tracks to correct playlists based on mood
- Configures playlist scheduling

Usage:
    python setup_playlists.py              # Create playlists and assign tracks
    python setup_playlists.py --delete-old # Delete all playlists first
    python setup_playlists.py --dry-run    # Show what would be done
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

# Add parent directory for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from http_client import AzuraCastClient, ClientError, HTTPConnectionError, ServerError
from settings import get_settings, validate_environment

try:
    from config import (
        MoodCategory,
        DaypartSegment,
        DAYPARTS,
        ROTATION_CATEGORIES,
        get_enabled_dayparts,
    )
    from track_db import TrackDB, normalize_track_key
except ImportError as e:
    print(f"Error: config.py not found or invalid: {e}")
    sys.exit(1)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


class PlaylistManager(AzuraCastClient):
    """Extended client for playlist management."""

    def create_playlist(
        self,
        name: str,
        type_: str = "default",
        source: str = "songs",
        weight: int = 3,
        schedule_items: list[dict] | None = None,
    ) -> dict[str, Any] | None:
        """
        Create a new playlist.

        Args:
            name: Playlist name.
            type_: Playlist type (default, once_per_x_songs, etc.)
            source: Source type (songs).
            weight: Playlist weight for rotation.
            schedule_items: Optional schedule configuration.

        Returns:
            Playlist data or None on failure.
        """
        data = {
            "name": name,
            "type": type_,
            "source": source,
            "weight": weight,
            "is_enabled": True,
        }

        if schedule_items:
            data["schedule_items"] = schedule_items

        try:
            response = self.post(
                f"/api/station/{self.station_id}/playlists",
                json=data,
            )
            if response.status_code in (200, 201):
                return response.json()
            logger.error(f"Failed to create playlist '{name}': HTTP {response.status_code}")
            return None
        except (ClientError, ServerError, HTTPConnectionError) as e:
            logger.error(f"Failed to create playlist '{name}': {e}")
            return None

    def update_playlist_weight(self, playlist_id: int, weight: int) -> bool:
        """Update an existing playlist's rotation weight."""
        try:
            response = self.put(
                f"/api/station/{self.station_id}/playlist/{playlist_id}",
                json={"weight": weight},
            )
            return response.status_code == 200
        except (ClientError, ServerError, HTTPConnectionError) as e:
            logger.warning(f"Failed to update playlist {playlist_id} weight: {e}")
            return False

    def delete_playlist(self, playlist_id: int) -> bool:
        """
        Delete a playlist.

        Args:
            playlist_id: Playlist ID.

        Returns:
            True if successful.
        """
        try:
            response = self.delete(f"/api/station/{self.station_id}/playlist/{playlist_id}")
            return response.status_code in (200, 204)
        except (ClientError, ServerError, HTTPConnectionError) as e:
            logger.warning(f"Failed to delete playlist {playlist_id}: {e}")
            return False


def get_schedule_for_daypart(daypart: DaypartSegment) -> list[dict]:
    """
    Generate schedule items for a playlist (covers all 7 days).

    AzuraCast API format:
    - start_time/end_time: integers in HHMM format (e.g., 2100 = 21:00)
    - days: array of integers (1=Monday ... 7=Sunday)

    Args:
        daypart: Daypart segment.

    Returns:
        List of schedule configuration items.
    """
    profile = DAYPARTS[daypart]

    # All 7 days
    all_days = [1, 2, 3, 4, 5, 6, 7]

    start_hour = profile.start_hour
    end_hour = profile.end_hour

    schedule_items = []

    def hour_to_hhmm(hour: int, minutes: int = 0) -> int:
        return hour * 100 + minutes

    for day in all_days:
        if start_hour > end_hour:
            schedule_items.append({
                "start_time": hour_to_hhmm(start_hour, 0),
                "end_time": hour_to_hhmm(23, 59),
                "start_date": None,
                "end_date": None,
                "days": [day],
            })
            next_day = day + 1 if day < 7 else 1
            schedule_items.append({
                "start_time": hour_to_hhmm(0, 0),
                "end_time": hour_to_hhmm(end_hour, 0),
                "start_date": None,
                "end_date": None,
                "days": [next_day],
            })
        else:
            schedule_items.append({
                "start_time": hour_to_hhmm(start_hour, 0),
                "end_time": hour_to_hhmm(end_hour, 0),
                "start_date": None,
                "end_date": None,
                "days": [day],
            })

    return schedule_items


def get_mood_from_file(file_info: dict[str, Any]) -> str | None:
    """
    Extract mood from file metadata.

    AzuraCast stores custom fields in different ways depending on version.
    """
    # Try custom_fields dict
    custom_fields = file_info.get("custom_fields", {})
    if isinstance(custom_fields, dict):
        mood = custom_fields.get("mood") or custom_fields.get("MOOD")
        if mood:
            return mood

    # Try direct field
    mood = file_info.get("mood")
    if mood:
        return mood

    # Try text field (some versions store it there)
    text = file_info.get("text", "")
    if "Mood:" in text:
        for line in text.split("\n"):
            if line.startswith("Mood:"):
                return line.split(":", 1)[1].strip()

    return None


def get_mood_from_id3_tags(filepath: str) -> str | None:
    """
    Extract mood from ID3 tags of a local file.

    Args:
        filepath: Path to MP3 file.

    Returns:
        Mood string or None.
    """
    try:
        from mutagen.id3 import ID3

        tags = ID3(filepath)

        for frame in tags.getall("TXXX"):
            try:
                if frame.desc.upper() == "MOOD":
                    return frame.text[0]
            except (IndexError, AttributeError):
                continue

        return None
    except Exception:
        return None


def download_and_get_mood(
    client: PlaylistManager,
    file_info: dict[str, Any],
    temp_dir: Path,
) -> str | None:
    """
    Download file and extract mood from ID3 tags.

    Args:
        client: AzuraCast client.
        file_info: File metadata.
        temp_dir: Temporary directory.

    Returns:
        Mood string or None.
    """
    import tempfile

    file_id = file_info.get("id")
    if not file_id:
        return None

    # Download to temp file
    temp_file = temp_dir / f"track_{file_id}.mp3"

    try:
        response = client.get(
            f"/api/station/{client.station_id}/file/{file_id}/play",
            stream=True,
        )

        if response.status_code != 200:
            return None

        with open(temp_file, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        # Extract mood from ID3 tags
        mood = get_mood_from_id3_tags(str(temp_file))

        return mood

    except Exception as e:
        logger.debug(f"Failed to download/read file {file_id}: {e}")
        return None

    finally:
        # Cleanup
        if temp_file.exists():
            temp_file.unlink()


def create_all_playlists(
    client: PlaylistManager,
    existing_playlists: dict[str, int],
    dry_run: bool = False,
) -> dict[str, int]:
    """
    Create all required playlists.

    Args:
        client: Playlist manager client.
        existing_playlists: Map of existing playlist names to IDs.
        dry_run: If True, don't make changes.

    Returns:
        Updated playlist map.
    """
    logger.info("\n=== Creating Playlists ===")

    created = 0
    skipped = 0

    playlist_map = dict(existing_playlists)

    # Two variants per daypart, same schedule, different weights:
    #   "<Zone>"           (Library)   ← MEDIUM/LIGHT/GOLD, poids faible
    #   "<Zone>-Discovery" (Discovery) ← FRESH + HEAVY, poids fort
    variants: list[tuple[str, DaypartSegment, int]] = []
    for daypart in get_enabled_dayparts():
        variants.append((daypart.value, daypart, ROTATION_CATEGORIES.library_weight))
        variants.append((
            f"{daypart.value}{ROTATION_CATEGORIES.discovery_suffix}",
            daypart,
            ROTATION_CATEGORIES.discovery_weight,
        ))

    for name, daypart, weight in variants:
        if name in playlist_map:
            logger.info(f"  {name}: exists (ID {playlist_map[name]}) — syncing weight={weight}")
            skipped += 1
            if not dry_run:
                client.update_playlist_weight(playlist_map[name], weight)
            continue

        if dry_run:
            logger.info(f"  {name}: [DRY-RUN] would create (weight={weight})")
            playlist_map[name] = -1  # placeholder so the assignment preview sees it
            created += 1
            continue

        # Get schedule configuration (covers all 7 days)
        schedule = get_schedule_for_daypart(daypart)

        result = client.create_playlist(
            name=name,
            type_="default",
            weight=weight,
            schedule_items=schedule,
        )

        if result:
            playlist_id = result.get("id")
            playlist_map[name] = playlist_id
            logger.info(f"  {name}: created (ID {playlist_id}, weight={weight})")
            created += 1
        else:
            logger.error(f"  {name}: FAILED to create")

    logger.info(f"\nCreated: {created} | Existing: {skipped}")
    return playlist_map


def delete_all_playlists(
    client: PlaylistManager,
    existing_playlists: dict[str, int],
    dry_run: bool = False,
) -> None:
    """
    Delete all existing playlists.

    Args:
        client: Playlist manager client.
        existing_playlists: Map of existing playlist names to IDs.
        dry_run: If True, don't make changes.
    """
    logger.info("\n=== Deleting Old Playlists ===")

    if not existing_playlists:
        logger.info("No playlists to delete")
        return

    deleted = 0
    for name, playlist_id in existing_playlists.items():
        if dry_run:
            logger.info(f"  {name} (ID {playlist_id}): [DRY-RUN] would delete")
            deleted += 1
            continue

        if client.delete_playlist(playlist_id):
            logger.info(f"  {name} (ID {playlist_id}): deleted")
            deleted += 1
        else:
            logger.error(f"  {name} (ID {playlist_id}): FAILED to delete")

    logger.info(f"\nDeleted: {deleted}/{len(existing_playlists)}")


def assign_tracks_to_playlists(
    client: PlaylistManager,
    files: list[dict[str, Any]],
    playlist_map: dict[str, int],
    dry_run: bool = False,
) -> None:
    """
    Assign existing tracks to playlists based on mood.

    Downloads files temporarily to read ID3 tags if mood not in API response.

    Args:
        client: Playlist manager client.
        files: List of files from AzuraCast.
        playlist_map: Map of playlist names to IDs.
        dry_run: If True, don't make changes.
    """
    import shutil

    from classify import target_playlist_names

    logger.info("\n=== Assigning Tracks to Playlists ===")

    if not files:
        logger.info("No tracks to assign")
        return

    # Tier par morceau depuis la DB de rotation. Un fichier inconnu de la
    # DB part en Library (MEDIUM) : conservateur, le re-tier nocturne de
    # classify.py corrigera si besoin.
    track_db = TrackDB(Path(__file__).parent.parent / "data" / "tracks.db")

    # Create temp directory
    temp_dir = Path(__file__).parent.parent / "temp_playlist_setup"
    temp_dir.mkdir(exist_ok=True)

    assigned = 0
    failed = 0
    no_mood = 0

    try:
        for i, file_info in enumerate(files, 1):
            file_id = file_info.get("id")
            artist = file_info.get("artist", "Unknown")
            title = file_info.get("title", "Unknown")

            # First try to get mood from API response
            mood = get_mood_from_file(file_info)

            # Then the rotation DB (populated at upload since v2)
            if not mood:
                row = track_db.conn.execute(
                    "SELECT mood FROM tracks WHERE track_key = ? AND deleted_at IS NULL",
                    (normalize_track_key(artist, title),),
                ).fetchone()
                if row and row["mood"]:
                    mood = row["mood"]

            # Last resort: download and read ID3 tags
            if not mood and not dry_run:
                logger.info(f"  [{i}/{len(files)}] {artist} - {title}: downloading to read tags...")
                mood = download_and_get_mood(client, file_info, temp_dir)

            if not mood:
                logger.warning(f"  [{i}/{len(files)}] {artist} - {title}: no mood found")
                no_mood += 1
                continue

            # Validate mood
            try:
                mood_cat = MoodCategory(mood)
            except ValueError:
                logger.warning(f"  [{i}/{len(files)}] {artist} - {title}: invalid mood '{mood}'")
                failed += 1
                continue

            # Tier from rotation DB → same playlist targets as the nightly
            # pipeline (classify.target_playlist_names).
            tier = track_db.get_tier(normalize_track_key(artist, title)) or "MEDIUM"
            assigned_playlists = [
                name for name in dict.fromkeys(target_playlist_names(mood_cat, tier))
                if name in playlist_map
            ]

            if not assigned_playlists:
                logger.warning(f"  [{i}/{len(files)}] {artist} - {title}: no playlists for mood '{mood}'")
                failed += 1
                continue

            # Assign to playlists
            if dry_run:
                logger.info(f"  [{i}/{len(files)}] {artist} - {title} ({mood}): [DRY-RUN] -> {len(assigned_playlists)} playlists")
                assigned += 1
                continue

            playlist_ids = [playlist_map[name] for name in assigned_playlists]

            if client.assign_playlists(file_id, playlist_ids):
                logger.info(f"  [{i}/{len(files)}] {artist} - {title} ({mood}): -> {len(assigned_playlists)} playlists")
                assigned += 1
            else:
                logger.error(f"  [{i}/{len(files)}] {artist} - {title}: assignment FAILED")
                failed += 1

    finally:
        track_db.close()
        # Cleanup temp directory
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)

    logger.info(f"\nAssigned: {assigned} | No mood: {no_mood} | Failed: {failed}")


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Manage AzuraCast playlists and assign tracks"
    )
    parser.add_argument(
        "--delete-old",
        action="store_true",
        help="Delete all existing playlists first",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument(
        "--skip-assign",
        action="store_true",
        help="Skip track assignment (only create playlists)",
    )
    args = parser.parse_args()

    logger.info("=== AzuraCast Playlist Manager ===")

    if args.dry_run:
        logger.info("Mode: DRY-RUN (no changes)")

    # Validate configuration
    is_valid, errors = validate_environment()
    if not is_valid:
        for error in errors:
            logger.error(f"Config error: {error}")
        return 1

    settings = get_settings()

    # Create client
    client = PlaylistManager(
        base_url=settings.azuracast_url,
        api_key=settings.azuracast_api_key,
        station_id=settings.azuracast_station_id,
        timeout=settings.http_timeout,
    )

    # Health check
    if not client.health_check():
        logger.error("AzuraCast is not reachable")
        return 1

    logger.info(f"Connected to: {settings.azuracast_url}")

    # Get existing playlists
    try:
        playlists_data = client.get_playlists()
        existing_playlists = {p["name"]: p["id"] for p in playlists_data}
        logger.info(f"Existing playlists: {len(existing_playlists)}")
    except (ClientError, ServerError, HTTPConnectionError) as e:
        logger.error(f"Failed to fetch playlists: {e}")
        return 1

    # Delete old playlists if requested
    if args.delete_old:
        delete_all_playlists(client, existing_playlists, args.dry_run)
        existing_playlists = {}  # Reset after deletion

    # Create playlists
    playlist_map = create_all_playlists(client, existing_playlists, args.dry_run)

    # Skip assignment if requested
    if args.skip_assign:
        logger.info("\nSkipping track assignment (--skip-assign)")
        return 0

    # Get all files
    try:
        files = client.get_station_files()
        logger.info(f"\nLibrary: {len(files)} tracks")
    except (ClientError, ServerError, HTTPConnectionError) as e:
        logger.error(f"Failed to fetch files: {e}")
        return 1

    if not files:
        logger.info("No tracks to assign")
        return 0

    # Assign tracks to playlists
    assign_tracks_to_playlists(client, files, playlist_map, args.dry_run)

    logger.info("\n=== Done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
