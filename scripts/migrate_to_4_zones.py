#!/usr/bin/env python3
"""
One-shot migration from the 8-daypart programming to the 4-zone model
(Dawn / Day / Dusk / Night).

Source of truth for moods : data/tracks.db (the `mood` column populated
by classify.py during upload). Falls through gracefully on the rare
track without a mood entry (logged + counted, not skipped silently).

Steps applied in order (with --apply) :
  1. Create the new Dawn / Day / Dusk playlists in AzuraCast.
     The existing "Night" playlist is REUSED (same name + schedule).
  2. For every live track, compute target zones from mood × current tier,
     then PUT (replace) its playlist memberships in AzuraCast.
  3. Delete the 7 legacy 8-daypart playlists (Early_Morning,
     Morning_Commute, Morning_Work, Lunch, Afternoon, Evening_Commute,
     Evening). They will be empty by then.
  4. Update tracks.db tier column to match what was assigned.

Default mode is DRY-RUN — pass --apply to actually mutate AzuraCast.

Usage :
    python3 scripts/migrate_to_4_zones.py            # dry-run
    python3 scripts/migrate_to_4_zones.py --apply    # do it
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from http_client import (  # noqa: E402
    AzuraCastClient, ClientError, HTTPConnectionError, ServerError,
)
from settings import get_settings, validate_environment  # noqa: E402
from track_db import TrackDB, normalize_track_key  # noqa: E402

from classify import compute_rotation_tier, tier_filter_dayparts  # noqa: E402
from config import (  # noqa: E402
    DAYPARTS, DaypartSegment, MoodCategory,
    ROTATION, get_dayparts_for_mood, get_enabled_dayparts,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


NEW_ZONE_NAMES = [dp.value for dp in get_enabled_dayparts()]
# Sanity check at import time: we expect exactly 4 zones.
assert set(NEW_ZONE_NAMES) == {"Dawn", "Day", "Dusk", "Night"}, (
    f"Migration script expects 4 zones, found: {NEW_ZONE_NAMES}"
)

LEGACY_NAMES = {
    "Early_Morning", "Morning_Commute", "Morning_Work", "Lunch",
    "Afternoon", "Evening_Commute", "Evening",
    # Note: "Night" intentionally absent — reused as-is.
}


def _schedule_items_for_daypart(dp: DaypartSegment) -> list[dict[str, Any]]:
    """Build AzuraCast schedule_items entries for a daypart."""
    profile = DAYPARTS[dp]
    start_hhmm = profile.start_hour * 100
    end_hhmm = profile.end_hour * 100
    # AzuraCast convention: 00:00 → 2400, but we use 0 and 2400 ambiguously.
    # Their UI expects HHMM ints (0-2400). For zones crossing midnight
    # (NIGHT 22 → 5), end is 500.
    return [{
        "start_time": start_hhmm,
        "end_time": end_hhmm,
        "start_date": None,
        "end_date": None,
        "days": [1, 2, 3, 4, 5, 6, 7],  # every day
        "loop_once": False,
    }]


def _create_playlist(client: AzuraCastClient, dp: DaypartSegment) -> int | None:
    """Create a single playlist in AzuraCast. Returns its ID, or None on failure."""
    profile = DAYPARTS[dp]
    body = {
        "name": dp.value,
        "type": "default",
        "source": "songs",
        "order": "shuffle",
        "weight": 3,
        "is_enabled": True,
        "include_in_requests": True,
        "include_in_on_demand": False,
        "avoid_duplicates": True,
        "schedule_items": _schedule_items_for_daypart(dp),
    }
    try:
        resp = client.post(f"/api/station/{client.station_id}/playlists", json=body)
        if resp.status_code in (200, 201):
            return resp.json().get("id")
        logger.error("Create %s failed: HTTP %d %s",
                     dp.value, resp.status_code, resp.text[:200])
    except (ClientError, ServerError, HTTPConnectionError) as e:
        logger.error("Create %s failed: %s", dp.value, e)
    return None


def _delete_playlist(client: AzuraCastClient, playlist_id: int) -> bool:
    try:
        resp = client.delete(f"/api/station/{client.station_id}/playlist/{playlist_id}")
        return resp.status_code in (200, 204)
    except (ClientError, ServerError, HTTPConnectionError) as e:
        logger.warning("Delete playlist %d failed: %s", playlist_id, e)
        return False


def _assign_playlists(
    client: AzuraCastClient, file_id: int, playlist_ids: list[int],
) -> bool:
    try:
        resp = client.put(
            f"/api/station/{client.station_id}/file/{file_id}",
            json={"playlists": playlist_ids},
        )
        return resp.status_code == 200
    except (ClientError, ServerError, HTTPConnectionError) as e:
        logger.warning("Assign failed for file %d: %s", file_id, e)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Actually mutate AzuraCast (default: dry-run)")
    args = parser.parse_args()

    ok, errs = validate_environment()
    if not ok:
        for e in errs:
            print(f"Config error: {e}", file=sys.stderr)
        return 1

    s = get_settings()
    c = AzuraCastClient(base_url=s.azuracast_url, api_key=s.azuracast_api_key,
                        station_id=s.azuracast_station_id, timeout=30)
    db = TrackDB(Path(__file__).parent.parent / "data" / "tracks.db")
    now = time.time()

    # --- 1. Inventory ---
    logger.info("Fetching AzuraCast state…")
    files = c.get_station_files()
    playlists = c.get_playlists()
    existing = {p["name"]: p["id"] for p in playlists}
    logger.info("  %d files, %d existing playlists", len(files), len(existing))

    to_create = [DaypartSegment(name) for name in NEW_ZONE_NAMES if name not in existing]
    to_delete = [(name, existing[name]) for name in existing
                 if name in LEGACY_NAMES]

    logger.info("Plan : create=%d, delete=%d",
                len(to_create), len(to_delete))
    if to_create:
        logger.info("  → create: %s", ", ".join(dp.value for dp in to_create))
    if to_delete:
        logger.info("  → delete: %s", ", ".join(name for name, _ in to_delete))

    # --- 2. Plan re-assignments for every file ---
    plan: list[dict[str, Any]] = []
    no_mood_count = 0
    invalid_mood_count = 0

    for f in files:
        file_id = f.get("id")
        artist = (f.get("artist") or "").strip()
        title = (f.get("title") or "").strip()

        db_t = db.get_track_by_file_id(file_id) if file_id else None
        if db_t:
            mood_str = db_t.get("mood")
            uploaded_at = db_t["uploaded_at"]
            plays = db_t["play_count"]
            track_key = db_t["track_key"]
        else:
            mood_str = None
            uploaded_at = f.get("uploaded_at") or now
            plays = 0
            track_key = normalize_track_key(artist, title) if artist and title else ""

        if not mood_str:
            no_mood_count += 1
            continue

        try:
            mood = MoodCategory(mood_str)
        except ValueError:
            invalid_mood_count += 1
            logger.warning("  Unknown mood %r for %s - %s", mood_str, artist, title)
            continue

        age_days = (now - uploaded_at) / 86400
        new_tier = compute_rotation_tier(plays, age_days)
        target_zones = tier_filter_dayparts(get_dayparts_for_mood(mood), new_tier)
        target_names = [z.value for z in target_zones]

        plan.append({
            "file_id": file_id,
            "track_key": track_key,
            "artist": artist,
            "title": title,
            "mood": mood.value,
            "age_days": age_days,
            "plays": plays,
            "new_tier": new_tier,
            "target_names": target_names,
            "current_names": [p["name"] for p in (f.get("playlists") or []) if p.get("name")],
        })

    logger.info("Tracks planned for re-assignment : %d", len(plan))
    if no_mood_count:
        logger.warning("Tracks skipped (no mood in DB) : %d", no_mood_count)
    if invalid_mood_count:
        logger.warning("Tracks skipped (invalid mood) : %d", invalid_mood_count)

    # Stats : distribution of target zones
    zone_stats: dict[str, int] = {z: 0 for z in NEW_ZONE_NAMES}
    tier_stats: dict[str, int] = {"HEAVY": 0, "MEDIUM": 0, "LIGHT": 0}
    for p in plan:
        tier_stats[p["new_tier"]] += 1
        for z in p["target_names"]:
            zone_stats[z] += 1
    logger.info("Tier distribution (post-migration) :")
    for t, n in tier_stats.items():
        logger.info("  %-7s %4d  (%.0f%%)", t, n, n/max(1,len(plan))*100)
    logger.info("Zone memberships (a track can be in multiple) :")
    for z, n in zone_stats.items():
        logger.info("  %-6s %4d", z, n)

    if not args.apply:
        logger.info("\nDRY-RUN — re-run with --apply to mutate AzuraCast.")
        db.close()
        return 0

    # --- 3. Create missing playlists ---
    if to_create:
        logger.info("\nCreating %d new playlists…", len(to_create))
        for dp in to_create:
            new_id = _create_playlist(c, dp)
            if new_id is None:
                logger.error("FAILED to create %s — aborting migration", dp.value)
                db.close()
                return 2
            existing[dp.value] = new_id
            logger.info("  + %s (id=%d)", dp.value, new_id)

    # Refresh map after creates
    pl_map = existing

    # --- 4. Re-assign every track ---
    logger.info("\nApplying playlist memberships to %d tracks…", len(plan))
    assigned = 0
    failed = 0
    for i, p in enumerate(plan, 1):
        target_ids = [pl_map[n] for n in p["target_names"] if n in pl_map]
        if not target_ids:
            logger.warning(
                "  [%d/%d] no target ids for %s - %s (zones=%s) — skipped",
                i, len(plan), p["artist"], p["title"], p["target_names"],
            )
            failed += 1
            continue
        if _assign_playlists(c, p["file_id"], target_ids):
            assigned += 1
            if p["track_key"]:
                db.update_tier(p["track_key"], p["new_tier"])
            if i % 25 == 0:
                logger.info("  …%d/%d", i, len(plan))
        else:
            failed += 1
    logger.info("Assignments : %d ok, %d failed", assigned, failed)

    # --- 5. Delete legacy playlists ---
    if to_delete:
        logger.info("\nDeleting %d legacy playlists…", len(to_delete))
        for name, pid in to_delete:
            if _delete_playlist(c, pid):
                logger.info("  - %s (id=%d)", name, pid)
            else:
                logger.warning("  FAILED to delete %s (id=%d)", name, pid)

    db.close()
    logger.info("\n=== Migration done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
