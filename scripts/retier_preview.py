#!/usr/bin/env python3
"""
Dry-run preview of the rotation re-tier pass.

Shows what classify.py:enforce_tiered_rotation WOULD do to the existing
AzuraCast library next time it runs, WITHOUT touching anything.

For each active track:
- Computes its NEW tier under the current ROTATION_CATEGORIES config
- Compares against its current AzuraCast playlist memberships
- Reports the diff (which playlists would be added or removed)

Output:
- Summary table : tier distribution before/after
- Detail of demotions (tracks losing playlists)
- Detail of promotions (tracks gaining playlists)

Usage:
    python3 scripts/retier_preview.py [--limit N] [--apply]

--apply : if passed, actually pushes the changes to AzuraCast (with
          rate-limit pacing). Without it, the script is read-only.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from http_client import AzuraCastClient  # noqa: E402
from settings import get_settings, validate_environment  # noqa: E402
from track_db import TrackDB, normalize_track_key  # noqa: E402

from classify import (  # noqa: E402
    compute_rotation_tier, tier_filter_dayparts, TIER_RANK,
)
from config import (  # noqa: E402
    ROTATION, ROTATION_CATEGORIES, get_dayparts_for_mood, DaypartSegment,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="Cap on tracks examined")
    parser.add_argument("--apply", action="store_true",
                        help="Actually push the changes (default: dry-run)")
    parser.add_argument("--show-no-change", action="store_true",
                        help="Also list tracks whose playlists don't change")
    args = parser.parse_args()

    ok, errs = validate_environment()
    if not ok:
        for e in errs:
            print(f"Config error: {e}", file=sys.stderr)
        return 1

    s = get_settings()
    c = AzuraCastClient(base_url=s.azuracast_url, api_key=s.azuracast_api_key,
                        station_id=s.azuracast_station_id, timeout=30)
    db_path = Path(__file__).parent.parent / "data" / "tracks.db"
    db = TrackDB(db_path)

    print(f"Pulling AzuraCast library + playlist map…")
    files = c.get_station_files()
    pl_map = {p["name"]: p["id"] for p in c.get_playlists()}
    now = time.time()
    print(f"  {len(files)} active files, {len(pl_map)} playlists\n")

    # Drop files past max_age_days (would be EXPIRED-deleted by main rotation,
    # not re-tiered).
    files = [
        f for f in files
        if (now - (
            (db.get_track_by_file_id(f.get("id")) or {}).get("uploaded_at")
            or f.get("uploaded_at") or f.get("mtime") or now
        )) / 86400 <= ROTATION.max_age_days
    ]
    if args.limit:
        files = files[: args.limit]

    before_tiers: Counter = Counter()
    after_tiers: Counter = Counter()
    no_change = 0
    promotions: list[dict] = []
    demotions: list[dict] = []
    skipped_no_mood = 0
    skipped_fresh = 0

    for f in files:
        file_id = f.get("id")
        artist = (f.get("artist") or "").strip()
        title = (f.get("title") or "").strip()

        db_t = db.get_track_by_file_id(file_id) if file_id else None
        if db_t:
            uploaded_at = db_t["uploaded_at"]
            play_count = db_t["play_count"]
            mood = db_t.get("mood")
            stored_tier = db_t.get("tier") or "LIGHT"
        else:
            uploaded_at = f.get("uploaded_at") or f.get("mtime") or now
            play_count = 0
            mood = None
            stored_tier = "LIGHT"

        age_days = (now - uploaded_at) / 86400
        before_tiers[stored_tier] += 1

        # Match the actual classify.py logic: only re-tier post-grace tracks
        # (CURRENT + FADING). FRESH stay at upload-time HEAVY.
        if age_days <= ROTATION.fresh_days:
            after_tiers["HEAVY"] += 1
            skipped_fresh += 1
            continue
        if not mood:
            after_tiers[stored_tier] += 1
            skipped_no_mood += 1
            continue

        new_tier = compute_rotation_tier(play_count, age_days)
        after_tiers[new_tier] += 1

        mood_dps = get_dayparts_for_mood(mood)
        target_names = {dp.value for dp in tier_filter_dayparts(mood_dps, new_tier)}
        current_names = {p.get("name") for p in (f.get("playlists") or []) if p.get("name")}

        if target_names == current_names:
            no_change += 1
            continue
        added = target_names - current_names
        removed = current_names - target_names

        info = {
            "artist": artist, "title": title, "age_days": age_days,
            "plays": play_count, "stored_tier": stored_tier, "new_tier": new_tier,
            "added": sorted(added), "removed": sorted(removed),
            "file_id": file_id, "target_names": target_names,
        }
        if TIER_RANK[new_tier] > TIER_RANK.get(stored_tier, 0):
            promotions.append(info)
        else:
            demotions.append(info)

    # ----- Reporting -----
    print("=" * 60)
    print("BEFORE (stored tier in tracks.db):")
    for t in ("HEAVY", "MEDIUM", "LIGHT", "DISCOVERY"):
        if t in before_tiers:
            print(f"  {t:10s} {before_tiers[t]:>4d}")
    print()
    print("AFTER (recomputed):")
    for t in ("HEAVY", "MEDIUM", "LIGHT"):
        print(f"  {t:10s} {after_tiers.get(t, 0):>4d}")
    print()
    total = sum(after_tiers.values())
    if total:
        for t in ("HEAVY", "MEDIUM", "LIGHT"):
            n = after_tiers.get(t, 0)
            print(f"    {t:10s} {n/total*100:>5.1f}%")
    print()
    print("Operations the re-tier pass WOULD perform:")
    print(f"  Fresh tracks (skip, stay HEAVY)         : {skipped_fresh}")
    print(f"  Skipped (no mood in DB)                 : {skipped_no_mood}")
    print(f"  No playlist change (tier stable)        : {no_change}")
    print(f"  Promotions (gain playlists)             : {len(promotions)}")
    print(f"  Demotions  (lose playlists)             : {len(demotions)}")
    print()

    # Show some examples
    def _show(rows, label, n=10):
        if not rows:
            return
        print(f"--- {label} examples (top {min(n, len(rows))}) ---")
        for r in rows[:n]:
            bits = []
            if r["added"]:
                bits.append("+" + ",".join(r["added"]))
            if r["removed"]:
                bits.append("-" + ",".join(r["removed"]))
            print(
                f"  [{r['stored_tier']:>10s}→{r['new_tier']:>6s}]  "
                f"({r['plays']:>3d}pl  {r['age_days']:>4.0f}d)  "
                f"{r['artist']} - {r['title']}\n"
                f"    {' '.join(bits)}"
            )
        print()

    promotions.sort(key=lambda x: x["plays"], reverse=True)
    demotions.sort(key=lambda x: x["age_days"])
    _show(promotions, "Promotions (most plays first)")
    _show(demotions, "Demotions (newest first)")

    # ----- Apply -----
    if args.apply:
        all_changes = promotions + demotions
        print("=" * 60)
        print(f"APPLYING {len(all_changes)} changes to AzuraCast…")
        applied = 0
        failed = 0
        for r in all_changes:
            target_ids = [pl_map[n] for n in r["target_names"] if n in pl_map]
            if not target_ids:
                failed += 1
                continue
            if c.assign_playlists(r["file_id"], target_ids):
                applied += 1
                # Persist the new tier in the DB
                tk = normalize_track_key(r["artist"], r["title"])
                if tk:
                    db.update_tier(tk, r["new_tier"])
            else:
                failed += 1
            # gentle rate-limit
            if (applied + failed) % 20 == 0:
                print(f"  {applied + failed}/{len(all_changes)} done…")
            time.sleep(0.05)
        print(f"Applied: {applied}  Failed: {failed}")

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
