#!/usr/bin/env python3
"""
Calibration audit for the personal taste filter.

Scores three populations against the taste profile and reports their
distributions so a threshold can be chosen with evidence:

1. POSITIVES  — held-out tracks from the personal library (files NOT in
   the profile: deeper album tracks of the same artists). Must pass.
2. LIBRARY    — every track currently in the radio (data/embeddings.npy).
   Shows where the existing, genre-filtered library sits.
3. NEGATIVES  — audio files in --negatives-dir (clearly off-taste tracks,
   e.g. downloaded samples of blocked genres). Must fail.

Usage:
  python3 scripts/calibrate_taste_filter.py [--positives 120]
      [--negatives-dir DIR] [--json OUT]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from audio_embeddings import EmbeddingStore, compute_embedding  # noqa: E402
from config import TASTE_FILTER  # noqa: E402
from taste_profile import AUDIO_EXTENSIONS, load_taste_profile, sample_library  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

PIPELINE_DIR = Path(__file__).parent.parent
DATA_DIR = PIPELINE_DIR / "data"
DEFAULT_MUSIC_ROOT = Path("/media/plex/Musique")


def _percentiles(scores: list[float]) -> dict[str, float]:
    arr = np.asarray(scores)
    return {
        "n": int(arr.size),
        "min": float(arr.min()),
        "p2": float(np.percentile(arr, 2)),
        "p5": float(np.percentile(arr, 5)),
        "p25": float(np.percentile(arr, 25)),
        "median": float(np.percentile(arr, 50)),
        "p95": float(np.percentile(arr, 95)),
        "max": float(arr.max()),
    }


def _report(label: str, stats: dict[str, float]) -> None:
    logger.info(
        "%-10s n=%-4d min=%.3f  p2=%.3f  p5=%.3f  p25=%.3f  med=%.3f  p95=%.3f  max=%.3f",
        label, stats["n"], stats["min"], stats["p2"], stats["p5"],
        stats["p25"], stats["median"], stats["p95"], stats["max"],
    )


def held_out_positives(profile, music_root: Path, count: int, k: int) -> list[dict]:
    """Score library files that are NOT part of the profile."""
    in_profile = set(profile.entry_paths)
    # per_artist=4 resamples the profile files first, then goes deeper —
    # everything not already in the profile is a held-out candidate.
    candidates = [
        (artist, path)
        for artist, path in sample_library(music_root, per_artist=4)
        if str(path) not in in_profile
    ]
    if not candidates:
        return []
    # Even spread across the artist list, deterministic.
    step = max(1, len(candidates) // count)
    picked = candidates[::step][:count]
    out: list[dict] = []
    for i, (artist, path) in enumerate(picked, 1):
        emb = compute_embedding(path)
        if emb is None:
            continue
        score = profile.score(emb, k=k)
        out.append({"artist": artist, "file": path.name, "score": score})
        if i % 20 == 0:
            logger.info("  positives: %d/%d scored", i, len(picked))
    return out


def library_scores(profile, k: int) -> list[dict]:
    store = EmbeddingStore(DATA_DIR)
    keys, matrix = store.all()
    if matrix is None:
        return []
    return [
        {"track": key, "score": profile.score(matrix[i], k=k)}
        for i, key in enumerate(keys)
    ]


def negative_scores(profile, negatives_dir: Path, k: int) -> list[dict]:
    out: list[dict] = []
    for f in sorted(negatives_dir.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        emb = compute_embedding(f)
        if emb is None:
            logger.warning("  negative embed failed: %s", f.name)
            continue
        out.append({"file": f.name, "score": profile.score(emb, k=k)})
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--music-root", type=Path, default=DEFAULT_MUSIC_ROOT)
    parser.add_argument("--positives", type=int, default=120)
    parser.add_argument("--negatives-dir", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None,
                        help="write full per-track results to this file")
    args = parser.parse_args()

    profile = load_taste_profile(DATA_DIR)
    if profile is None:
        logger.error("No taste profile — run build_taste_profile.py first")
        return 1
    k = TASTE_FILTER.k
    logger.info("Profile: %d vectors, %d artists — k=%d", profile.size, len(profile.artists), k)
    logger.info("")

    results: dict[str, list[dict]] = {}

    logger.info("Scoring radio library (%s)…", "data/embeddings.npy")
    results["library"] = library_scores(profile, k)

    logger.info("Scoring held-out positives from %s…", args.music_root)
    results["positives"] = held_out_positives(profile, args.music_root, args.positives, k)

    if args.negatives_dir and args.negatives_dir.is_dir():
        logger.info("Scoring negatives from %s…", args.negatives_dir)
        results["negatives"] = negative_scores(profile, args.negatives_dir, k)

    logger.info("")
    logger.info("=== Distributions (cosine k-NN mean, k=%d) ===", k)
    summary: dict[str, dict] = {}
    for label in ("positives", "library", "negatives"):
        rows = results.get(label)
        if rows:
            summary[label] = _percentiles([r["score"] for r in rows])
            _report(label, summary[label])

    if "positives" in summary:
        p = summary["positives"]
        logger.info("")
        logger.info("Suggested threshold candidates (accept ≈98%% / 95%% of held-out taste):")
        logger.info("  P2  of positives: %.3f", p["p2"])
        logger.info("  P5  of positives: %.3f", p["p5"])
        if "negatives" in summary:
            n = summary["negatives"]
            logger.info("Negatives max: %.3f — separation vs positives P5: %+.3f",
                        n["max"], p["p5"] - n["max"])

    if args.json:
        args.json.write_text(
            json.dumps({"summary": summary, "results": results}, indent=2,
                       ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("")
        logger.info("Full results written to %s", args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
