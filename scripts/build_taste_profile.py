#!/usr/bin/env python3
"""
Build/refresh the personal taste profile from the Plex music library.

Samples 2 tracks per artist (spread across albums), computes CLAP
embeddings and writes data/taste_profile.npy + taste_profile_index.json.

Incremental: files already embedded in a previous run are reused, so a
re-run after adding albums only pays for the new artists.

Usage:
  python3 scripts/build_taste_profile.py [--music-root /media/plex/Musique]
                                         [--per-artist 2] [--limit N]

The nightly pipeline does NOT depend on this script: the profile lives
in data/ and is only rebuilt on demand.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

from audio_embeddings import compute_embedding  # noqa: E402
from taste_profile import (  # noqa: E402
    PROFILE_INDEX,
    PROFILE_NPY,
    sample_library,
    save_taste_profile,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

PIPELINE_DIR = Path(__file__).parent.parent
DATA_DIR = PIPELINE_DIR / "data"
DEFAULT_MUSIC_ROOT = Path("/media/plex/Musique")


def _load_existing(data_dir: Path) -> dict[str, np.ndarray]:
    """Map path -> embedding from a previous build (for incrementality)."""
    npy_path = data_dir / PROFILE_NPY
    idx_path = data_dir / PROFILE_INDEX
    if not npy_path.exists() or not idx_path.exists():
        return {}
    try:
        embeddings = np.load(npy_path)
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        entries = idx.get("entries", [])
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if embeddings.ndim != 2 or embeddings.shape[0] != len(entries):
        return {}
    return {e["path"]: embeddings[i] for i, e in enumerate(entries) if e.get("path")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--music-root", type=Path, default=DEFAULT_MUSIC_ROOT)
    parser.add_argument("--per-artist", type=int, default=2)
    parser.add_argument("--limit", type=int, default=0,
                        help="stop after N files (0 = no limit, for testing)")
    args = parser.parse_args()

    if not args.music_root.is_dir():
        logger.error("Music root not found: %s (SSD not mounted?)", args.music_root)
        return 1

    logger.info("Sampling library %s (%d per artist)…", args.music_root, args.per_artist)
    samples = sample_library(args.music_root, per_artist=args.per_artist)
    if args.limit:
        samples = samples[: args.limit]
    logger.info("%d files sampled from %d artists",
                len(samples), len({a for a, _ in samples}))
    if not samples:
        logger.error("No audio files found — aborting")
        return 1

    existing = _load_existing(DATA_DIR)
    logger.info("%d embeddings reusable from previous build", len(existing))

    rows: list[np.ndarray] = []
    entries: list[dict[str, str]] = []
    computed = failed = reused = 0
    checkpoint_every = 25

    def _flush() -> None:
        if rows:
            save_taste_profile(
                DATA_DIR,
                np.vstack(rows),
                entries,
                built_at=datetime.now(timezone.utc).isoformat(),
            )

    for i, (artist, path) in enumerate(samples, 1):
        path_str = str(path)
        emb = existing.get(path_str)
        if emb is not None:
            reused += 1
        else:
            emb = compute_embedding(path)
            if emb is None:
                failed += 1
                logger.warning("  [%d/%d] FAILED %s", i, len(samples), path.name)
                continue
            computed += 1
            if computed % 10 == 0:
                logger.info("  [%d/%d] %d computed, %d reused, %d failed",
                            i, len(samples), computed, reused, failed)
        rows.append(np.asarray(emb, dtype=np.float32))
        entries.append({"path": path_str, "artist": artist})
        # Periodic checkpoint so an interruption keeps progress.
        if computed and computed % checkpoint_every == 0:
            _flush()

    if not rows:
        logger.error("No embeddings computed — profile not written")
        return 1

    _flush()
    logger.info("")
    logger.info("Taste profile written: %d vectors (%d computed, %d reused, %d failed)",
                len(rows), computed, reused, failed)
    logger.info("  %s", DATA_DIR / PROFILE_NPY)
    logger.info("  %s", DATA_DIR / PROFILE_INDEX)
    return 0


if __name__ == "__main__":
    sys.exit(main())
