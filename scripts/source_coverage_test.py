#!/usr/bin/env python3
"""
Compare SoundCloud vs YouTube coverage on a real discovery queue.

For each track in `tracks-to-download.json` (or any user-supplied JSON
with [{artist, title}, ...]), probe both sources and report per-source
best match + score. Aggregate stats at the end so we can decide
data-first whether to add SoundCloud as a primary source.

Usage:
    python3 scripts/source_coverage_test.py [path-to-tracks.json] [--limit N]

Output:
- Per track : YT score / duration / channel + SC score / duration / uploader
- Stats     : YT-only / SC-only / both / neither, distribution by source preference

The "best" decision is made by reusing the existing scoring logic in
download._score_candidate (fuzzy artist/title match + duration sanity
+ uploader trust). Tracks scoring < download.MATCH_SCORE_THRESHOLD on a
source are considered NOT covered on that source.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from download import (  # noqa: E402
    MATCH_SCORE_THRESHOLD,
    _score_candidate,
)

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger(__name__)


def probe(search_prefix: str, artist: str, title: str, k: int = 5,
          timeout: int = 45) -> list[dict[str, Any]]:
    """
    Run `yt-dlp --dump-json` against either ytsearchN or scsearchN,
    return the parsed candidate list.
    """
    cmd = [
        "yt-dlp",
        "--dump-json",
        "--no-download",
        "--no-warnings",
        "--no-playlist",
        "--socket-timeout", "20",
        "--", f'{search_prefix}{k}:"{artist}" "{title}"',
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return []
    if result.returncode != 0:
        return []
    out = []
    for line in result.stdout.strip().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def best_candidate(artist: str, title: str, candidates: list[dict[str, Any]]) -> tuple[float, dict[str, Any] | None]:
    """Pick best by _score_candidate from download.py."""
    best_score = 0.0
    best = None
    for c in candidates:
        score, _ = _score_candidate(artist, title, c)
        if score > best_score:
            best_score = score
            best = c
    return best_score, best


def probe_track(track: dict[str, str]) -> dict[str, Any]:
    """Probe both sources for a single track. Returns row summary."""
    artist = (track.get("artist") or "").strip()
    title = (track.get("title") or "").strip()
    if not artist or not title:
        return {"artist": artist, "title": title, "yt_score": 0, "sc_score": 0,
                "yt_dur": 0, "sc_dur": 0, "yt_url": "", "sc_url": "", "yt_chan": "", "sc_user": ""}

    yt_cands = probe("ytsearch", artist, title, k=5)
    yt_score, yt_best = best_candidate(artist, title, yt_cands)

    sc_cands = probe("scsearch", artist, title, k=5)
    sc_score, sc_best = best_candidate(artist, title, sc_cands)

    return {
        "artist": artist,
        "title": title,
        "yt_score": yt_score,
        "sc_score": sc_score,
        "yt_dur": (yt_best or {}).get("duration") or 0,
        "sc_dur": (sc_best or {}).get("duration") or 0,
        "yt_url": (yt_best or {}).get("webpage_url") or "",
        "sc_url": (sc_best or {}).get("webpage_url") or "",
        "yt_chan": ((yt_best or {}).get("channel") or (yt_best or {}).get("uploader") or "")[:30],
        "sc_user": ((sc_best or {}).get("uploader") or "")[:30],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("queue", nargs="?",
                        default=str(Path(__file__).parent.parent / "tracks-to-download.json"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--json-out", default=None,
                        help="Save full per-track results to a JSON file")
    args = parser.parse_args()

    queue_path = Path(args.queue)
    if not queue_path.exists():
        print(f"Queue not found: {queue_path}", file=sys.stderr)
        return 1

    tracks = json.loads(queue_path.read_text(encoding="utf-8"))
    if args.limit:
        tracks = tracks[: args.limit]

    print(f"Probing {len(tracks)} tracks (workers={args.workers}, score threshold={MATCH_SCORE_THRESHOLD})")
    print(f"{'#':>3}  {'YT':>5}  {'SC':>5}  {'Δdur':>6}  artist - title")
    print("-" * 100)

    rows: list[dict[str, Any]] = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(probe_track, t): i for i, t in enumerate(tracks, 1)}
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                row = fut.result()
            except Exception as e:
                print(f"  err: {e}")
                continue
            ddur = abs((row["yt_dur"] or 0) - (row["sc_dur"] or 0))
            print(f"{i:>3}  {row['yt_score']:>5.2f}  {row['sc_score']:>5.2f}  {ddur:>6}  {row['artist']} - {row['title'][:50]}")
            rows.append(row)

    elapsed = time.time() - t0
    print(f"\nElapsed: {elapsed:.0f}s ({len(rows)/elapsed:.1f} tracks/s)")

    # --- aggregate stats ---
    th = MATCH_SCORE_THRESHOLD
    yt_ok = sum(1 for r in rows if r["yt_score"] >= th)
    sc_ok = sum(1 for r in rows if r["sc_score"] >= th)
    both = sum(1 for r in rows if r["yt_score"] >= th and r["sc_score"] >= th)
    neither = sum(1 for r in rows if r["yt_score"] < th and r["sc_score"] < th)
    yt_only = yt_ok - both
    sc_only = sc_ok - both

    print("\n=== COVERAGE ===")
    print(f"  YouTube only      : {yt_only:>4}  ({yt_only/len(rows)*100:.1f}%)")
    print(f"  SoundCloud only   : {sc_only:>4}  ({sc_only/len(rows)*100:.1f}%)")
    print(f"  Both              : {both:>4}  ({both/len(rows)*100:.1f}%)")
    print(f"  Neither           : {neither:>4}  ({neither/len(rows)*100:.1f}%)")
    print(f"  YT total          : {yt_ok:>4}  ({yt_ok/len(rows)*100:.1f}%)")
    print(f"  SC total          : {sc_ok:>4}  ({sc_ok/len(rows)*100:.1f}%)")

    # Among tracks on both sources, which scores higher?
    if both:
        yt_wins = sum(1 for r in rows if r["yt_score"] >= th and r["sc_score"] >= th and r["yt_score"] > r["sc_score"])
        sc_wins = sum(1 for r in rows if r["yt_score"] >= th and r["sc_score"] >= th and r["sc_score"] > r["yt_score"])
        tie = both - yt_wins - sc_wins
        print(f"\n=== When both available, who wins ===")
        print(f"  YT higher score   : {yt_wins:>4}")
        print(f"  SC higher score   : {sc_wins:>4}")
        print(f"  Tie               : {tie:>4}")

    # Duration mismatches > 10% (suggests preview clips or remixes)
    big_delta = [r for r in rows if r["yt_dur"] > 0 and r["sc_dur"] > 0
                 and abs(r["yt_dur"] - r["sc_dur"]) > max(30, 0.1 * r["yt_dur"])]
    print(f"\n=== Duration mismatches (>10% or >30s) ===")
    print(f"  {len(big_delta)} tracks. Examples:")
    for r in big_delta[:5]:
        print(f"    {r['artist']} - {r['title']}: YT={r['yt_dur']}s SC={r['sc_dur']}s")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(rows, indent=2, ensure_ascii=False))
        print(f"\nDetailed results written to {args.json_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
